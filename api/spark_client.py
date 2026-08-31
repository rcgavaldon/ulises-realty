"""Flexmls / Spark API client — the live listing feed.

STATUS: wired but dormant. Every function is a no-op until SPARK_TOKEN is set
in the Modal secret. Nothing here can break the site: if the token is missing,
if Spark is down, or if a field is shaped differently than expected, the sync
records the failure and the site keeps serving the last good data (or the
built-in samples). Fail-safe by construction.

WHEN THE KEY LANDS — verify these against the live Spark docs before trusting
them. They are written from the documented Spark conventions, but the doc site
blocked automated fetches, so treat the marked lines as UNVERIFIED:
  - base URL, auth header shape, and the X-SparkApi-User-Agent requirement
  - the `days(n)` filter function used for the hot sheet
  - StandardFields key names
Run `python api/spark_probe.py` once the token exists — it prints the raw
shape of one listing so the mapping below can be corrected in minutes.
"""
import os

# UNVERIFIED — confirm against sparkplatform.com/docs when the token lands.
BASE = os.environ.get("SPARK_BASE", "https://replication.sparkapi.com/v1")
TIMEOUT = 25


def configured() -> bool:
    return bool(os.environ.get("SPARK_TOKEN"))


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['SPARK_TOKEN']}",
        "X-SparkApi-User-Agent": os.environ.get("SPARK_UA", "RG Automations/1.0"),
        "Accept": "application/json",
    }


def _get(path, params=None):
    import httpx
    r = httpx.get(f"{BASE}{path}", headers=_headers(), params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _map(rec):
    """Spark record -> our listing shape. Only IDX-displayable fields.

    Deliberately tolerant: any missing field becomes None rather than raising,
    so one odd record can't take down the whole sync.
    """
    f = rec.get("StandardFields", rec) or {}
    photos = f.get("Photos") or []
    img = ""
    if photos:
        p = photos[0]
        img = (p.get("UriLarge") or p.get("Uri800") or p.get("UriThumb") or "") if isinstance(p, dict) else ""

    street = " ".join(str(x) for x in [
        f.get("StreetNumber"), f.get("StreetDirPrefix"), f.get("StreetName"),
        f.get("StreetSuffix"),
    ] if x).strip()

    return {
        "id": rec.get("Id") or f.get("ListingId"),
        "address": street or f.get("UnparsedFirstLineAddress") or "",
        "area": f.get("SubdivisionName") or f.get("MlsArea") or f.get("City") or "",
        "city": f.get("City") or "",
        "postal": str(f.get("PostalCode") or ""),
        "price": _num(f.get("ListPrice")),
        "beds": _num(f.get("BedsTotal")),
        "baths": _num(f.get("BathsTotal")),
        "sqft": _num(f.get("BuildingAreaTotal")),
        "status": f.get("MlsStatus") or f.get("StandardStatus") or "",
        "img": img,
        "on_market": f.get("OnMarketDate") or "",
        "price_change": f.get("PriceChangeTimestamp") or "",
        "public_remarks": (f.get("PublicRemarks") or "")[:400],
        # Attribution is an IDX display requirement — carry it through.
        "office": f.get("ListOfficeName") or "",
        "agent": f.get("ListAgentName") or "",
        # Public shareable listing page (NOT the flexmls admin view).
        # ⚠️ UNVERIFIED which field carries it — check the probe output; the
        # Flexmls public portal link may need to be built from the ListingId.
        "url": f.get("VirtualTourURLUnbranded") or f.get("ListingURL") or "",
    }


def _clean(rows):
    out = []
    for r in rows or []:
        try:
            m = _map(r)
        except Exception:
            continue
        if m["address"] and m["price"]:
            out.append(m)
    return out


def _results(payload):
    try:
        return payload["D"]["Results"]
    except (KeyError, TypeError):
        return []


def my_listings(limit=24):
    """The agent's own active listings -> the Featured Homes grid."""
    if not configured():
        return []
    data = _get("/my/listings", {
        "_limit": limit,
        "_expand": "Photos",
        "_filter": "MlsStatus Eq 'Active'",
    })
    return _clean(_results(data))


def hot_sheet(limit=12, days=7, saved_search_id=None):
    """The 'hot list'.

    Preferred path: the agent's own saved search in Flexmls (he keeps a
    specific list). Pass its id via SPARK_HOTSHEET_ID and we use that verbatim,
    which is what he actually curates.

    Fallback: new-on-market or price-reduced inside `days`.
    """
    if not configured():
        return []
    sid = saved_search_id or os.environ.get("SPARK_HOTSHEET_ID")
    if sid:
        # UNVERIFIED endpoint shape — confirm with spark_probe.py.
        data = _get(f"/savedsearches/{sid}/listings", {"_limit": limit, "_expand": "Photos"})
        return _clean(_results(data))

    data = _get("/listings", {
        "_limit": limit,
        "_expand": "Photos",
        # UNVERIFIED: Spark's days(n) relative-date function.
        "_filter": (f"MlsStatus Eq 'Active' And "
                    f"(OnMarketDate Ge days({days}) Or PriceChangeTimestamp Ge days({days}))"),
        "_orderby": "-OnMarketDate",
    })
    return _clean(_results(data))
