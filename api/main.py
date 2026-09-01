"""Ulises Realty demo API — isolated Modal app (fully separate from Sofia prod).

POST /lead            form submit -> instant Retell outbound call + SMS lead card to owner
POST /retell-webhook  call_ended (no-answer -> SMS lead + schedule redial) and
                      call_analyzed (SMS summary to owner, honor opt-out)
GET|POST /telnyx-inbound  lead calls the 505 back -> Sofia answers (TeXML -> Retell SIP)
POST /value           website tool: address -> value range + property tax estimate
POST /tools/lookup-listings  Retell tool: Sofia searches Ulises's listings mid-call
POST /tools/property-lookup  Retell tool: Sofia prices any address + its taxes
POST /tools/compare-properties  Retell tool: Sofia compares two addresses
POST /tools/book-showing     Retell tool: Sofia books a tentative slot on the calendar
GET  /health

Crons: retry_worker (every 5 min, redial cadence within 9a-8p MT)
       weekly_report (Mon 9:15a MT, ROI text to owner)

Deploy:  modal deploy api/main.py
Secret:  ulises-realty (RETELL_API_KEY, TELNYX_API_KEY, AGENT_EN, AGENT_ES,
         FROM_NUMBER, OWNER_CELL, GCP_SA_JSON, CAL_ID, DNC_NUMBERS)
State:   modal.Dict 'ulises-realty-state'
         keys: lead:<phone>, retries (one dict phone->plan), optout:<phone>, stats:<iso-week>
"""
import json
import os
import time

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi[standard]==0.115.*", "httpx", "retell-sdk",
        "google-api-python-client", "google-auth", "tzdata",
    )
    .add_local_python_source("listings_data", "property_data", "spark_client", "sierra_client")
)
app = modal.App("ulises-realty-api")
state = modal.Dict.from_name("ulises-realty-state", create_if_missing=True)

TZ = "America/Denver"          # El Paso is Mountain Time
QUIET_START, QUIET_END = 9, 20  # only dial 9:00-19:59 local
RETRY_STEPS_MIN = [5, 60]       # attempt2 +5min, attempt3 +60min; attempt4 = next 9:15am
MAX_ATTEMPTS = 4                # 1 initial + 3 redials

NO_ANSWER_REASONS = {
    "dial_no_answer", "dial_busy", "dial_failed", "no_answer",
    "voicemail_reached", "machine_detected",
}


def _now_local():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TZ))


def _within_hours(dt=None):
    dt = dt or _now_local()
    return QUIET_START <= dt.hour < QUIET_END


def _next_morning_ts():
    from datetime import timedelta
    dt = _now_local()
    nxt = (dt + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
    return nxt.timestamp()


def _sms(to: str, text: str):
    import httpx
    try:
        r = httpx.post(
            "https://api.telnyx.com/v2/messages",
            headers={"Authorization": f"Bearer {os.environ['TELNYX_API_KEY']}"},
            json={"from": os.environ["FROM_NUMBER"], "to": to, "text": text[:1500]},
            timeout=15,
        )
        return r.status_code < 300
    except Exception:
        return False


def _sms_owner(text: str):
    _sms(os.environ["OWNER_CELL"], text)


def _blocked(phone: str) -> str | None:
    """Return reason string if this phone must never be dialed/texted."""
    dnc = {n.strip() for n in os.environ.get("DNC_NUMBERS", "").split(",") if n.strip()}
    if phone in dnc or phone[-10:] in {d[-10:] for d in dnc}:
        return "dnc"
    if state.get(f"optout:{phone}", False):
        return "optout"
    return None


def _place_call(lead: dict) -> str:
    """Fire the outbound Retell call for a stored lead. Returns status string."""
    from retell import Retell
    lang = lead.get("lang", "en")
    agent_id = os.environ["AGENT_ES"] if lang == "es" else os.environ["AGENT_EN"]
    try:
        client = Retell(api_key=os.environ["RETELL_API_KEY"])
        client.call.create_phone_call(
            from_number=os.environ["FROM_NUMBER"],
            to_number=lead["phone"],
            override_agent_id=agent_id,
            retell_llm_dynamic_variables={
                "name": lead.get("name", ""),
                "interest": lead.get("interest_desc", "real estate"),
                "message": lead.get("message", "none"),
                "call_language": "Spanish" if lang == "es" else "English",
                "call_direction": "outbound_callback",
                "property": lead.get("address") or "none given",
                "valuation": lead.get("valuation_line") or "none run",
                "prequalified": lead.get("prequalified") or "unknown",
                "own_or_rent": lead.get("own_rent") or "unknown",
                "move_date": lead.get("move_date") or "unknown",
                "lead_level": _lead_level(lead),
                "during_hours": "yes" if _during_hours() else "no",
            },
            metadata={"source": "ulises-realty", "phone": lead["phone"]},
        )
        return "initiated"
    except Exception as e:
        return f"failed: {e}"


def _bump_stat(key: str, n: int = 1):
    from datetime import date
    wk = date.today().isocalendar()
    sk = f"stats:{wk[0]}-w{wk[1]}"
    s = state.get(sk, {})
    s[key] = s.get(key, 0) + n
    state[sk] = s


# ── business hours / booking settings (editable from admin.html) ────────────
DEFAULT_SETTINGS = {
    "hours": {   # 24h "HH:MM" local El Paso; [] = closed
        "mon": [["09:00", "18:00"]], "tue": [["09:00", "18:00"]],
        "wed": [["09:00", "18:00"]], "thu": [["09:00", "18:00"]],
        "fri": [["09:00", "18:00"]], "sat": [["10:00", "14:00"]],
        "sun": [],
    },
    "slot_min": 20,      # appointment length
    "buffer_min": 5,     # gap enforced after each appointment
}


def _settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(state.get("settings", {}) or {})
    return s


def _cal_svc():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(os.environ["GCP_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds)


def _cal_id(demo: bool = False):
    """Live calendar for real leads; the demo calendar for anything flagged
    demo, so testing and pitch demos never touch the agent's real calendar."""
    if demo:
        return os.environ["CAL_ID"]
    return (state.get("settings", {}) or {}).get("cal_id") or os.environ["CAL_ID"]


def _is_demo(phone: str) -> bool:
    return bool(state.get(f"lead:{phone}", {}).get("demo"))


def _busy_windows(svc, start, end, demo=False):
    """Google freebusy for the working calendar; [] on failure (fail-open,
    the human confirms every tentative booking anyway)."""
    cal = _cal_id(demo)
    try:
        fb = svc.freebusy().query(body={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "items": [{"id": cal}],
        }).execute()
        from datetime import datetime
        return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                 datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                for b in fb["calendars"][cal].get("busy", [])]
    except Exception:
        return []


def _open_slots(day_dt, limit=3, demo=False):
    """Free slots on day_dt: business hours ∩ calendar free, slot+buffer sized."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TZ)
    s = _settings()
    step = timedelta(minutes=s["slot_min"] + s["buffer_min"])
    dur = timedelta(minutes=s["slot_min"])
    key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day_dt.weekday()]
    windows = s["hours"].get(key, [])
    if not windows:
        return []
    svc = _cal_svc()
    day_start = day_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    busy = _busy_windows(svc, day_start, day_start + timedelta(days=1), demo)
    now = datetime.now(tz)
    out = []
    for w in windows:
        try:
            h1, m1 = map(int, w[0].split(":"))
            h2, m2 = map(int, w[1].split(":"))
        except (ValueError, IndexError):
            continue
        cur = day_dt.replace(hour=h1, minute=m1, second=0, microsecond=0)
        wend = day_dt.replace(hour=h2, minute=m2, second=0, microsecond=0)
        while cur + dur <= wend:
            pad_end = cur + dur + timedelta(minutes=s["buffer_min"])
            if cur > now and not any(b0 < pad_end and b1 > cur for b0, b1 in busy):
                out.append(cur)
                if len(out) >= limit:
                    return out
            cur += step
    return out


def _lead_level(lead: dict) -> str:
    """hot / warm / cold — an expected move date is the strongest intent signal."""
    score = 0
    if lead.get("move_date"):
        score += 2
    if lead.get("prequalified") in ("yes", "cash"):
        score += 1
    if lead.get("interest") in ("sell", "value", "buysell"):
        score += 1
    if len(lead.get("message", "")) > 25:
        score += 1
    return "hot" if score >= 2 else ("warm" if score == 1 else "cold")


LEVEL_EMOJI = {"hot": "🔥 HOT LEAD", "warm": "🌤 Warm lead", "cold": "❄ Lead"}


def _during_hours() -> bool:
    """Inside the agent's bookable business hours right now?"""
    dt = _now_local()
    key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.weekday()]
    for w in _settings()["hours"].get(key, []):
        try:
            h1, m1 = map(int, w[0].split(":"))
            h2, m2 = map(int, w[1].split(":"))
            if (h1, m1) <= (dt.hour, dt.minute) < (h2, m2):
                return True
        except (ValueError, IndexError):
            continue
    return False


def _rich_event(name, phone, purpose, prop, lead, source):
    """Calendar event body Ulises can act on at a glance — not a dry title."""
    lead = lead or {}
    level = _lead_level(lead)
    flag = {"hot": "🔥 ", "warm": "", "cold": ""}[level]
    want = INTEREST.get(lead.get("interest", ""), {}).get("en", purpose)
    summary = f"{flag}Call: {name} — {want}" if purpose == "consult" \
        else f"{flag}{purpose.title()}: {name}" + (f" @ {prop}" if prop else "")
    rows = [f"Lead level: {level.upper()}", f"Phone: {phone}"]
    for label, key in (("Wants", "interest"), ("Pre-qualified", "prequalified"),
                       ("Currently", "own_rent"), ("Move date", "move_date"),
                       ("Property", "address"), ("Language", "lang"),
                       ("Their note", "message")):
        v = lead.get(key)
        if v and v not in ("none", "ninguno"):
            rows.append(f"{label}: {v}")
    if prop and prop != lead.get("address"):
        rows.append(f"Discussed property: {prop}")
    if lead.get("valuation_line"):
        rows.append(f"Value tool: {lead['valuation_line']}")
    rows.append(f"Booked via: {source} — TENTATIVE until you confirm")
    body = {"summary": summary, "description": "\n".join(rows)}
    if level == "hot":
        body["colorId"] = "11"   # tomato — jumps out on his calendar
    return body


INTEREST = {
    "buy":   {"en": "buying a home",         "es": "comprar casa"},
    "buysell": {"en": "selling their home and buying their next one",
                "es": "vender su casa y comprar la siguiente"},
    "rent":  {"en": "renting a home",        "es": "rentar una casa"},
    "sell":  {"en": "selling their home",    "es": "vender su casa"},
    "value": {"en": "a free home valuation", "es": "un avaluo gratis de su casa"},
    "other": {"en": "El Paso real estate",   "es": "bienes raices en El Paso"},
}


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("ulises-realty")],
    region="us-east",
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response

    web = FastAPI()
    web.add_middleware(
        CORSMiddleware,
        allow_origins=["https://rcgavaldon.github.io"],
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )

    def norm_phone(raw: str) -> str | None:
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return None

    @web.get("/health")
    def health():
        return {"ok": True, "app": "ulises-realty-api", "rev": "v6-demo"}

    # GitHub Actions fires these on schedule (Modal free plan's 5 cron slots
    # are taken by Sofia prod). Guarded by CRON_TOKEN.
    @web.post("/cron/retry")
    def cron_retry(req: Request):
        if req.headers.get("x-cron-token") != os.environ.get("CRON_TOKEN"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        retry_worker()
        return {"ok": True}

    @web.post("/cron/weekly")
    def cron_weekly(req: Request):
        if req.headers.get("x-cron-token") != os.environ.get("CRON_TOKEN"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        weekly_report()
        return {"ok": True}

    # Daily Flexmls pull. A second backup schedule hits /cron/spark-check,
    # which re-pulls only if the daily one didn't land — belt and suspenders.
    @web.post("/cron/spark-sync")
    def cron_spark_sync(req: Request):
        if req.headers.get("x-cron-token") != os.environ.get("CRON_TOKEN"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return spark_sync(force=True)

    @web.post("/cron/spark-check")
    def cron_spark_check(req: Request):
        if req.headers.get("x-cron-token") != os.environ.get("CRON_TOKEN"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return spark_sync(force=False)

    # Public: the site pulls its listing data from here once the feed is live.
    @web.get("/listings-feed")
    def listings_feed():
        cache = state.get("listings_cache", None)
        if not cache or not cache.get("featured"):
            return {"live": False}
        return {
            "live": True,
            "synced_at": cache.get("ts"),
            "featured": cache.get("featured", []),
            "hot": cache.get("hot", []),
        }

    # ── public: open phone-call slots for the site's booking picker ──────────
    @web.get("/slots")
    def slots(req: Request):
        demo = req.query_params.get("demo") in ("1", "true", "yes")
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ)
        out = []
        base = datetime.now(tz)
        for d in range(0, 5):
            day = (base + timedelta(days=d)).replace(hour=12, minute=0, second=0, microsecond=0)
            for slot in _open_slots(day, limit=4, demo=demo):
                # ASCII only — non-ASCII here mojibakes through the Windows mount
                out.append({"iso": slot.isoformat(),
                            "label": slot.strftime("%a %b %d, %I:%M %p").replace(", 0", ", ")})
            if len(out) >= 8:
                break
        return {"slots": out[:8], "slot_min": _settings()["slot_min"]}

    def _direct_book(name: str, phone: str, start_iso: str, lang: str, note: str,
                     demo: bool = False):
        """Site picked a slot -> event on the calendar, no instant call.
        Returns label on success, None if the slot is gone."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ)
        try:
            start = datetime.fromisoformat(start_iso)
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            day = start.replace(hour=12, minute=0)
            if start not in _open_slots(day, limit=50, demo=demo):
                return None
            s = _settings()
            svc = _cal_svc()
            lead = state.get(f"lead:{phone}", {})
            body = _rich_event(name, phone, "consult", "", lead,
                               "DEMO self-booking" if demo else "self-booked on the website")
            body["start"] = {"dateTime": start.isoformat(), "timeZone": TZ}
            body["end"] = {"dateTime": (start + timedelta(minutes=s["slot_min"])).isoformat(),
                           "timeZone": TZ}
            svc.events().insert(calendarId=_cal_id(demo), body=body).execute()
            _bump_stat("booked")
            bookings = state.get("bookings", [])
            bookings.append({"ts": time.time(), "start": start.isoformat(), "name": name,
                             "phone": phone,
                             "purpose": "DEMO consult" if demo else "consult (site)",
                             "property": ""})
            state["bookings"] = bookings[-200:]
            return start.strftime("%A %b %d at %I:%M %p").replace(" 0", " ")
        except Exception:
            return None

    # ── form submit ──────────────────────────────────────────────────────────
    @web.post("/lead")
    async def lead(req: Request):
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if not body.get("consent"):
            return JSONResponse({"error": "consent required"}, status_code=400)
        name = str(body.get("name", "")).strip()[:80]
        phone = norm_phone(str(body.get("phone", "")))
        if not name or not phone:
            return JSONResponse({"error": "name and valid US phone required"}, status_code=400)
        if _blocked(phone):
            return JSONResponse({"ok": True, "call": "skipped"})

        lang = "es" if str(body.get("language", "en")).lower().startswith("es") else "en"
        interest_key = str(body.get("interest", "other"))
        address = str(body.get("address", "")).strip()[:160]
        demo = bool(body.get("demo"))

        # If they ran the site's value tool, carry the numbers onto the call so
        # Sofia opens already knowing them.
        val = body.get("valuation") or {}
        valuation_line = ""
        if isinstance(val, dict) and val.get("ok"):
            valuation_line = (
                f"estimated ${val.get('value_low', 0):,}-${val.get('value_high', 0):,}, "
                f"taxes about ${val.get('annual_tax', 0):,}/yr in {val.get('jurisdiction', 'El Paso')}"
            )

        lead_rec = {
            "phone": phone, "name": name, "lang": lang,
            "interest": interest_key,
            "interest_desc": INTEREST.get(interest_key, INTEREST["other"])[lang],
            "message": str(body.get("message", "")).strip()[:300] or ("ninguno" if lang == "es" else "none"),
            "email": str(body.get("email", "")).strip()[:120],
            "address": address,
            "valuation_line": valuation_line,
            "prequalified": str(body.get("prequalified", "")).strip()[:20],
            "own_rent": str(body.get("own_rent", "")).strip()[:20],
            "move_date": str(body.get("move_date", "")).strip()[:20],
            "demo": demo,
            "ts": time.time(),
        }

        # rate limit: 3 calls/phone/hr, 30/day global
        now = time.time()
        rl = state.get("ratelimit", {"per": {}, "day": []})
        rl["per"][phone] = [t for t in rl["per"].get(phone, []) if now - t < 3600]
        rl["day"] = [t for t in rl["day"] if now - t < 86400]
        if len(rl["per"][phone]) >= 3 or len(rl["day"]) >= 30:
            _sms_owner(f"⚠️ ULISES DEMO: rate-limited lead {name} {phone}")
            return JSONResponse({"ok": True, "call": "rate-limited"})
        rl["per"][phone].append(now)
        rl["day"].append(now)
        state["ratelimit"] = rl

        state[f"lead:{phone}"] = lead_rec
        idx = state.get("lead_index", [])
        if phone in idx:
            idx.remove(phone)
        idx.append(phone)
        state["lead_index"] = idx[-500:]

        # They picked a slot on the site -> book it, no instant call.
        slot_iso = str(body.get("slot_iso", "")).strip()
        if slot_iso:
            label = _direct_book(name, phone, slot_iso, lang,
                                 f"Wants: {interest_key}. Note: {lead_rec['message'][:150]}",
                                 demo=demo)
            if label:
                _bump_stat("leads")
                if lang == "es":
                    _sms(phone, f"Confirmado: Ulises Ortega le llamará el {label} (hora de El Paso). Si necesita cambiarla, responda a este mensaje. Responda STOP para no ser contactado.")
                else:
                    _sms(phone, f"Confirmed: Ulises Ortega will call you {label} (El Paso time). Reply here if you need to change it. Reply STOP to opt out.")
                card = [("🧪 DEMO BOOKING (demo calendar)" if demo
                         else "🗓️ ULISES SITE BOOKING (no insta-call)"), name, phone,
                        f"Phone call: {label}", f"Wants: {interest_key} · Lang: {lang.upper()}"]
                if not demo:
                    try:
                        import sierra_client
                        if sierra_client.push_lead(lead_rec):
                            card.append("→ pushed to Sierra CRM")
                    except Exception:
                        pass
                _sms_owner("\n".join(card))
                return JSONResponse({"ok": True, "scheduled": label})
            # slot vanished -> fall through to the instant call so no lead is lost
        call_status = _place_call(lead_rec)
        _bump_stat("leads")
        _bump_stat("calls_placed")

        card = [(("🧪 DEMO — " if demo else "") +
                 f"{LEVEL_EMOJI[_lead_level(lead_rec)]} — ULISES SITE"), name, phone,
                f"Wants: {interest_key} · Lang: {lang.upper()}"]
        if address:
            card.append(f"Property: {address}")
        quals = [x for x in (
            f"prequal: {lead_rec['prequalified']}" if lead_rec["prequalified"] else "",
            f"now: {lead_rec['own_rent']}" if lead_rec["own_rent"] else "",
            f"move: {lead_rec['move_date']}" if lead_rec["move_date"] else "") if x]
        if quals:
            card.append(" · ".join(quals))
        if valuation_line:
            card.append(f"Ran value tool: {valuation_line}")
        card.append(f"Note: {lead_rec['message'][:120]}")
        card.append(f"Sofia call: {call_status[:80]}")
        try:
            import sierra_client
            if sierra_client.push_lead(lead_rec):
                card.append("→ pushed to Sierra CRM")
        except Exception:
            pass
        _sms_owner("\n".join(card))
        return JSONResponse({"ok": True, "call": call_status})

    # ── pitch-day drawing entries (QR on the last slide) ─────────────────────
    @web.post("/raffle")
    async def raffle(req: Request):
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        name = str(body.get("name", "")).strip()[:80]
        phone = norm_phone(str(body.get("phone", "")))
        if not name or not phone:
            return JSONResponse({"error": "name and valid US phone required"}, status_code=400)
        entry = {
            "name": name, "phone": phone,
            "email": str(body.get("email", "")).strip()[:120],
            "brokerage": str(body.get("brokerage", "")).strip()[:80],
            "pain": str(body.get("pain", "")).strip()[:200],
            "consent": bool(body.get("consent")),
            "ts": time.time(),
        }
        entries = state.get("raffle", [])
        entries = [e for e in entries if e["phone"] != phone]  # one entry per phone
        entries.append(entry)
        state["raffle"] = entries[-500:]
        _sms_owner(f"🎟️ ENTRY #{len(entries)} — {name} · {phone}"
                   f"{' · ' + entry['brokerage'] if entry['brokerage'] else ''}"
                   f"{chr(10) + 'Pain: ' + entry['pain'][:80] if entry['pain'] else ''}")
        return JSONResponse({"ok": True, "count": len(entries)})

    @web.get("/admin/raffle")
    def admin_raffle(req: Request):
        if req.headers.get("x-admin-token") != os.environ.get("CRON_TOKEN"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"entries": (state.get("raffle", []) or [])[::-1]}

    @web.post("/admin/raffle/draw")
    def admin_raffle_draw(req: Request):
        """Deterministic public draw: seeded by entry count so it's reproducible
        and auditable if anyone in the room asks how winners were picked."""
        if req.headers.get("x-admin-token") != os.environ.get("CRON_TOKEN"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        import hashlib
        entries = state.get("raffle", []) or []
        if len(entries) < 2:
            return {"error": "need at least 2 entries"}
        seed = hashlib.sha256(
            ("|".join(e["phone"] for e in entries)).encode()).hexdigest()
        order = sorted(range(len(entries)),
                       key=lambda i: hashlib.sha256(
                           (seed + str(i)).encode()).hexdigest())
        winners = [entries[order[0]], entries[order[1]]]
        state["raffle_winners"] = winners
        _sms_owner("🏆 DRAWING RESULT\n"
                   f"1st (3 months): {winners[0]['name']} {winners[0]['phone']}\n"
                   f"2nd (1 month): {winners[1]['name']} {winners[1]['phone']}")
        return {"first": winners[0], "second": winners[1], "total": len(entries)}

    # ── Retell webhook ───────────────────────────────────────────────────────
    @web.post("/retell-webhook")
    async def retell_webhook(req: Request):
        try:
            body = await req.json()
        except Exception:
            return {"ok": True}
        event = body.get("event")
        call = body.get("call", {}) or {}
        meta = call.get("metadata") or {}
        if meta.get("source") != "ulises-realty" and call.get("direction") != "inbound":
            return {"ok": True}
        phone = meta.get("phone") or call.get("to_number") or ""
        if call.get("direction") == "inbound":
            phone = call.get("from_number") or phone

        if event == "call_ended":
            reason = (call.get("disconnection_reason") or "").lower()
            dur = int((call.get("duration_ms") or 0) / 1000)
            answered = reason not in NO_ANSWER_REASONS and dur >= 12
            retries = state.get("retries", {})
            if answered:
                if phone in retries:
                    retries.pop(phone, None)
                    state["retries"] = retries
                _bump_stat("connected")
            elif call.get("direction") != "inbound" and phone:
                plan = retries.get(phone, {"attempts": 1, "texted": False})
                lead_rec = state.get(f"lead:{phone}", {"phone": phone, "lang": "en", "name": ""})
                if not plan.get("texted"):
                    if lead_rec.get("lang") == "es":
                        _sms(phone, f"Hola {lead_rec.get('name','')}, soy Sofía, asistente de Ulises Ortega Bienes Raíces. Le llamé por su solicitud en la página — llame o mande texto a este número cuando guste. Responda STOP para no ser contactado.")
                    else:
                        _sms(phone, f"Hi {lead_rec.get('name','')}, this is Sofia with Ulises Ortega Real Estate — I just tried calling about your inquiry. Call or text me back here anytime. Reply STOP to opt out.")
                    plan["texted"] = True
                if plan["attempts"] < MAX_ATTEMPTS:
                    if plan["attempts"] - 1 < len(RETRY_STEPS_MIN):
                        nxt = time.time() + RETRY_STEPS_MIN[plan["attempts"] - 1] * 60
                    else:
                        nxt = _next_morning_ts()
                    plan["next_at"] = nxt
                    retries[phone] = plan
                    state["retries"] = retries
                else:
                    retries.pop(phone, None)
                    state["retries"] = retries
                    _sms_owner(f"📵 ULISES DEMO: no answer after {MAX_ATTEMPTS} tries — {lead_rec.get('name','?')} {phone}. Left SMS.")
            return {"ok": True}

        if event == "call_analyzed":
            analysis = call.get("call_analysis", {}) or {}
            custom = analysis.get("custom_analysis_data", {}) or {}
            if str(custom.get("opt_out", "")).lower() in ("true", "yes", "1"):
                state[f"optout:{phone}"] = True
                retries = state.get("retries", {})
                retries.pop(phone, None)
                state["retries"] = retries
                _sms_owner(f"🚫 ULISES DEMO: {phone} asked not to be contacted. Honored.")
                return {"ok": True}
            dur = int((call.get("duration_ms") or 0) / 1000)
            direction = "inbound" if call.get("direction") == "inbound" else "callback"
            lines = [f"📋 SOFIA {direction.upper()} DONE ({dur}s) — {phone}"]
            for k in ("areas", "budget", "preapproved", "timeline", "callback_time", "must_haves"):
                v = (custom.get(k) or "").strip()
                if v and v.lower() not in ("unknown", "n/a", "none", ""):
                    lines.append(f"{k}: {v}")
            summary = (analysis.get("call_summary") or "").strip()
            if summary:
                lines.append(f"Summary: {summary[:350]}")
            rec = call.get("recording_url")
            if rec:
                lines.append(f"Rec: {rec}")
            try:
                import sierra_client
                sierra_client.add_call_note(phone, "\n".join(lines))
            except Exception:
                pass
            _sms_owner("\n".join(lines))
            return {"ok": True}

        return {"ok": True}

    # ── inbound: lead calls the 505 back ─────────────────────────────────────
    @web.api_route("/telnyx-inbound", methods=["GET", "POST"])
    async def telnyx_inbound(req: Request):
        import asyncio
        from retell import Retell
        try:
            form = await req.form()
        except Exception:
            form = {}

        def tp(key, default=""):
            v = form.get(key) if hasattr(form, "get") else None
            return v or req.query_params.get(key, default) or default

        from_number = tp("From", "unknown")
        to_number = tp("To", os.environ["FROM_NUMBER"])
        known = state.get(f"lead:{from_number}", None)
        lang = (known or {}).get("lang", "en")
        agent_id = os.environ["AGENT_ES"] if lang == "es" else os.environ["AGENT_EN"]
        if known and known.get("name"):
            begin = (f"¡Hola {known['name']}! Habla Sofía, la asistente virtual de Ulises Ortega — qué bueno que regresó la llamada. ¿En qué le ayudo?"
                     if lang == "es" else
                     f"Hi {known['name']}! This is Sofia, Ulises Ortega's virtual assistant — thanks for calling back. How can I help?")
        else:
            begin = "Thank you for calling Ulises Ortega Real Estate — this is Sofia, his virtual assistant. How can I help you today?"
        try:
            client = Retell(api_key=os.environ["RETELL_API_KEY"])
            call = await asyncio.to_thread(
                client.call.register_phone_call,
                agent_id=agent_id,
                direction="inbound",
                from_number=from_number,
                to_number=to_number,
                retell_llm_dynamic_variables={
                    "name": (known or {}).get("name", "there"),
                    "interest": (known or {}).get("interest_desc", "El Paso real estate"),
                    "message": (known or {}).get("message", "none"),
                    "call_language": "Spanish" if lang == "es" else "English",
                    "call_direction": "inbound",
                    "property": (known or {}).get("address") or "none given",
                    "valuation": (known or {}).get("valuation_line") or "none run",
                    "prequalified": (known or {}).get("prequalified") or "unknown",
                    "own_or_rent": (known or {}).get("own_rent") or "unknown",
                    "move_date": (known or {}).get("move_date") or "unknown",
                    "lead_level": _lead_level(known or {}),
                    "during_hours": "yes" if _during_hours() else "no",
                },
                agent_override={"retell_llm": {"begin_message": begin}},
            )
            _bump_stat("inbound")
            twiml = ('<?xml version="1.0" encoding="UTF-8"?>'
                     f'<Response><Dial><Sip>sip:{call.call_id}@sip.retellai.com</Sip></Dial></Response>')
        except Exception:
            twiml = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Response><Say>Thanks for calling Ulises Ortega Real Estate. '
                     'Please try again in a moment.</Say><Hangup/></Response>')
        return Response(content=twiml, media_type="application/xml")

    # ── website: home value + property tax lead magnet ───────────────────────
    @web.post("/value")
    async def value(req: Request):
        from property_data import estimate
        try:
            body = await req.json()
        except Exception:
            body = {}
        addr = str(body.get("address", "")).strip()[:160]
        if not addr:
            return JSONResponse({"ok": False, "error": "address required"}, status_code=400)
        est = estimate(
            address=addr,
            sqft=body.get("sqft"),
            beds=body.get("beds"),
            condition=body.get("condition", "average"),
            homestead=bool(body.get("homestead", True)),
        )
        _bump_stat("value_lookups")
        return JSONResponse(est)

    # ── Retell custom tools ──────────────────────────────────────────────────
    @web.post("/tools/property-lookup")
    async def property_lookup(req: Request):
        """Sofia prices any address on a call: value range + property taxes."""
        from property_data import estimate, spoken
        try:
            body = await req.json()
        except Exception:
            body = {}
        args = body.get("args", body) or {}
        addr = str(args.get("address") or "").strip()
        if not addr:
            return {"result": "Ask the caller for the property address first."}
        est = estimate(
            address=addr, sqft=args.get("sqft"), beds=args.get("beds"),
            condition=args.get("condition", "average"),
            homestead=bool(args.get("homestead", True)),
        )
        _bump_stat("property_lookups")
        return {"result": spoken(est, addr)}

    @web.post("/tools/compare-properties")
    async def compare_properties(req: Request):
        """Sofia compares two addresses side by side — value and yearly taxes."""
        from property_data import compare, estimate
        try:
            body = await req.json()
        except Exception:
            body = {}
        args = body.get("args", body) or {}
        a_addr = str(args.get("address_a") or "").strip()
        b_addr = str(args.get("address_b") or "").strip()
        if not (a_addr and b_addr):
            return {"result": "Ask the caller for both addresses before comparing."}
        a = estimate(address=a_addr, sqft=args.get("sqft_a"), beds=args.get("beds_a"))
        b = estimate(address=b_addr, sqft=args.get("sqft_b"), beds=args.get("beds_b"))
        _bump_stat("property_lookups", 2)
        return {"result": compare(a, b, a_addr, b_addr)}

    @web.post("/tools/lookup-listings")
    async def lookup_listings(req: Request):
        from listings_data import search
        try:
            body = await req.json()
        except Exception:
            body = {}
        args = body.get("args", body) or {}
        hot_only = str(args.get("hot_only", "")).lower() in ("true", "yes", "1")

        # Prefer the live Flexmls cache once the sync is running.
        cache = state.get("listings_cache", None)
        if cache and cache.get("featured"):
            rows = cache.get("hot", []) if hot_only else cache.get("featured", [])
            q_addr = str(args.get("address") or "").lower()
            q_area = str(args.get("area") or "").lower()
            res = []
            for l in rows:
                if q_addr and not any(t in str(l.get("address", "")).lower()
                                      for t in q_addr.split() if len(t) > 2):
                    continue
                if q_area and q_area not in str(l.get("area", "")).lower() \
                        and q_area not in str(l.get("city", "")).lower():
                    continue
                try:
                    if args.get("max_price") and l.get("price", 0) > float(args["max_price"]) * 1.10:
                        continue
                    if args.get("min_beds") and (l.get("beds") or 0) < int(args["min_beds"]):
                        continue
                except (TypeError, ValueError):
                    pass
                res.append({
                    "address": l.get("address", ""), "area": l.get("area", ""),
                    "price": int(l.get("price") or 0), "beds": l.get("beds") or "?",
                    "baths": l.get("baths") or "?", "sqft": int(l.get("sqft") or 0),
                    "status": l.get("status", "Active"),
                    "highlights": l.get("public_remarks") or "",
                    "hot": l.get("hot_tag", "") if hot_only else "",
                })
            res = res[:3]
        else:
            res = search(
                area=args.get("area"), max_price=args.get("max_price"),
                min_beds=args.get("min_beds"), address=args.get("address"),
                hot_only=hot_only,
            )[:3]
        if not res:
            return {"result": "No exact matches in Ulises's current featured listings. Tell the caller Ulises has full MLS access and will pull matching homes for them personally."}
        out = []
        for l in res:
            line = (f"{l['address']} ({l['area']}): ${l['price']:,}, {l['beds']} bed / "
                    f"{l['baths']} bath, {l['sqft']:,} sqft, status {l['status']}. {l['highlights']}")
            if l.get("hot"):
                line += f" HOT: {l['hot']}."
            out.append(line)
        return {"result": " | ".join(out)}

    @web.post("/tools/book-showing")
    async def book_showing(req: Request):
        try:
            body = await req.json()
        except Exception:
            body = {}
        args = body.get("args", body) or {}
        call = body.get("call", {}) or {}
        phone = (call.get("metadata") or {}).get("phone") or call.get("from_number") or ""
        name = str(args.get("name") or "").strip() or state.get(f"lead:{phone}", {}).get("name", "Lead")
        start_iso = str(args.get("start_iso") or "").strip()
        purpose = str(args.get("purpose") or "showing").strip()
        prop = str(args.get("property") or "").strip()
        if not start_iso:
            return {"result": "Missing start time — ask the caller for a specific day and time."}
        try:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(TZ)
            s = _settings()
            start = datetime.fromisoformat(start_iso)
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            end = start + timedelta(minutes=s["slot_min"])

            # inside business hours?
            key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][start.weekday()]
            in_hours = False
            for w in s["hours"].get(key, []):
                h1, m1 = map(int, w[0].split(":"))
                h2, m2 = map(int, w[1].split(":"))
                if (start.hour, start.minute) >= (h1, m1) and \
                        (end.hour, end.minute) <= (h2, m2):
                    in_hours = True
                    break
            if not in_hours:
                alts = _open_slots(start, limit=2, demo=_is_demo(phone))
                alt = " or ".join(a.strftime("%I:%M %p").lstrip("0") for a in alts)
                return {"result": f"That time is outside Ulises's hours that day. "
                                  f"{'Offer ' + alt + ' instead.' if alt else 'Ask for a different day.'}"}

            # conflict with his calendar (slot + buffer)?
            demo = _is_demo(phone)
            svc = _cal_svc()
            pad_end = end + timedelta(minutes=s["buffer_min"])
            if any(b0 < pad_end and b1 > start
                   for b0, b1 in _busy_windows(svc, start - timedelta(minutes=s["buffer_min"]),
                                               pad_end, demo)):
                alts = _open_slots(start, limit=2, demo=demo)
                alt = " or ".join(a.strftime("%I:%M %p").lstrip("0") for a in alts)
                return {"result": f"Ulises already has something at that time. "
                                  f"{'Offer ' + alt + ' instead.' if alt else 'Ask for another day.'}"}

            lead = state.get(f"lead:{phone}", {})
            body = _rich_event(name, phone, purpose, prop, lead,
                               "DEMO — booked by Sofia" if demo else "booked by Sofia on a call")
            body["start"] = {"dateTime": start.isoformat(), "timeZone": TZ}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": TZ}
            svc.events().insert(calendarId=_cal_id(demo), body=body).execute()
            _bump_stat("booked")
            bookings = state.get("bookings", [])
            bookings.append({"ts": time.time(), "start": start.isoformat(), "name": name,
                             "phone": phone,
                             "purpose": ("DEMO " + purpose) if demo else purpose,
                             "property": prop})
            state["bookings"] = bookings[-200:]
            _sms_owner(f"📅 ULISES DEMO BOOKED\n{purpose} — {name} {phone}\n{start.strftime('%a %b %d %I:%M %p')} MT\n{prop or ''}\n(tentative — confirm with lead)")
            return {"result": f"Booked tentatively for {start.strftime('%A %B %d at %I:%M %p')}. Tell the caller Ulises will confirm shortly."}
        except Exception as e:
            return {"result": f"Could not book ({str(e)[:80]}). Take their preferred time and tell them Ulises will confirm it personally."}

    @web.post("/tools/check-availability")
    async def check_availability(req: Request):
        """Sofia: open 20-minute slots on a given day (hours ∩ calendar free)."""
        try:
            body = await req.json()
        except Exception:
            body = {}
        args = body.get("args", body) or {}
        call = body.get("call", {}) or {}
        ph = (call.get("metadata") or {}).get("phone") or call.get("from_number") or ""
        dm = _is_demo(ph)
        date_str = str(args.get("date") or "").strip()
        try:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(TZ)
            base = datetime.fromisoformat(date_str).replace(tzinfo=tz) if date_str \
                else datetime.now(tz)
            for d in range(0, 7):
                day = (base + timedelta(days=d)).replace(hour=12, minute=0, second=0, microsecond=0)
                slots = _open_slots(day, limit=3, demo=dm)
                if slots:
                    times = ", ".join(x.strftime("%I:%M %p").lstrip("0") for x in slots)
                    return {"result": f"Open on {slots[0].strftime('%A %B %d')}: {times} "
                                      f"(each is a {_settings()['slot_min']}-minute slot). Offer these."}
                if date_str:
                    return {"result": f"Nothing open on {day.strftime('%A %B %d')} — ask for another day."}
            return {"result": "Nothing open this week — take their preferred time as a message and Ulises will confirm."}
        except Exception as e:
            return {"result": f"Couldn't check availability ({str(e)[:60]}). Take their preferred time and Ulises will confirm."}

    # ── admin panel (token-guarded; page lives at /admin.html on the site) ───
    def _admin_ok(req: Request) -> bool:
        return req.headers.get("x-admin-token") == os.environ.get("CRON_TOKEN")

    @web.get("/admin/overview")
    def admin_overview(req: Request):
        if not _admin_ok(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        from datetime import date
        wk = date.today().isocalendar()
        leads = []
        for ph in (state.get("lead_index", []) or [])[-30:][::-1]:
            l = state.get(f"lead:{ph}", None)
            if l:
                row = {k: l.get(k, "") for k in
                       ("name", "phone", "interest", "lang", "address",
                        "prequalified", "own_rent", "move_date", "ts")}
                row["level"] = _lead_level(l)
                leads.append(row)
        cache = state.get("listings_cache", {}) or {}
        return {
            "week": state.get(f"stats:{wk[0]}-w{wk[1]}", {}),
            "leads": leads,
            "bookings": (state.get("bookings", []) or [])[-20:][::-1],
            "retry_queue": len(state.get("retries", {}) or {}),
            "feed": {"live": bool(cache.get("featured")), "synced_at": cache.get("ts"),
                     "fail_note": state.get("spark_fail_note", "")},
            "sierra": {"configured": bool(os.environ.get("SIERRA_API_KEY")),
                       "fail_note": state.get("sierra_fail_note", "")},
            "settings": _settings(),
        }

    @web.post("/admin/settings")
    async def admin_settings(req: Request):
        if not _admin_ok(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        s = state.get("settings", {}) or {}
        if isinstance(body.get("hours"), dict):
            s["hours"] = {k: v for k, v in body["hours"].items()
                          if k in DEFAULT_SETTINGS["hours"] and isinstance(v, list)}
        for k in ("slot_min", "buffer_min"):
            if body.get(k):
                try:
                    s[k] = max(5, min(120, int(body[k])))
                except (TypeError, ValueError):
                    pass
        if "cal_id" in body:
            s["cal_id"] = str(body["cal_id"]).strip()[:120]
        state["settings"] = s
        return {"ok": True, "settings": _settings()}

    return web


# ── Flexmls daily sync (fired by GitHub Actions: a daily pull + a staggered
# backup check that only re-pulls if the daily one didn't land) ──────────────
SPARK_STALE_AFTER = 26 * 3600   # backup re-pulls past this age
SPARK_ALERT_AFTER = 50 * 3600   # owner gets an SMS past this age (2 misses)


def spark_sync(force: bool):
    import spark_client
    if not spark_client.configured():
        return {"ok": True, "live": False, "note": "SPARK_TOKEN not set — dormant"}

    cache = state.get("listings_cache", {}) or {}
    age = time.time() - cache.get("ts", 0)
    if not force and age < SPARK_STALE_AFTER:
        return {"ok": True, "skipped": "fresh", "age_h": round(age / 3600, 1)}

    try:
        featured = spark_client.my_listings()
        hot_raw = spark_client.hot_sheet()
        hot = []
        for l in hot_raw[:6]:
            tag, tag_es = "Just Listed", "Recién Publicada"
            if l.get("price_change"):
                tag, tag_es = "Price Change", "Cambio de Precio"
            hot.append({**l, "hot_tag": tag, "hot_tag_es": tag_es,
                        "note": l.get("public_remarks", "")[:90],
                        "note_es": ""})
        if not featured and not hot:
            raise RuntimeError("Spark returned no listings")
        state["listings_cache"] = {"ts": time.time(), "featured": featured[:12], "hot": hot}
        state["spark_fail_note"] = ""
        return {"ok": True, "featured": len(featured), "hot": len(hot)}
    except Exception as e:
        err = str(e)[:200]
        state["spark_fail_note"] = err
        # alert only when the feed is genuinely stale (both timers missed)
        if age > SPARK_ALERT_AFTER and cache:
            _sms_owner(f"⚠️ ULISES SITE: Flexmls feed hasn't synced in {int(age/3600)}h. "
                       f"Site is serving the last good pull. Err: {err[:100]}")
        return {"ok": False, "error": err, "age_h": round(age / 3600, 1)}


# ── redial cadence worker (fired by GitHub Actions cron — Modal's 5-schedule
# free-plan cap is fully used by Sofia prod, so no @schedule here) ───────────
def retry_worker():
    if not _within_hours():
        return
    now = time.time()
    retries = state.get("retries", {})
    changed = False
    for phone, plan in list(retries.items()):
        if now < plan.get("next_at", 0):
            continue
        if _blocked(phone):
            retries.pop(phone, None)
            changed = True
            continue
        lead_rec = state.get(f"lead:{phone}", None)
        if not lead_rec:
            retries.pop(phone, None)
            changed = True
            continue
        plan["attempts"] += 1
        plan["next_at"] = float("inf")  # webhook re-schedules on another no-answer
        retries[phone] = plan
        changed = True
        status = _place_call(lead_rec)
        _bump_stat("calls_placed")
        print(f"RETRY attempt {plan['attempts']} -> {phone}: {status}")
    if changed:
        state["retries"] = retries


# ── weekly ROI report (fired by GitHub Actions cron, Mon 9:15am MT) ──────────
def weekly_report():
    from datetime import date, timedelta
    wk = (date.today() - timedelta(days=3)).isocalendar()  # the week that just ended
    s = state.get(f"stats:{wk[0]}-w{wk[1]}", {})
    if not s:
        return
    _sms_owner(
        "📊 SOFIA WEEKLY — Ulises demo\n"
        f"Leads in: {s.get('leads', 0)}\n"
        f"Calls placed: {s.get('calls_placed', 0)}\n"
        f"Connected: {s.get('connected', 0)}\n"
        f"Inbound answered: {s.get('inbound', 0)}\n"
        f"Home-value lookups: {s.get('value_lookups', 0)}\n"
        f"Property/tax questions on calls: {s.get('property_lookups', 0)}\n"
        f"Appointments booked: {s.get('booked', 0)}"
    )
