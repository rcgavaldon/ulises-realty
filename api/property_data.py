"""Property value + property-tax estimator for El Paso County, TX.

WHY THIS EXISTS
    Ulises opens sales conversations with "here's what you're paying in
    property taxes." This module powers both the website's address-lookup
    lead magnet and Sofia's on-call property questions.

DATA POSTURE (read before changing anything)
    Everything here is a MODEL, not a republication of appraisal-district
    records. We never state an owner's name, never quote an official
    appraised value, and never present a number as authoritative. Output is
    always a RANGE, always labeled an estimate, always paired with "Ulises
    will pull the exact county numbers for you." That keeps us clear of data
    licensing and of any restriction on redistributing county records.

    When a real data provider is licensed, implement `_provider_lookup()`
    below and set PROPERTY_API_KEY / PROPERTY_API_VENDOR in the Modal secret.
    The provider result is merged over the model; the public contract of
    `estimate()` does not change, so nothing downstream needs edits.

    ⚠️ VERIFY ANNUALLY: tax rates and $/sqft below are modeled figures for
    demo/estimation. Refresh from the El Paso County Tax Assessor-Collector's
    published rate sheet each fall before quoting them to real homeowners.
"""
import os
import re

# ── Tax rates ────────────────────────────────────────────────────────────────
# Combined rate per $100 of taxable value, split into the pieces a homeowner
# actually sees on their statement. "school" is broken out separately because
# the Texas homestead exemption applies to the school portion.
# ⚠️ MODELED — verify against the county rate sheet before each tax season.
JURISDICTIONS = {
    "el_paso_episd": {
        "label": "City of El Paso / EPISD",
        "school": 1.05,
        "other": 1.74,        # city + county + hospital district + community college
    },
    "el_paso_ysleta": {
        "label": "City of El Paso / Ysleta ISD",
        "school": 1.19,
        "other": 1.74,
    },
    "el_paso_socorro": {
        "label": "City of El Paso / Socorro ISD",
        "school": 1.24,
        "other": 1.74,
    },
    "horizon_city": {
        "label": "Horizon City / Clint ISD",
        "school": 1.14,
        "other": 1.32,
    },
    "socorro": {
        "label": "Socorro / Socorro ISD",
        "school": 1.24,
        "other": 1.28,
    },
    "canutillo": {
        "label": "Canutillo ISD (county)",
        "school": 1.16,
        "other": 1.05,
    },
    "county_other": {
        "label": "El Paso County (unincorporated)",
        "school": 1.10,
        "other": 1.05,
    },
}

# Texas school-district homestead exemption (SB 4, approved by voters Nov 2025).
# ⚠️ VERIFY: confirm the current amount before each tax season.
HOMESTEAD_SCHOOL_EXEMPTION = 140_000

# ── Area model ───────────────────────────────────────────────────────────────
# ppsf = modeled price per finished square foot; med_sqft = typical home size,
# used only when the homeowner doesn't know their square footage.
AREAS = {
    "upper valley":  {"ppsf": 192, "med_sqft": 2200, "juris": "canutillo"},
    "west side":     {"ppsf": 186, "med_sqft": 2100, "juris": "el_paso_episd"},
    "cimarron":      {"ppsf": 190, "med_sqft": 2300, "juris": "el_paso_episd"},
    "coronado":      {"ppsf": 195, "med_sqft": 2400, "juris": "el_paso_episd"},
    "kern place":    {"ppsf": 178, "med_sqft": 2000, "juris": "el_paso_episd"},
    "sunset heights": {"ppsf": 150, "med_sqft": 1900, "juris": "el_paso_episd"},
    "central":       {"ppsf": 132, "med_sqft": 1500, "juris": "el_paso_episd"},
    "northeast":     {"ppsf": 143, "med_sqft": 1750, "juris": "el_paso_episd"},
    "east side":     {"ppsf": 152, "med_sqft": 1850, "juris": "el_paso_ysleta"},
    "far east":      {"ppsf": 147, "med_sqft": 1900, "juris": "el_paso_socorro"},
    "mission valley": {"ppsf": 138, "med_sqft": 1700, "juris": "el_paso_ysleta"},
    "horizon city":  {"ppsf": 145, "med_sqft": 1800, "juris": "horizon_city"},
    "socorro":       {"ppsf": 136, "med_sqft": 1700, "juris": "socorro"},
    "canutillo":     {"ppsf": 152, "med_sqft": 1900, "juris": "canutillo"},
    "anthony":       {"ppsf": 134, "med_sqft": 1700, "juris": "county_other"},
    "fort bliss":    {"ppsf": 150, "med_sqft": 1800, "juris": "el_paso_episd"},
}
DEFAULT_AREA = {"ppsf": 155, "med_sqft": 1800, "juris": "el_paso_episd"}

# ZIP → area. Lets us classify from an address string with no geocoder.
ZIP_AREA = {
    "79821": "anthony",     "79835": "canutillo",   "79836": "far east",
    "79838": "socorro",     "79849": "far east",    "79853": "far east",
    "79901": "central",     "79902": "kern place",  "79903": "central",
    "79904": "northeast",   "79905": "central",     "79906": "fort bliss",
    "79907": "east side",   "79908": "fort bliss",  "79911": "west side",
    "79912": "west side",   "79915": "east side",   "79916": "fort bliss",
    "79918": "fort bliss",  "79922": "upper valley", "79924": "northeast",
    "79925": "east side",   "79927": "socorro",     "79928": "horizon city",
    "79930": "central",     "79932": "upper valley", "79934": "northeast",
    "79935": "east side",   "79936": "far east",    "79938": "far east",
}

# Phrases a homeowner might type that map straight to an area.
AREA_ALIASES = {
    "westside": "west side", "west": "west side", "far west": "west side",
    "eastside": "east side", "east": "east side", "lower valley": "mission valley",
    "ne": "northeast", "north east": "northeast", "northwest": "upper valley",
    "utep": "kern place", "downtown": "central", "montecillo": "west side",
    "cimarron ridge": "cimarron", "tierra del este": "far east",
    "eastlake": "far east", "vista del sol": "east side",
}

CONDITION_ADJ = {"needs work": 0.86, "fair": 0.93, "average": 1.0,
                 "good": 1.06, "updated": 1.12, "excellent": 1.15}


def _digits(s):
    return "".join(c for c in (s or "") if c.isdigit())


def classify(address: str = "", area_hint: str = ""):
    """Return (area_key, area_cfg, how_we_matched). Never raises."""
    blob = f"{address or ''} {area_hint or ''}".lower()

    zips = re.findall(r"\b(79\d{3})\b", blob)
    for z in zips:
        if z in ZIP_AREA:
            key = ZIP_AREA[z]
            return key, AREAS[key], f"ZIP {z}"

    for alias, key in AREA_ALIASES.items():
        if alias in blob:
            return key, AREAS[key], f"'{alias}'"

    # longest area name first so "west side" wins over "west"
    for key in sorted(AREAS, key=len, reverse=True):
        if key in blob:
            return key, AREAS[key], f"'{key}'"

    return "el paso", DEFAULT_AREA, "El Paso countywide average"


def _provider_lookup(address: str):
    """Real assessor/AVM provider goes here once one is licensed.

    Must return a dict with any of: sqft, beds, baths, year_built,
    assessed_value, annual_tax, has_homestead. Anything it returns overrides
    the model. Return None when unavailable — the model then answers alone.
    """
    if not os.environ.get("PROPERTY_API_KEY"):
        return None
    return None  # ← implement against the chosen vendor; contract above


def estimate(address: str = "", sqft=None, beds=None, area_hint: str = "",
             condition: str = "average", homestead: bool = True):
    """Model a home's value range and annual property tax.

    Returns a plain dict. Every dollar figure is an ESTIMATE and the caller
    is responsible for presenting it as one.
    """
    area_key, cfg, matched_by = classify(address, area_hint)

    facts = _provider_lookup(address) or {}
    used_provider = bool(facts)

    try:
        sqft = int(float(sqft)) if sqft else None
    except (TypeError, ValueError):
        sqft = None
    sqft = facts.get("sqft") or sqft
    sqft_known = bool(sqft)
    if not sqft:
        sqft = cfg["med_sqft"]
        try:
            b = int(float(beds)) if beds else None
        except (TypeError, ValueError):
            b = None
        if b:
            # nudge off the area median by bedroom count
            sqft = int(cfg["med_sqft"] * (1 + 0.13 * (b - 3)))

    adj = CONDITION_ADJ.get(str(condition or "average").lower().strip(), 1.0)
    mid = facts.get("assessed_value") or int(round(sqft * cfg["ppsf"] * adj, -3))
    # wider band when we had to guess the square footage
    spread = 0.07 if (sqft_known or used_provider) else 0.12
    low = int(round(mid * (1 - spread), -3))
    high = int(round(mid * (1 + spread), -3))

    j = JURISDICTIONS[cfg["juris"]]
    school_taxable = mid
    if homestead:
        school_taxable = max(0, mid - HOMESTEAD_SCHOOL_EXEMPTION)
    school_tax = school_taxable * j["school"] / 100
    other_tax = mid * j["other"] / 100
    annual = facts.get("annual_tax") or int(round(school_tax + other_tax, -1))

    # What the exemption is worth on this house — the sales hook. Reported the
    # same either way: money they're saving, or money they're leaving on the
    # table by not having filed. Never collapses to zero just because they
    # said "no exemption".
    hs_savings = int(round(min(mid, HOMESTEAD_SCHOOL_EXEMPTION) * j["school"] / 100, -1))

    combined_rate = round(j["school"] + j["other"], 3)
    return {
        "ok": True,
        "area": area_key.title() if area_key != "el paso" else "El Paso",
        "matched_by": matched_by,
        "jurisdiction": j["label"],
        "sqft_used": sqft,
        "sqft_known": sqft_known,
        "value_low": low,
        "value_mid": mid,
        "value_high": high,
        "ppsf": cfg["ppsf"],
        "annual_tax": annual,
        "monthly_tax": int(round(annual / 12, -1)),
        "combined_rate_pct": combined_rate,
        "tax_breakdown": [
            {"name": "School district", "rate": j["school"],
             "amount": int(round(school_tax, -1))},
            {"name": "City, county, hospital & college", "rate": j["other"],
             "amount": int(round(other_tax, -1))},
        ],
        "homestead_applied": bool(homestead),
        "homestead_savings": hs_savings,
        "source": "provider+model" if used_provider else "model",
        "disclaimer": ("Estimate only — modeled from El Paso County area values and "
                       "current tax rates, not an official appraisal or tax statement."),
    }


def spoken(est: dict, address: str = "") -> str:
    """One-paragraph phone-friendly rendering for Sofia. No owner names, ever."""
    if not est.get("ok"):
        return "I couldn't pull an estimate for that address."
    who = f"{address}" if address else "that property"
    parts = [
        f"For {who} in the {est['area']} area, a home that size in today's market "
        f"estimates around ${est['value_low']:,} to ${est['value_high']:,}.",
        f"Property taxes there run about ${est['annual_tax']:,} a year — roughly "
        f"${est['monthly_tax']:,} a month — at a combined rate near "
        f"{est['combined_rate_pct']}% under {est['jurisdiction']}.",
    ]
    if est.get("homestead_savings"):
        if est.get("homestead_applied"):
            parts.append(f"That already assumes a homestead exemption, which is saving "
                         f"about ${est['homestead_savings']:,} a year — worth confirming "
                         f"it's actually on file, because a lot of people think it is and it isn't.")
        else:
            parts.append(f"And that's without a homestead exemption — filing one would cut "
                         f"about ${est['homestead_savings']:,} a year off that bill. It's free "
                         f"to file, and Ulises walks people through it all the time.")
    parts.append("Say clearly that this is an estimate and Ulises will pull the exact "
                 "county numbers and a full market analysis for them.")
    return " ".join(parts)


def compare(a: dict, b: dict, addr_a: str = "Property A", addr_b: str = "Property B") -> str:
    """Phone-friendly side-by-side of two estimates."""
    if not (a.get("ok") and b.get("ok")):
        return "I need both addresses before I can compare them."
    dv = a["value_mid"] - b["value_mid"]
    dt = a["annual_tax"] - b["annual_tax"]
    return (
        f"{addr_a}: about ${a['value_low']:,}-${a['value_high']:,}, taxes near "
        f"${a['annual_tax']:,} a year ({a['jurisdiction']}). "
        f"{addr_b}: about ${b['value_low']:,}-${b['value_high']:,}, taxes near "
        f"${b['annual_tax']:,} a year ({b['jurisdiction']}). "
        f"So {addr_a} estimates about ${abs(dv):,} "
        f"{'higher' if dv >= 0 else 'lower'} in value and about ${abs(dt):,} "
        f"{'more' if dt >= 0 else 'less'} in yearly taxes — a difference of roughly "
        f"${abs(int(dt / 12)):,} a month on the tax side alone. "
        "These are estimates; Ulises confirms exact figures."
    )
