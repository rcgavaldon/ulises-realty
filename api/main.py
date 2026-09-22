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
import asyncio
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
    .add_local_python_source("listings_data", "property_data", "spark_client", "sierra_client",
                             "flex_share")
)
app = modal.App("ulises-realty-api")
state = modal.Dict.from_name("ulises-realty-state", create_if_missing=True)

TZ = "America/Denver"          # El Paso is Mountain Time
QUIET_START, QUIET_END = 9, 20  # only dial 9:00-19:59 local
RETRY_STEPS_MIN = [5, 30, 60]   # attempt2 +5min, attempt3 +30min, attempt4 +60min
MAX_ATTEMPTS = 4                # 1 initial + 3 redials

NO_ANSWER_REASONS = {
    "dial_no_answer", "dial_busy", "dial_failed", "no_answer",
    "voicemail_reached", "machine_detected",
}

# Missing-email follow-up (Sierra won't take a lead without one). Phone callers
# and old cached forms are the only way a lead arrives without it.
EMAIL_RE = __import__("re").compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
YES_WORDS = {"yes", "y", "yes.", "si", "sí", "correct", "correcto", "yep", "yeah"}
EMAIL_REMIND_AFTER = 20 * 3600   # one reminder, next working morning at the earliest
EMAIL_GIVEUP_AFTER = 72 * 3600   # then stop asking and tell the owner
LEAD_FRESH_DAYS = 90             # older than this, a caller's number may be someone else's now


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


SOFIA_NUMBER = "+19152950548"   # Ulises's Sofia line (9/17): calls go out from it


def _call_from() -> str:
    return SOFIA_NUMBER


def _sms_from() -> str:
    """Texts keep going out from the old 505 (FROM_NUMBER) until the 915's
    texting registration is approved; the owner flips settings.sms_from then."""
    s = state.get("settings", {}) or {}
    return s.get("sms_from") or os.environ["FROM_NUMBER"]


def _our_numbers() -> set:
    return {n for n in (SOFIA_NUMBER, os.environ.get("FROM_NUMBER", "")) if n}


def _sms(to: str, text: str):
    import httpx
    try:
        r = httpx.post(
            "https://api.telnyx.com/v2/messages",
            headers={"Authorization": f"Bearer {os.environ['TELNYX_API_KEY']}"},
            json={"from": _sms_from(), "to": to, "text": text[:1500]},
            timeout=15,
        )
        return r.status_code < 300
    except Exception:
        return False


def _owner_cells() -> list[str]:
    """Where lead alerts go: the agent's cell from settings (set from the
    dashboard), else OWNER_CELL in the secret; plus an optional cc while
    someone else is watching the rollout."""
    s = state.get("settings", {}) or {}
    cells = [s.get("owner_cell") or os.environ["OWNER_CELL"]]
    if s.get("cc_cell") and s["cc_cell"] not in cells:
        cells.append(s["cc_cell"])
    return cells


def _sms_owner(text: str):
    for cell in _owner_cells():
        _sms(cell, text)


def _retell_signed(raw: bytes, sig: str) -> bool:
    """Retell signs every webhook: x-retell-signature = "v=<ms>,d=<hex>" where
    hex = HMAC-SHA256(api_key, raw_body + ms), valid for 5 minutes. Same scheme
    the retell-sdk verifier implements; done inline so an SDK refactor can't
    break it."""
    import hashlib
    import hmac
    import re as _re
    key = os.environ.get("RETELL_API_KEY", "")
    m = _re.search(r"v=(\d+),d=([0-9a-fA-F]+)", sig or "")
    if not key or not m:
        return False
    ts, digest = m.group(1), m.group(2).lower()
    if abs(int(time.time() * 1000) - int(ts)) > 5 * 60 * 1000:
        return False
    try:
        want = hmac.new(key.encode(), (raw.decode("utf-8") + ts).encode(), hashlib.sha256).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(want, digest)


def _alert_once(key: str, text: str, every: int = 6 * 3600, robert: bool = False):
    """Owner alert that can't spam: at most once per `every` seconds per key.
    robert=True: a technical alert for Robert (OWNER_CELL), never the client."""
    last = state.get(f"alert:{key}", 0) or 0
    if time.time() - last > every:
        state[f"alert:{key}"] = time.time()
        if robert:
            _sms(os.environ["OWNER_CELL"], text)
        else:
            _sms_owner(text)


def _retell_confirm(body: dict):
    """An event that failed the signature check: ask Retell itself whether this
    call is real and one of OUR agents'. If so, return Retell's own copy of the
    call (never the posted one, which could be forged). Else None."""
    cid = str((body.get("call") or {}).get("call_id") or "")
    if not cid or body.get("event") not in ("call_ended", "call_analyzed"):
        return None
    try:
        from retell import Retell
        c = Retell(api_key=os.environ["RETELL_API_KEY"]).call.retrieve(cid, timeout=8)
        d = c.model_dump() if hasattr(c, "model_dump") else dict(c)
    except Exception:
        return None
    if d.get("agent_id") not in {os.environ.get("AGENT_EN"), os.environ.get("AGENT_ES")}:
        return None
    return d


def _bump_sig(ok: bool):
    """Count signed vs unsigned Retell events so the first real call after a
    deploy proves the key is right (shown on the admin overview)."""
    s = state.get("retell_sig", {}) or {}
    had_ok = bool(s.get("ok"))
    k = "ok" if ok else "bad"
    s[k] = int(s.get(k, 0)) + 1
    s[f"last_{k}_ts"] = time.time()
    state["retell_sig"] = s
    return had_ok


def _sierra_configured() -> bool:
    try:
        import sierra_client
        return sierra_client.configured()
    except Exception:
        return False


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
            from_number=_call_from(),
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
                "last_call": _last_call_line(lead),
                "booked_for": _booked_line(lead),
                "email_on_file": "yes" if lead.get("email") else "no",
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


MIN_NOTICE_MIN = 30   # never offer or book a slot that starts sooner than this

_CAL_CREDS = None     # one set of credentials per container, so the token is reused


def _cal_svc():
    global _CAL_CREDS
    from googleapiclient.discovery import build
    if _CAL_CREDS is None:
        from google.oauth2 import service_account
        _CAL_CREDS = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GCP_SA_JSON"]),
            scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=_CAL_CREDS)


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
    earliest = datetime.now(tz) + timedelta(minutes=MIN_NOTICE_MIN)
    buf = timedelta(minutes=s["buffer_min"])
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
            # keep `buffer` clear on BOTH sides of anything already on the calendar
            if cur >= earliest and not any(b0 < cur + dur + buf and b1 > cur - buf
                                           for b0, b1 in busy):
                out.append(cur)
                if len(out) >= limit:
                    return out
            cur += step
    return out



# ── lead lifecycle: one record per lead, so the dashboard can answer
# "who did Sofia call, what happened, and what's still pending?" ────────────
STATUS_LABEL = {
    "calling": "Calling now", "no_answer": "No answer", "connected": "Spoke with them",
    "booked": "Booked", "gave_up": "Gave up", "opted_out": "Opted out",
}


def _touch_lead(phone: str, **updates):
    """Merge fields into the stored lead. Safe if the lead doesn't exist."""
    if not phone:
        return
    lead = state.get(f"lead:{phone}", None)
    if lead is None:
        return
    lead.update(updates)
    state[f"lead:{phone}"] = lead


def _log_call(phone: str, **event):
    """Append one call outcome to the lead's history (kept to last 20)."""
    if not phone:
        return
    lead = state.get(f"lead:{phone}", None)
    if lead is None:
        return
    calls = lead.get("calls", [])
    calls.append({"ts": time.time(), **event})
    lead["calls"] = calls[-20:]
    state[f"lead:{phone}"] = lead


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


def _add_to_cal_link(title, start, minutes=20, details=""):
    """Google 'add to calendar' link — works on any phone, unlike attendee
    invites, which a service account can't send without Workspace delegation."""
    from datetime import timedelta
    from urllib.parse import quote_plus
    fmt = "%Y%m%dT%H%M%SZ"
    try:
        s = start.astimezone(__import__("datetime").timezone.utc)
    except Exception:
        return ""
    e = s + timedelta(minutes=minutes)
    return ("https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote_plus(title)}"
            f"&dates={s.strftime(fmt)}/{e.strftime(fmt)}"
            f"&details={quote_plus(details[:300])}")


def _last_call_line(lead) -> str:
    """Gist of the last call, short enough to ride in a prompt variable."""
    s = " ".join(str((lead or {}).get("last_summary") or "").split())
    return s[:400] if s else "none"


def _booked_line(lead) -> str:
    """The appointment Sofia should know about: only one still ahead of us.
    A past booking is history, never a reason to refuse a new one."""
    try:
        from datetime import datetime, timezone
        if datetime.fromisoformat((lead or {}).get("booked_iso") or "") > datetime.now(timezone.utc):
            return lead.get("booked_for") or "none"
    except (ValueError, TypeError):
        pass
    return "none"


_DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _label_es(dt) -> str:
    """'lunes 21 de sep a las 10:00 AM' - ASCII, for the Spanish confirmation text."""
    return f"{_DIAS[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]} a las " \
           f"{dt.strftime('%I:%M %p').lstrip('0')}"


def _rich_event(name, phone, purpose, prop, lead, source):
    """Calendar event body Ulises can act on at a glance — not a dry title."""
    lead = lead or {}
    level = _lead_level(lead)
    flag = {"hot": "🔥 ", "warm": "", "cold": ""}[level]
    want = INTEREST.get(lead.get("interest", ""), {}).get("en", purpose)
    summary = f"{flag}Call: {name} — {want}" if purpose == "consult" \
        else f"{flag}{purpose.title()}: {name}" + (f" @ {prop}" if prop else "")
    rows = [f"Lead level: {level.upper()}", f"Phone: {phone}"]
    if lead.get("email"):
        rows.append(f"Email: {lead['email']}")
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
    secrets=[modal.Secret.from_name("ulises-realty"),
             # Spark/Flexmls lives in its own secret so the main one is never
             # rewritten (and risk losing a key) just to rotate an MLS token.
             modal.Secret.from_name("ulises-spark"),
             # Sierra CRM key + Ulises's agent id: own secret for the same
             # reason. Dormant until it holds SIERRA_API_KEY + SIERRA_AGENT_ID.
             modal.Secret.from_name("ulises-sierra")],
    region="us-east",
)
@modal.asgi_app()
def api():
    from fastapi import BackgroundTasks, FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response

    web = FastAPI()
    web.add_middleware(
        CORSMiddleware,
        allow_origins=["https://rcgavaldon.github.io",
                       "https://www.ulisesortegarealty.com",
                       "https://ulisesortegarealty.com"],
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )

    def norm_phone(raw: str) -> str | None:
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        # a real US number: 10 digits, area code and exchange don't start with 0/1
        if len(digits) == 10 and digits[0] not in "01" and digits[3] not in "01":
            return "+1" + digits
        return None

    def _cron_ok(req: Request) -> bool:
        tok = os.environ.get("CRON_TOKEN") or ""
        return bool(tok) and req.headers.get("x-cron-token") == tok

    @web.get("/health")
    def health():
        return {"ok": True, "app": "ulises-realty-api", "rev": "v19-hotsheet"}

    # GitHub Actions fires these on schedule (Modal free plan's 5 cron slots
    # are taken by Sofia prod). Guarded by CRON_TOKEN.
    @web.post("/cron/retry")
    def cron_retry(req: Request):
        if not _cron_ok(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        # Cloudflare and GitHub both fire this: one tick at a time, or the same
        # due lead could be dialed twice.
        now = time.time()
        try:
            got = state.put("tick_lock", now, skip_if_exists=True)
        except (AttributeError, TypeError):          # plain dict in offline tests
            got = "tick_lock" not in state
            if got:
                state["tick_lock"] = now
        if not got:
            if now - float(state.get("tick_lock") or 0) < 300:
                return {"ok": True, "skipped": "tick running"}
            state["tick_lock"] = now                 # a dead tick's lock: take it over
        try:
            # each job on its own: one failing never skips the others
            for job in (retry_worker, email_followup_worker, _sierra_stuck_scan,
                        _sierra_retry_tick, _sierra_confirm_tick, _listings_tick):
                try:
                    job()
                except Exception as e:
                    print(f"tick {job.__name__} failed: {e}")
            state["last_tick_ts"] = time.time()
        finally:
            try:
                state.pop("tick_lock")
            except Exception:
                pass
        return {"ok": True}

    @web.post("/cron/weekly")
    def cron_weekly(req: Request):
        if not _cron_ok(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        weekly_report()
        return {"ok": True}

    # Daily Flexmls pull. A second backup schedule hits /cron/spark-check,
    # which re-pulls only if the daily one didn't land — belt and suspenders.
    @web.post("/cron/spark-sync")
    def cron_spark_sync(req: Request):
        if not _cron_ok(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return spark_sync(force=True)

    @web.post("/cron/spark-check")
    def cron_spark_check(req: Request):
        if not _cron_ok(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return spark_sync(force=False)

    # Public: the site pulls its listing data from here once the feed is live.
    @web.get("/listings-feed")
    def listings_feed():
        cache = state.get("listings_cache", None)
        if not cache or not (cache.get("featured") or cache.get("hot")):
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

    def _direct_book(name: str, phone: str, start_iso: str, demo: bool = False):
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

    def _sierra_push(lead_rec: dict, adopt: bool = False) -> str:
        """Website/phone lead -> Sierra under Ulises, at most ONCE per lead.
        Never for demo leads. Stores Sierra's leadId only when Sierra confirmed
        the lead landed on Ulises, so a call note can never reach another
        agent's lead. Returns the one-line status for the owner's SMS card
        ("" when Sierra is off). Blocking: call it via asyncio.to_thread."""
        try:
            import sierra_client
        except Exception:
            return ""
        phone = lead_rec.get("phone", "")
        if lead_rec.get("demo") or not sierra_client.configured():
            return ""
        cur = state.get(f"lead:{phone}", {}) or {}
        if cur.get("sierra_lead_id"):
            return sierra_client.describe({"status": "already", "lead_id": cur["sierra_lead_id"]})
        # Atomic claim so two overlapping submits can never both create a lead.
        # The claim expires after 120 s (a crashed push must not block forever).
        claim = f"claim:sierra:{phone}"
        try:
            got_claim = state.put(claim, time.time(), skip_if_exists=True)
        except (AttributeError, TypeError):          # plain dict in offline tests
            got_claim = claim not in state
            if got_claim:
                state[claim] = time.time()
        if not got_claim:
            if time.time() - float(state.get(claim) or 0) < 120:
                return sierra_client.describe({"status": "inflight"})
            state[claim] = time.time()                 # stale claim: take it over
        # a create that timed out (or a push that died) may already exist: adopt it
        adopt = adopt or cur.get("sierra_status") in ("create_unknown", "inflight")
        _ts = time.time()
        state["sierra_push_ts"] = _ts                   # lets the stuck-scan skip idle ticks
        _touch_lead(phone, sierra_status="inflight", sierra_inflight_ts=_ts)
        try:
            res = sierra_client.push_lead(lead_rec, adopt=adopt)
        except Exception as e:
            sierra_client._note_fail(f"push: {str(e)[:120]}")
            res = {"status": "error"}
        upd = {"sierra_status": res.get("status") or "error"}
        if upd["sierra_status"] == "error" and adopt:
            upd["sierra_status"] = "create_unknown"     # it may exist: keep adopting on retry
        if res.get("status") in ("sent", "routing"):
            upd["sierra_lead_id"] = res["lead_id"]
            upd["sierra_created_ts"] = time.time()
            if res["status"] == "routing":
                q = [p for p in (state.get("sierra_confirm", []) or []) if p != phone]
                q.append(phone)
                state["sierra_confirm"] = q[-100:]
        elif res.get("status") in ("error", "create_unknown"):
            # Sierra down / timeout: the cron tick tries again (max 3). After a
            # create that timed out, the retry first looks for the lead we may
            # have created (source Sofia-AI) and adopts it instead of re-posting.
            upd["sierra_attempts"] = int(cur.get("sierra_attempts") or 0) + 1
            q = [p for p in (state.get("sierra_retry", []) or []) if p != phone]
            q.append(phone)
            state["sierra_retry"] = q[-100:]
        elif res.get("status") == "wrong_agent":
            upd["sierra_misrouted_id"] = res.get("lead_id")   # kept for the owner; never used for notes
        _touch_lead(phone, **upd)
        try:
            state.pop(claim)                            # done: a corrected resubmit may push now
        except Exception:
            pass
        return sierra_client.describe(res)

    SIERRA_ROUTE_ALERT_MIN = 15   # still unassigned after this -> owner alert

    def _sierra_confirm(phone: str, force: bool = False):
        """Read the lead back from Sierra (the check that proved lead #5562855):
        on Ulises -> "sent" (call notes allowed); on anyone else -> alert;
        still unassigned after 15 min -> alert. Read-only against Sierra.
        force=True also re-checks an 'unassigned' lead ClearView may have fixed."""
        l = state.get(f"lead:{phone}", None) or {}
        allowed = ("routing", "unassigned") if force else ("routing",)
        if l.get("sierra_status") not in allowed or not l.get("sierra_lead_id"):
            if l.get("sierra_status") != "routing":
                _drop_confirm(phone)
            return
        try:
            import sierra_client
            got = sierra_client.assigned_agent(l["sierra_lead_id"])
        except Exception:
            got = None
        if got is None:
            return                                    # Sierra unreachable: next tick
        aid, who = got
        agent = sierra_client._agent_id()
        lid = l["sierra_lead_id"]
        if aid == agent:
            _touch_lead(phone, sierra_status="sent", sierra_confirmed_ts=time.time())
            _drop_confirm(phone)
        elif aid and aid > 0:
            _touch_lead(phone, sierra_status="wrong_agent", sierra_misrouted_id=lid)
            _drop_confirm(phone)
            sierra_client._note_fail(f"lead {lid} routed to {who or aid}, not Ulises")
            _alert_once(f"sierra_route:{lid}",
                        f"!! SIERRA: lead #{lid} ({l.get('name') or phone}) went to {who or aid}, "
                        "NOT Ulises. Ask ClearView to reassign it and check their Sofia-AI rule.",
                        every=10 * 365 * 86400)
        elif l.get("sierra_status") == "routing" and \
                time.time() - float(l.get("sierra_created_ts") or 0) > SIERRA_ROUTE_ALERT_MIN * 60:
            _touch_lead(phone, sierra_status="unassigned")
            _drop_confirm(phone)
            sierra_client._note_fail(f"lead {lid} still unassigned after {SIERRA_ROUTE_ALERT_MIN} min")
            _alert_once(f"sierra_route:{lid}",
                        f"!! SIERRA: lead #{lid} ({l.get('name') or phone}) is still UNASSIGNED after "
                        f"{SIERRA_ROUTE_ALERT_MIN} min. ClearView's Sofia-AI rule may be off.",
                        every=10 * 365 * 86400)

    def _drop_confirm(phone: str):
        q = state.get("sierra_confirm", []) or []
        if phone in q:
            state["sierra_confirm"] = [p for p in q if p != phone]

    def _sierra_confirm_tick():
        for ph in list(state.get("sierra_confirm", []) or []):
            _sierra_confirm(ph)

    async def _confirm_soon(phone: str):
        """Check the routing 30 s and 2 min after create (the rule took 13 s on
        9/14). The cron tick is the backup if this container goes away first."""
        try:
            for wait in (30, 90):
                await asyncio.sleep(wait)
                await asyncio.to_thread(_sierra_confirm, phone)
                if (state.get(f"lead:{phone}", {}) or {}).get("sierra_status") != "routing":
                    return
        except Exception as e:
            print(f"confirm_soon failed: {e}")

    _BG_TASKS = set()

    def _bg(coro):
        """Fire-and-forget that can't be garbage-collected mid-flight."""
        t = asyncio.create_task(coro)
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)

    async def _finish_lead(lead_rec: dict, card: list):
        """Runs after the visitor already has their answer: the Sierra push, the
        routing check, then the owner card carrying the Sierra line."""
        line = await asyncio.to_thread(_sierra_push, lead_rec)
        _bg(_confirm_soon(lead_rec["phone"]))
        await asyncio.to_thread(_sms_owner, "\n".join(card + ([line] if line else [])))

    def _sierra_retry_tick():
        """Cron: re-push leads whose Sierra create failed (Sierra down,
        timeout). Up to 3 tries per lead, then the owner adds it by hand."""
        q = state.get("sierra_retry", []) or []
        if not q:
            return
        done = set()
        for ph in q:
            l = state.get(f"lead:{ph}", None) or {}
            if not l or l.get("sierra_lead_id") or l.get("demo") or _blocked(ph) \
                    or l.get("sierra_status") not in ("error", "inflight", "create_unknown"):
                done.add(ph)
                continue
            if int(l.get("sierra_attempts") or 0) >= 3:
                done.add(ph)
                _sms_owner(f"⚠️ SIERRA: couldn't add {l.get('name') or 'lead'} {ph} after 3 tries. "
                           "Check Sierra and add them by hand if they're not there.")
                continue
            line = _sierra_push(l, adopt=l.get("sierra_status") in ("create_unknown", "inflight"))
            l2 = state.get(f"lead:{ph}", {}) or {}
            if l2.get("sierra_status") not in ("error", "create_unknown"):
                done.add(ph)
                if line:
                    _sms_owner(f"🔁 SIERRA retry: {l.get('name') or 'lead'} {ph}\n{line}")
        if done:
            cur = state.get("sierra_retry", []) or []
            state["sierra_retry"] = [p for p in cur if p not in done]

    def _sierra_stuck_scan():
        """A push whose container died mid-flight stays 'inflight' forever.
        Hand those to the retry queue (which adopts the lead if it was made)."""
        now = time.time()
        if float(state.get("sierra_scan_ts") or 0) > float(state.get("sierra_push_ts") or 0) + 300:
            return                     # nothing pushed since the last full scan: skip 100 reads
        q = state.get("sierra_retry", []) or []
        add = []
        for ph in (state.get("lead_index", []) or [])[-100:]:
            l = state.get(f"lead:{ph}", None) or {}
            if l.get("sierra_status") == "inflight" and not l.get("sierra_lead_id") \
                    and now - float(l.get("sierra_inflight_ts") or 0) > 300 and ph not in q:
                add.append(ph)
        if add:
            state["sierra_retry"] = (q + add)[-100:]
        state["sierra_scan_ts"] = now

    def _spoken_email(s: str) -> str:
        """'john dot doe at gmail dot com' -> john.doe@gmail.com, else ''."""
        t = (s or "").strip().lower()
        for a, b in ((" at ", "@"), (" dot ", "."), (" underscore ", "_"), (" dash ", "-"), (" ", "")):
            t = t.replace(a, b)
        return t if EMAIL_RE.fullmatch(t) else ""

    def _ask_email(lead: dict, heard: str = ""):
        """No email on file -> one text asking for it. Typed by them, so it's
        right; if Sofia heard one on the call, ask them to confirm it."""
        phone, name = lead["phone"], lead.get("name") or ""
        cand = _spoken_email(heard)
        if lead.get("lang") == "es":
            msg = f"Hola {name}, " if name else "Hola, "
            msg += "soy Sofía, asistente de Ulises Ortega. "
            msg += (f"Tenemos su correo como {cand}. Responda SÍ si es correcto, o envíe el correcto. "
                    if cand else
                    "Responda con su correo electrónico para que Ulises le mande propiedades y le dé seguimiento. ")
            msg += "Responda STOP para no ser contactado."
        else:
            msg = f"Hi {name}, " if name else "Hi, "
            msg += "this is Sofia, Ulises Ortega's assistant. "
            msg += (f"We have your email as {cand}. Reply YES if that's right, or send the correct one. "
                    if cand else
                    "Reply with your email so Ulises can send you listings and follow up. ")
            msg += "Reply STOP to opt out."
        sent = _sms(phone, msg)
        _touch_lead(phone, awaiting_email=True, email_candidate=cand,
                    email_asked_ts=time.time(), email_nudges=0)
        waiting = state.get("awaiting_email", []) or []
        if phone not in waiting:
            waiting.append(phone)
            state["awaiting_email"] = waiting[-200:]
        tag = " (DEMO: nothing goes to Sierra)" if lead.get("demo") else ""
        if sent:
            _sms_owner(f"⏳ MISSING EMAIL: {name or 'caller'} {phone} — texted them for it. "
                       f"Goes to Sierra when they reply.{tag}")
        else:
            _sms_owner(f"⚠️ MISSING EMAIL: {name or 'caller'} {phone} — the text to them FAILED. "
                       f"Get their email by hand.{tag}")

    # ── form submit ──────────────────────────────────────────────────────────
    @web.post("/lead")
    async def lead(req: Request, bg: BackgroundTasks):
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if not body.get("consent"):
            return JSONResponse({"error": "consent required"}, status_code=400)
        name = str(body.get("name", "")).strip()[:80]
        raw_phone = str(body.get("phone", "")).strip()[:40]
        phone = norm_phone(raw_phone)
        if not name or not phone:
            # Tell a human instead of losing a real person over a typo'd number.
            if name or raw_phone:
                _alert_once(f"badlead:{''.join(c for c in raw_phone if c.isdigit())}:{name[:20]}",
                            f"⚠️ ULISES SITE: inquiry with a number Sofia can't dial — "
                            f"{name or '?'} · '{raw_phone}' · {str(body.get('email', ''))[:60]}. "
                            "Reach out by hand.", every=3600)
            return JSONResponse({"error": "name and valid US phone required"}, status_code=400)
        if _blocked(phone):
            # opted out before but came back on their own: a human decides, Sofia contacts nobody
            _alert_once(f"blocked_lead:{phone}",
                        f"{'DEMO - ' if body.get('demo') else ''}\U0001F6AB ULISES SITE: {name} {phone} "
                        f"submitted the form but is opted out ({_blocked(phone)}). Sofia did not call "
                        f"or text. {str(body.get('email', ''))[:60]}", every=86400)
            return JSONResponse({"ok": True, "call": "skipped"})

        lang = "es" if str(body.get("language", "en")).lower().startswith("es") else "en"
        interest_key = str(body.get("interest", "other"))
        address = str(body.get("address", "")).strip()[:160]
        demo = bool(body.get("demo"))
        # Sierra rejects malformed emails; a bad one goes the "no email" route
        # (call still happens, owner told to add by hand) instead of a Sierra 4xx.
        email = str(body.get("email", "")).strip().lower()[:120]
        if not EMAIL_RE.fullmatch(email):
            email = ""

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
            "email": email,
            "address": address,
            "valuation_line": valuation_line,
            "prequalified": str(body.get("prequalified", "")).strip()[:20],
            "own_rent": str(body.get("own_rent", "")).strip()[:20],
            "move_date": str(body.get("move_date", "")).strip()[:20],
            "demo": demo,
            "ts": time.time(),
            "status": "calling", "attempts": 1, "next_at": None, "calls": [],
        }

        # rate limit: 3 calls/phone/hr, 30/day global
        now = time.time()
        rl = state.get("ratelimit", {"per": {}, "day": []})
        rl["per"][phone] = [t for t in rl["per"].get(phone, []) if now - t < 3600]
        rl["day"] = [t for t in rl["day"] if now - t < 86400]
        if len(rl["per"][phone]) >= 3 or len(rl["day"]) >= 30:
            _sms_owner(f"{'🧪 DEMO — ' if demo else ''}⚠️ ULISES SITE: {name} {phone} submitted again "
                       "(limit reached) — no new call placed.")
            return JSONResponse({"ok": True, "call": "rate-limited"})
        # Sofia already dialed this number in the last 10 min (a double-submit or a
        # re-run of the value tool): keep the new info, don't ring them again.
        called_recently = any(now - t < 600 for t in rl["per"][phone])
        rl["per"][phone].append(now)
        rl["day"].append(now)
        state["ratelimit"] = rl

        # A resubmit by the SAME person must not orphan what already happened
        # (the Sierra lead we created, the email they texted us). A different
        # person on this number — a new email, or a record past the freshness
        # window (numbers change hands) — starts clean, so nothing of the old
        # person's can ever be attached to them.
        prev = state.get(f"lead:{phone}", None) or {}
        same_person = bool(prev) \
            and time.time() - float(prev.get("ts") or 0) < LEAD_FRESH_DAYS * 86400 \
            and (not email or not prev.get("email") or email == prev.get("email"))
        if same_person:
            for k in ("sierra_lead_id", "sierra_status", "sierra_inflight_ts", "sierra_misrouted_id",
                      "sierra_created_ts", "sierra_attempts", "email_asked_ts", "awaiting_email",
                      "email_candidate", "email_nudges", "email_nudged", "email_reminded_ts",
                      "email_received_ts", "email_gave_up",
                      # what Sofia knows about them: a resubmit must not wipe it
                      "last_summary", "open_questions", "booked_for", "booked_iso"):
                if k in prev:
                    lead_rec[k] = prev[k]
            if _booked_line(prev) != "none":
                lead_rec["status"] = "booked"          # still booked: the dashboard keeps saying so
            if prev.get("calls"):
                lead_rec["calls"] = prev["calls"]
            if not lead_rec["email"] and prev.get("email"):
                lead_rec["email"] = prev["email"]
        if lead_rec["email"]:
            lead_rec["awaiting_email"] = False   # they just gave it on the form
        if same_person:
            # a push that finished since we read `prev` must not be wiped by this write
            latest = state.get(f"lead:{phone}", None) or {}
            for k in ("sierra_lead_id", "sierra_status", "sierra_created_ts", "sierra_misrouted_id"):
                if latest.get(k):
                    lead_rec[k] = latest[k]
        state[f"lead:{phone}"] = lead_rec
        idx = state.get("lead_index", [])
        if phone in idx:
            idx.remove(phone)
        idx.append(phone)
        state["lead_index"] = idx[-500:]

        # They picked a slot on the site -> book it, no instant call.
        slot_iso = str(body.get("slot_iso", "")).strip()
        if slot_iso:
            label = _direct_book(name, phone, slot_iso, demo=demo)
            if label:
                _bump_stat("leads")
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _Z
                _st = _dt.fromisoformat(slot_iso)
                if _st.tzinfo is None:
                    _st = _st.replace(tzinfo=_Z(TZ))
                _touch_lead(phone, status="booked", attempts=0, next_at=None,
                            booked_for=label, booked_iso=_st.isoformat())
                _link = _add_to_cal_link(
                    "Call with Ulises Ortega", _st, _settings()["slot_min"],
                    "Ulises Ortega, ClearView Realty. Questions? Call or text (915) 295-0548.")
                if lang == "es":
                    _sms(phone, f"Confirmado: Ulises Ortega le llamara el {_label_es(_st)} (hora de El Paso).\n"
                                f"Agregar a su calendario: {_link}\n"
                                "Responda a este mensaje para cambiarla. STOP para no ser contactado.")
                else:
                    _sms(phone, f"Confirmed: Ulises Ortega will call you {label} (El Paso time).\n"
                                f"Add to your calendar: {_link}\n"
                                "Reply here to change it. Reply STOP to opt out.")
                card = [("🧪 DEMO BOOKING (demo calendar)" if demo
                         else "🗓️ ULISES SITE BOOKING (no insta-call)"), name, phone,
                        f"Phone call: {label}", f"Wants: {interest_key} · Lang: {lang.upper()}"]
                bg.add_task(_finish_lead, lead_rec, card)
                return JSONResponse({"ok": True, "scheduled": label})
            # slot vanished -> fall through to the instant call so no lead is lost
        if called_recently:
            call_status = "skipped: already called in the last 10 min"
            _bump_stat("leads")
        else:
            call_status = await asyncio.to_thread(_place_call, lead_rec)
            _bump_stat("leads")
            _bump_stat("calls_placed")
            if not str(call_status).startswith("initiated"):
                # the dial never left, so no webhook will come: queue the redial ourselves
                rt = state.get("retries", {}) or {}
                rt[phone] = {"attempts": 1, "texted": False, "next_at": time.time() + 300}
                state["retries"] = rt
                _touch_lead(phone, status="no_answer", next_at=rt[phone]["next_at"])

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
        bg.add_task(_finish_lead, lead_rec, card)
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
        # No per-entry SMS — 70 buzzes mid-pitch is noise. The deck shows
        # entries on screen live instead; the owner gets one text at the draw.
        return JSONResponse({"ok": True, "count": len(entries)})

    @web.get("/admin/raffle")
    def admin_raffle(req: Request):
        if _role(req) != "owner":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"entries": (state.get("raffle", []) or [])[::-1]}

    # Winner texts stay DISARMED until the owner turns them on, so nothing is
    # ever sent to a room full of strangers without an explicit decision.
    @web.post("/admin/raffle/arm-texts")
    def admin_arm_texts(req: Request):
        if req.headers.get("x-admin-token") != os.environ.get("CRON_TOKEN"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        state["raffle_texts_armed"] = True
        return {"ok": True, "armed": True}

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

        texted = False
        if state.get("raffle_texts_armed") and not state.get("raffle_texted"):
            prizes = ["3 months free", "1 month free"]
            for w, prize in zip(winners, prizes):
                if _blocked(w["phone"]):
                    continue
                _sms(w["phone"],
                     f"You won! {w['name'].split()[0]}, this is Robert with RG Automations — "
                     f"you just won {prize} of your AI assistant, fully set up, at the ClearView "
                     f"drawing. I'll call you today to get you started. Reply STOP to opt out.")
            state["raffle_texted"] = True
            texted = True

        return {"first": winners[0], "second": winners[1], "total": len(entries),
                "texted": texted}

    # ── inbound SMS: honor STOP, forward everything else ─────────────────────
    # Every text we send says "Reply STOP" — this is what makes that true.
    # Point the Telnyx messaging profile's inbound webhook at this URL.
    STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke",
                  "alto", "parar", "cancelar", "baja"}
    START_WORDS = {"start", "unstop", "alta"}

    @web.post("/telnyx-sms")
    async def telnyx_sms(req: Request):
        try:
            body = await req.json()
        except Exception:
            return {"ok": True}
        payload = (body.get("data") or {}).get("payload") or {}
        if payload.get("direction") != "inbound":
            return {"ok": True}
        # No Telnyx signing key is configured, so at least insist the event is a
        # received message addressed to THIS agent's number.
        etype = (body.get("data") or {}).get("event_type")
        if etype and etype != "message.received":
            return {"ok": True}
        tos = [str(t.get("phone_number") or "") for t in (payload.get("to") or []) if isinstance(t, dict)]
        if tos and not (set(tos) & _our_numbers()):
            return {"ok": True}                  # a different number on the same profile
        frm = (payload.get("from") or {}).get("phone_number") or ""
        text = (payload.get("text") or "").strip()
        # Telnyx re-delivers when we answer slower than ~2 s: act on each message once.
        mid = str(payload.get("id") or "")
        if mid:
            seen = state.get("sms_seen", []) or []
            if mid in seen:
                return {"ok": True}
            seen.append(mid)
            state["sms_seen"] = seen[-300:]
        toks = __import__("re").findall(r"[a-z0-9áéíóúñü]+", text.lower())
        word = toks[0] if toks else ""
        # "stop"-type words count anywhere; everyday words like "cancel" or "end"
        # only when they are the whole message ("I need to cancel my showing" is
        # not an opt-out).
        strong = {"stop", "stopall", "unsubscribe", "alto", "baja"}
        weak = STOP_WORDS - strong
        low = text.lower()
        wants_out = any(t in strong for t in toks) or (len(toks) == 1 and word in weak) \
            or "opt out" in low or "optout" in low or "opt-out" in low
        es = (state.get(f"lead:{frm}", {}) or {}).get("lang") == "es"

        if wants_out:
            state[f"optout:{frm}"] = True
            retries = state.get("retries", {})
            if retries.pop(frm, None) is not None:
                state["retries"] = retries
            _touch_lead(frm, awaiting_email=False, status="opted_out", next_at=None)
            waiting = state.get("awaiting_email", []) or []
            if frm in waiting:
                state["awaiting_email"] = [p for p in waiting if p != frm]
            _sms(frm, "Listo, no le volveremos a contactar. Responda START si cambia de opinión."
                      if es else
                      "You're unsubscribed and won't be contacted again. "
                      "Reply START if you ever change your mind.")
            _sms_owner(f"🚫 OPT-OUT honored: {frm}\nThey wrote: {text[:200]}")
            return {"ok": True, "optout": True}

        # Re-subscribe only on an explicit START, never on a stray "yes".
        if len(toks) == 1 and word in START_WORDS and state.get(f"optout:{frm}", False):
            state[f"optout:{frm}"] = False
            _sms(frm, "Listo, está suscrito de nuevo. Responda STOP cuando quiera."
                      if es else "You're re-subscribed. Reply STOP anytime.")
            _sms_owner(f"✅ RE-SUBSCRIBED: {frm}")
            return {"ok": True, "optin": True}

        lead = state.get(f"lead:{frm}", {}) or {}
        who = lead.get("name") or "unknown"
        fresh = bool(lead) and time.time() - float(lead.get("ts") or 0) < LEAD_FRESH_DAYS * 86400

        # An email from a lead we don't have one for -> save it and send them to
        # Sierra, whether we asked (the usual case) or they offered it. A typed
        # address always wins over the one Sofia thought she heard.
        if fresh and not lead.get("email") and not _blocked(frm):
            m = EMAIL_RE.search(text)
            email = m.group(0).lower() if m else ""
            if not email and word in YES_WORDS and lead.get("awaiting_email"):
                email = lead.get("email_candidate") or ""
            if email:
                _touch_lead(frm, email=email, awaiting_email=False, email_received_ts=time.time())
                waiting = [p for p in (state.get("awaiting_email", []) or []) if p != frm]
                state["awaiting_email"] = waiting
                lead = state.get(f"lead:{frm}", {}) or {}
                if lead.get("lang") == "es":
                    _sms(frm, "¡Recibido, gracias! Ulises le dará seguimiento pronto. Responda STOP para no ser contactado.")
                else:
                    _sms(frm, "Got it, thank you! Ulises will follow up shortly. Reply STOP to opt out.")
                line = await asyncio.to_thread(_sierra_push, lead)
                _bg(_confirm_soon(frm))
                if not line:
                    line = "demo: would go to Sierra now" if lead.get("demo") else "Sierra: off"
                _sms_owner(f"✅ EMAIL RECEIVED: {who} {frm} -> {email}\n{line}")
                return {"ok": True, "email": True}
            if lead.get("awaiting_email"):
                if not lead.get("email_nudged"):
                    _touch_lead(frm, email_nudged=True)
                    _sms(frm, ("No encontré un correo en su mensaje — responda solo con su correo electrónico. Responda STOP para no ser contactado."
                               if lead.get("lang") == "es" else
                               "Sorry, I didn't see an email address in that — please reply with just your email. Reply STOP to opt out."))
                _sms_owner(f"💬 TEXT from {who} {frm} (still no email):\n{text[:300]}")
                return {"ok": True}

        _sms_owner(f"💬 TEXT from {who} {frm}:\n{text[:300]}")
        return {"ok": True}

    # ── Retell webhook ───────────────────────────────────────────────────────
    @web.post("/retell-webhook")
    async def retell_webhook(req: Request):
        # This path now writes to the brokerage CRM, so only Retell may drive it.
        raw = await req.body()
        try:
            body = json.loads(raw)
        except Exception:
            return {"ok": True}
        if not isinstance(body, dict) or "event" not in body or "call" not in body:
            return {"ok": True}                  # not Retell-shaped: scanners, typos
        # Only a Retell-signed event may write to the brokerage CRM or text a
        # lead for their email. An unsigned Retell-shaped event still runs the
        # existing call bookkeeping, so a key mix-up can never stop redials.
        trusted = _retell_signed(raw, req.headers.get("x-retell-signature", ""))
        had_ok = _bump_sig(trusted)          # had a signed event arrived BEFORE this one?
        if not trusted:
            # The signature can fail on a key mix-up. Retell itself is the other
            # proof: a call it confirms as ours is trusted, using ITS copy.
            confirmed = await asyncio.to_thread(_retell_confirm, body)
            if confirmed:
                body = {**body, "call": confirmed}
                trusted = True
                _cf = state.get("retell_sig", {}) or {}
                _cf["confirmed"] = int(_cf.get("confirmed", 0)) + 1
                state["retell_sig"] = _cf
        if not trusted:
            print(f"retell-webhook: no valid signature, event={body.get('event')}")
            if had_ok:
                # Retell has already proven it signs with our key, so anything
                # unsigned from here on is not Retell. Drop it.
                return {"ok": True}
            _alert_once("retell_bad_sig",
                        "⚠️ ULISES: a call event failed the Retell check and Retell couldn't "
                        "confirm it. Calls still work; Sierra notes and email texts are paused for it.",
                        robert=True)
        event = body.get("event")
        # Retell retries a slow webhook: act on each (call, event) once.
        _cid = str((body.get("call") or {}).get("call_id") or "")
        if _cid and event in ("call_ended", "call_analyzed"):
            _seen = state.get("retell_seen", []) or []
            _key = f"{_cid}:{event}"
            if _key in _seen:
                return {"ok": True}
            _seen.append(_key)
            state["retell_seen"] = _seen[-300:]
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
                _log_call(phone, outcome="connected", seconds=dur,
                          direction=call.get("direction", "outbound"))
                cur = state.get(f"lead:{phone}", {}).get("status")
                if cur != "booked":
                    _touch_lead(phone, status="connected", next_at=None)
            elif call.get("direction") != "inbound" and phone:
                if _blocked(phone) or (state.get(f"lead:{phone}", None) or {}).get("status") == "booked":
                    # opted out while it rang, or booked meanwhile: no redial, no text
                    cur = state.get("retries", {}) or {}
                    if cur.pop(phone, None) is not None:
                        state["retries"] = cur
                    if _blocked(phone):
                        _touch_lead(phone, status="opted_out", next_at=None)
                    return {"ok": True}
                plan = retries.get(phone, {"attempts": 1, "texted": False})
                lead_rec = state.get(f"lead:{phone}", {"phone": phone, "lang": "en", "name": ""})
                _log_call(phone, outcome="no_answer", seconds=dur, reason=reason,
                          attempt=plan.get("attempts", 1))
                if not plan.get("texted") and not _blocked(phone) and state.get(f"lead:{phone}"):
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
                    cur = state.get("retries", {}) or {}      # re-read: the text above took time
                    cur[phone] = plan
                    state["retries"] = cur
                    _touch_lead(phone, status="no_answer",
                                attempts=plan["attempts"], next_at=nxt)
                else:
                    cur = state.get("retries", {}) or {}
                    cur.pop(phone, None)
                    state["retries"] = cur
                    _touch_lead(phone, status="gave_up", next_at=None,
                                attempts=plan["attempts"])
                    _sms_owner(f"📵 ULISES{' DEMO' if lead_rec.get('demo') else ''}: no answer after {MAX_ATTEMPTS} tries — {lead_rec.get('name','?')} {phone}. Left SMS.")
            return {"ok": True}

        if event == "call_analyzed":
            analysis = call.get("call_analysis", {}) or {}
            custom = analysis.get("custom_analysis_data", {}) or {}
            if str(custom.get("opt_out", "")).lower() in ("true", "yes", "1"):
                state[f"optout:{phone}"] = True
                retries = state.get("retries", {})
                retries.pop(phone, None)
                state["retries"] = retries
                _touch_lead(phone, status="opted_out", next_at=None, awaiting_email=False)
                _sms_owner(f"🚫 ULISES{' DEMO' if _is_demo(phone) else ''}: {phone} asked not to be contacted. Honored.")
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
            _bp = (custom.get("best_phone") or "").strip()
            if _bp and _bp.lower() not in ("same", "none", "unknown", "n/a"):
                lines.append(f"Best number: {_bp}")
            open_q = (custom.get("open_questions") or "").strip()
            if open_q.lower() in ("none", "n/a", "unknown"):
                open_q = ""
            if open_q:
                lines.append(f"They asked: {open_q[:300]}")
            rec = call.get("recording_url")
            if rec:
                lines.append(f"Rec: {rec}")
            _log_call(phone, outcome="summary", seconds=dur,
                      summary=summary[:400], recording=rec or "",
                      fields={k: (custom.get(k) or "") for k in
                              ("areas", "budget", "preapproved", "timeline",
                               "callback_time", "must_haves") if custom.get(k)})
            # Unanswered dials also produce call_analyzed: those get no note, no
            # email ask, and never replace what a real conversation taught us.
            _connected = dur >= 12 and \
                (call.get("disconnection_reason") or "").lower() not in NO_ANSWER_REASONS
            # What the call taught us about someone we didn't have on file.
            _known = state.get(f"lead:{phone}", {}) or {}
            if trusted:          # only Retell-signed details may flow on to the CRM
                _upd = {}
                if _connected:
                    _upd["last_summary"] = "\n".join(
                        x for x in lines[1:] if not x.startswith("Rec: "))[:900]
                    if open_q:
                        _upd["open_questions"] = open_q[:300]
                _cn = (custom.get("caller_name") or "").strip()
                if _cn and _cn.lower() not in ("unknown", "n/a", "none") and not _known.get("name"):
                    _upd["name"] = _cn[:80]
                # a caller we met on the phone: texts go in the language they spoke
                _cl = (custom.get("caller_language") or "").strip().lower()
                if _known.get("source") == "inbound_call" and _cl.startswith(("sp", "es")):
                    _upd["lang"] = "es"
                _it = (custom.get("intent") or "").strip().lower()
                if _it in INTEREST and _it != "other" and _known.get("interest") in (None, "", "other"):
                    _upd["interest"] = _it
                    _upd["interest_desc"] = INTEREST[_it][_known.get("lang", "en")]
                if _known:
                    _touch_lead(phone, **_upd)
                    _known.update(_upd)
            # Call note goes ONLY to the Sierra lead we created for this phone
            # AND that Sierra confirmed is on Ulises (never a search, never a
            # misrouted one), and only while the record is fresh — a phone
            # number can change hands.
            if trusted and _connected and _known.get("sierra_status") in ("routing", "unassigned"):
                await asyncio.to_thread(_sierra_confirm, phone, True)
                _known = state.get(f"lead:{phone}", {}) or _known
            if trusted and _connected and _known.get("sierra_lead_id") \
                    and _known.get("sierra_status") == "sent" and not _known.get("demo") \
                    and time.time() - float(_known.get("sierra_created_ts") or _known.get("ts") or 0) \
                    < LEAD_FRESH_DAYS * 86400:
                lid = _known["sierra_lead_id"]
                try:
                    import sierra_client
                    # ClearView can reassign a lead after we confirmed it: check again
                    # right before writing. Unreachable = no note (fail closed).
                    got = await asyncio.to_thread(sierra_client.assigned_agent, lid)
                    if got and got[0] == sierra_client._agent_id():
                        if await asyncio.to_thread(sierra_client.add_call_note, lid, "\n".join(lines)):
                            lines.append(f"-> note added to Sierra #{lid}")
                    elif got and (got[0] or 0) > 0:
                        _touch_lead(phone, sierra_status="reassigned")
                        lines.append(f"-> Sierra #{lid} is now on {got[1] or got[0]}: no note added")
                except Exception:
                    pass
            # No email on file (they phoned in) -> one text asking for it.
            if trusted and _connected and _known and not _known.get("email") \
                    and not _known.get("email_asked_ts") \
                    and not _blocked(phone):
                _ask_email(_known, custom.get("email_spoken") or "")
                lines.append("-> no email on file: texted them for it")
            _sms_owner("\n".join(lines))
            return {"ok": True}

        return {"ok": True}

    # ── inbound: lead calls the 505 back ─────────────────────────────────────
    @web.api_route("/telnyx-inbound", methods=["GET", "POST"])
    async def telnyx_inbound(req: Request, bg: BackgroundTasks):
        from retell import Retell
        try:
            form = await req.form()
        except Exception:
            form = {}

        def tp(key, default=""):
            v = form.get(key) if hasattr(form, "get") else None
            return v or req.query_params.get(key, default) or default

        from_number = tp("From", "unknown")
        to_number = tp("To", _call_from())
        known = state.get(f"lead:{from_number}", None)
        if known is not None and time.time() - float(known.get("ts") or 0) > LEAD_FRESH_DAYS * 86400:
            known = None   # stale: the number may belong to someone else by now
        # A cold caller becomes a lead the moment Sofia picks up: the call
        # summary, the email text, and the Sierra push all hang off this record.
        # Only a real call hitting OUR number (Telnyx POSTs the TeXML webhook)
        # may create a record; a GET with query params never does.
        real_call = req.method == "POST" and \
            to_number[-10:] in {n[-10:] for n in _our_numbers()}
        if real_call:
            # they called us: a pending redial must not ring them mid-call
            _r = state.get("retries", {}) or {}
            if _r.pop(from_number, None) is not None:
                state["retries"] = _r
        if known is None and real_call and from_number.startswith("+") and not _blocked(from_number):
            known = {
                "phone": from_number, "name": "", "lang": "en",
                "interest": "other", "interest_desc": INTEREST["other"]["en"],
                "message": "none", "email": "", "address": "", "valuation_line": "",
                "prequalified": "", "own_rent": "", "move_date": "",
                # Every call on his line is real, the owner's phones included (Robert, 9/22).
                # Demo mode is only the site's ?demo=1 link now.
                "demo": False,
                "source": "inbound_call", "ts": time.time(),
                "status": "connected", "attempts": 0, "next_at": None, "calls": [],
            }
            state[f"lead:{from_number}"] = known
            idx = [p for p in (state.get("lead_index", []) or []) if p != from_number]
            idx.append(from_number)
            state["lead_index"] = idx[-500:]
        lang = (known or {}).get("lang", "en")
        agent_id = os.environ["AGENT_ES"] if lang == "es" else os.environ["AGENT_EN"]
        if known and known.get("name"):
            begin = (f"¡Hola {known['name']}! Habla Sofía, la asistente virtual de Ulises Ortega — qué bueno que regresó la llamada. ¿En qué le ayudo?"
                     if lang == "es" else
                     f"Hi {known['name']}! This is Sofia, Ulises Ortega's virtual assistant — thanks for calling back. How can I help?")
        else:
            # unknown caller: we don't know their language yet, so greet in both
            begin = ("Thank you for calling Ulises Ortega Real Estate, this is Sofia, his virtual assistant. "
                     "Si prefiere español, con gusto. How can I help you today?")
        try:
            client = Retell(api_key=os.environ["RETELL_API_KEY"])
            call = await asyncio.to_thread(
                client.call.register_phone_call,
                agent_id=agent_id,
                direction="inbound",
                from_number=from_number,
                to_number=to_number,
                retell_llm_dynamic_variables={
                    "name": (known or {}).get("name") or "there",
                    "interest": (known or {}).get("interest_desc", "El Paso real estate"),
                    "message": (known or {}).get("message", "none"),
                    "call_language": ("Spanish" if lang == "es" else "English") if (known or {}).get("name")
                                     else "the caller's language (English or Spanish: switch to Spanish if they speak it)",
                    "call_direction": "inbound",
                    "property": (known or {}).get("address") or "none given",
                    "valuation": (known or {}).get("valuation_line") or "none run",
                    "prequalified": (known or {}).get("prequalified") or "unknown",
                    "own_or_rent": (known or {}).get("own_rent") or "unknown",
                    "move_date": (known or {}).get("move_date") or "unknown",
                    "lead_level": _lead_level(known or {}),
                    "during_hours": "yes" if _during_hours() else "no",
                    "last_call": _last_call_line(known or {}),
                    "booked_for": _booked_line(known),
                    "email_on_file": "yes" if (known or {}).get("email") else "no",
                },
                agent_override={"retell_llm": {"begin_message": begin}},
            )
            bg.add_task(_bump_stat, "inbound")     # not while the caller hears ringback
            twiml = ('<?xml version="1.0" encoding="UTF-8"?>'
                     f'<Response><Dial><Sip>sip:{call.call_id}@sip.retellai.com</Sip></Dial></Response>')
        except Exception as e:
            # Never lose a caller silently.
            _sms_owner(f"⚠️ ULISES: a call from {from_number} could not reach Sofia "
                       f"({str(e)[:60]}). Call them back.")
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
        from property_data import classify
        try:
            body = await req.json()
        except Exception:
            body = {}
        args = body.get("args", body) or {}
        hot_only = str(args.get("hot_only", "")).lower() in ("true", "yes", "1")

        call = body.get("call", {}) or {}
        phone = (call.get("metadata") or {}).get("phone") or call.get("from_number") or ""
        # Real MLS rows only: his own listings, else the hot sheet. The fictional
        # samples are for demo calls ONLY - a real buyer must never hear a made-up home.
        cache = state.get("listings_cache", None) or {}
        rows = (cache.get("hot") or []) if hot_only else (cache.get("featured") or cache.get("hot") or [])
        if rows:
            q_addr = str(args.get("address") or "").lower()
            q_area = str(args.get("area") or "").lower()
            res = []
            for l in rows:
                if q_addr and not any(t in str(l.get("address", "")).lower()
                                      for t in q_addr.split() if len(t) > 2):
                    continue
                # match the subdivision, the city, or the part of town its ZIP is in
                if q_area and q_area not in str(l.get("area", "")).lower() \
                        and q_area not in str(l.get("city", "")).lower() \
                        and q_area not in classify(str(l.get("postal", "")))[0]:
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
                    # never imply the home is Ulises's own listing
                    "highlights": " ".join(x for x in (l.get("public_remarks") or "",
                                                       f"Listed by {l['office']}." if l.get("office") else "") if x),
                    "hot": l.get("hot_tag", "") if hot_only else "",
                })
            res = res[:3]
        elif _is_demo(phone):
            res = search(
                area=args.get("area"), max_price=args.get("max_price"),
                min_beds=args.get("min_beds"), address=args.get("address"),
                hot_only=hot_only,
            )[:3]
        else:
            res = []
        if not res:
            return {"result": "No exact matches in Ulises's current featured listings. Tell the caller Ulises has full MLS access and will pull matching homes for them personally."}
        def _n(v):                                   # 3.0 -> "3"; "?" stays "?"
            return f"{v:g}" if isinstance(v, (int, float)) else str(v)
        out = []
        for l in res:
            line = (f"{l['address']} ({l['area']}): ${l['price']:,}, {_n(l['beds'])} bed / "
                    f"{_n(l['baths'])} bath" + (f", {l['sqft']:,} sqft" if l.get("sqft") else "")
                    + f", status {l['status']}. {l['highlights']}")
            if l.get("hot"):
                line += f" HOT: {l['hot']}."
            out.append(line)
        return {"result": " | ".join(out)}

    @web.post("/tools/book-showing")
    async def book_showing(req: Request, bg: BackgroundTasks):
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
            start = start.astimezone(tz)        # an offset from the model -> El Paso time
            now = datetime.now(tz)
            if start < now + timedelta(minutes=MIN_NOTICE_MIN):
                alts = _open_slots(max(start, now), limit=2, demo=_is_demo(phone))
                alt = " or ".join(a.strftime("%A %I:%M %p").replace(" 0", " ") for a in alts)
                return {"result": f"That time has passed or is too soon. Today is {now:%A %B %d, %Y}. "
                                  + (f"Offer {alt} instead." if alt else "Ask for a later day.")}
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
            if any(b0 < pad_end and b1 > start - timedelta(minutes=s["buffer_min"])
                   for b0, b1 in _busy_windows(svc, start - timedelta(minutes=s["buffer_min"]),
                                               pad_end, demo)):
                alts = _open_slots(start, limit=2, demo=demo)
                alt = " or ".join(a.strftime("%I:%M %p").lstrip("0") for a in alts)
                return {"result": f"Ulises already has something at that time. "
                                  f"{'Offer ' + alt + ' instead.' if alt else 'Ask for another day.'}"}

            lead = state.get(f"lead:{phone}", {}) or {}
            # no tool can move an event: a second booking says what it replaces
            prev = _booked_line(lead)
            resched = f"RESCHEDULED - replaces {prev}. Delete that event." if prev != "none" else ""
            body = _rich_event(name, phone, purpose, prop, lead,
                               "DEMO — booked by Sofia" if demo else "booked by Sofia on a call")
            if resched:
                body["description"] = resched + "\n" + body["description"]
            body["start"] = {"dateTime": start.isoformat(), "timeZone": TZ}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": TZ}
            svc.events().insert(calendarId=_cal_id(demo), body=body).execute()
            label = start.strftime("%a %b %d %I:%M %p")

            def _after():
                # Sofia already has her answer. The owner text goes first, so a
                # bookkeeping error can never eat it.
                kind = {"consult": "PHONE CONSULT", "valuation": "HOME VALUATION",
                        "showing": "SHOWING (IN PERSON)"}.get(purpose, purpose.upper())
                _sms_owner(f"📅 {'DEMO — ' if demo else ''}TENTATIVE {kind}\n"
                           f"{name} {phone}\n"
                           f"{label} MT\n"
                           + (f"{prop}\n" if prop else "")
                           + (f"{resched}\n" if resched else "")
                           + "Not confirmed yet — call them to lock it in.")
                _touch_lead(phone, status="booked", next_at=None, booked_for=label,
                            booked_iso=start.isoformat())
                _bump_stat("booked")
                bookings = state.get("bookings", [])
                bookings.append({"ts": time.time(), "start": start.isoformat(), "name": name,
                                 "phone": phone,
                                 "purpose": ("DEMO " + purpose) if demo else purpose,
                                 "property": prop})
                state["bookings"] = bookings[-200:]

            bg.add_task(_after)
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
            today = datetime.now(tz)
            if date_str and base.date() < today.date():
                return {"result": f"That date has passed. Today is {today:%A %B %d, %Y}. "
                                  "Ask which upcoming day works."}
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

    # ── dashboard auth ───────────────────────────────────────────────────────
    # Two levels: the OWNER token (also the cron key — never hand this out) and
    # a per-CLIENT token, so each agent gets their own long link that shows
    # their pipeline and lets them set their hours, but nothing operational.
    def _role(req: Request):
        tok = req.headers.get("x-admin-token") or ""
        if tok and tok == os.environ.get("CRON_TOKEN"):
            return "owner"
        if tok and tok == (state.get("settings", {}) or {}).get("client_token"):
            return "client"
        return None

    def _admin_ok(req: Request) -> bool:
        return _role(req) is not None

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
                        "prequalified", "own_rent", "move_date", "ts",
                        "status", "attempts", "next_at", "booked_for", "demo",
                        "email", "source", "sierra_status", "sierra_lead_id", "awaiting_email",
                        "open_questions")}
                row["level"] = _lead_level(l)
                row["calls"] = (l.get("calls") or [])[-6:][::-1]
                row["status_label"] = STATUS_LABEL.get(l.get("status", ""), "New")
                leads.append(row)
        cache = state.get("listings_cache", {}) or {}
        pending = [l for l in leads if l.get("status") in ("calling", "no_answer")]
        spoke_unbooked = [l for l in leads if l.get("status") == "connected"]
        return {
            "pipeline": {"awaiting_callback": len(pending),
                         "spoke_not_booked": len(spoke_unbooked),
                         "booked": len([l for l in leads if l.get("status") == "booked"])},
            "week": state.get(f"stats:{wk[0]}-w{wk[1]}", {}),
            "leads": leads,
            "bookings": (state.get("bookings", []) or [])[-20:][::-1],
            "retry_queue": len(state.get("retries", {}) or {}),
            "feed": {"live": bool(cache.get("featured")), "synced_at": cache.get("ts"),
                     "fail_note": state.get("spark_fail_note", "")},
            "retell_signatures": state.get("retell_sig", {}) or {},
            "awaiting_email": len(state.get("awaiting_email", []) or []),
            "sierra_confirming": len(state.get("sierra_confirm", []) or []),
            "last_tick_ts": state.get("last_tick_ts"),
            "numbers": {"calls_from": _call_from(), "texts_from": _sms_from()},
            "sierra": {"configured": _sierra_configured(),
                       "fail_note": state.get("sierra_fail_note", "")},
            "role": _role(req),
            "settings": {k: v for k, v in _settings().items()
                         if _role(req) == "owner" or k not in ("cal_id", "client_token")},
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
        if "cal_id" in body and _role(req) == "owner":
            s["cal_id"] = str(body["cal_id"]).strip()[:120]
        # Which of our numbers texts go out from (flip to the 915 once its
        # texting registration is approved). Owner-only, and only our numbers.
        if "sms_from" in body and _role(req) == "owner":
            v = norm_phone(str(body["sms_from"] or "")) if body["sms_from"] else ""
            if not v or v in _our_numbers():
                s["sms_from"] = v or ""
        # Who gets the lead texts. Owner-only: this points automated texts at a person.
        for k in ("owner_cell", "cc_cell"):
            if k in body and _role(req) == "owner":
                v = str(body[k] or "").strip()
                s[k] = (norm_phone(v) or "") if v else ""
        state["settings"] = s
        return {"ok": True, "settings": _settings()}

    @web.post("/admin/reset-lead")
    async def admin_reset_lead(req: Request):
        """Owner only: forget one phone so a test can run from scratch.
        Touches only our own records, never Sierra."""
        if _role(req) != "owner":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await req.json()
        except Exception:
            body = {}
        ph = norm_phone(str(body.get("phone", "")))
        if not ph:
            return JSONResponse({"error": "phone required"}, status_code=400)
        try:
            del state[f"lead:{ph}"]
        except KeyError:
            pass
        for key in ("lead_index", "awaiting_email", "sierra_retry", "sierra_confirm"):
            lst = state.get(key, []) or []
            if ph in lst:
                state[key] = [p for p in lst if p != ph]
        r = state.get("retries", {}) or {}
        if r.pop(ph, None) is not None:
            state["retries"] = r
        return {"ok": True, "reset": ph}

    return web


# ── Flexmls daily sync (fired by GitHub Actions: a daily pull + a staggered
# backup check that only re-pulls if the daily one didn't land) ──────────────
SPARK_STALE_AFTER = 26 * 3600   # backup re-pulls past this age
SPARK_ALERT_AFTER = 50 * 3600   # owner gets an SMS past this age (2 misses)


HOTSHEET_REFRESH = 6 * 3600     # the 5-min tick re-pulls the shared hot sheet past this age


def spark_sync(force: bool, stale_after: int = SPARK_STALE_AFTER):
    """Listing pull. Hot sheet = Ulises's public Flexmls shared link (no key
    needed, always current). Featured = his own listings, once Spark can see
    them. Either failing keeps the last good pull on the site."""
    import flex_share
    import spark_client

    cache = state.get("listings_cache", {}) or {}
    age = time.time() - cache.get("ts", 0)
    if not force and age < stale_after:
        return {"ok": True, "skipped": "fresh", "age_h": round(age / 3600, 1)}

    try:
        featured = []
        if spark_client.configured():
            try:
                featured = spark_client.my_listings()
            except Exception:
                featured = []
        hot = flex_share.fetch()
        hot_raw = [] if hot or not spark_client.configured() else spark_client.hot_sheet()
        for l in hot_raw[:6]:
            tag, tag_es = "Just Listed", "Recién Publicada"
            if l.get("price_change"):
                tag, tag_es = "Price Change", "Cambio de Precio"
            hot.append({**l, "hot_tag": tag, "hot_tag_es": tag_es,
                        "note": l.get("public_remarks", "")[:90],
                        "note_es": ""})
        if not featured and not hot:
            raise RuntimeError("no listings from the shared hot sheet or Spark")
        state["listings_cache"] = {"ts": time.time(), "featured": featured[:12], "hot": hot[:40]}
        state["spark_fail_note"] = ""
        return {"ok": True, "featured": len(featured), "hot": len(hot)}
    except Exception as e:
        err = str(e)[:200]
        state["spark_fail_note"] = err
        # alert only when the feed is genuinely stale (both timers missed)
        if age > SPARK_ALERT_AFTER and cache:
            _alert_once("listings_stale",
                        f"⚠️ ULISES SITE: listing feed hasn't synced in {int(age/3600)}h. "
                        f"Site is serving the last good pull. Err: {err[:100]}",
                        every=24 * 3600, robert=True)
        return {"ok": False, "error": err, "age_h": round(age / 3600, 1)}


def _listings_tick():
    """5-min tick: keep the hot sheet fresh (re-pull past HOTSHEET_REFRESH)."""
    spark_sync(False, stale_after=HOTSHEET_REFRESH)


# ── redial cadence worker (fired by GitHub Actions cron — Modal's 5-schedule
# free-plan cap is fully used by Sofia prod, so no @schedule here) ───────────
def retry_worker():
    if not _within_hours():
        return
    now = time.time()

    def save(phone, plan):
        # re-read and merge per phone, so a webhook reschedule written while we
        # were dialing someone else is never overwritten by an old snapshot
        cur = state.get("retries", {}) or {}
        if plan is None:
            cur.pop(phone, None)
        else:
            cur[phone] = plan
        state["retries"] = cur

    for phone, plan in list((state.get("retries", {}) or {}).items()):
        # Watchdog: a dial with no result after 15 min (no webhook came back)
        # counts as a no-answer instead of waiting forever.
        if plan.get("next_at") == float("inf") and now - float(plan.get("dialed_ts") or 0) > 900:
            if plan.get("attempts", 1) >= MAX_ATTEMPTS:
                save(phone, None)
                _touch_lead(phone, status="gave_up", next_at=None)
                _sms_owner(f"\U0001F4F5 ULISES{' DEMO' if _is_demo(phone) else ''}: no result from "
                           f"{phone} after {MAX_ATTEMPTS} tries. Follow up by hand.")
                continue
            step = RETRY_STEPS_MIN[min(plan.get("attempts", 1) - 1, len(RETRY_STEPS_MIN) - 1)]
            plan["next_at"] = now + step * 60
            save(phone, plan)
            continue
        if now < plan.get("next_at", 0):
            continue
        # the loop runs on a snapshot: a callback, a STOP or a booking may have
        # changed this plan since. Act only on what is due right now.
        live = (state.get("retries", {}) or {}).get(phone)
        if not live or now < float(live.get("next_at") or 0):
            continue
        plan = live
        lead_rec = state.get(f"lead:{phone}", None)
        if _blocked(phone) or not lead_rec or lead_rec.get("status") == "booked":
            save(phone, None)          # opted out, gone, or already booked: never dial
            continue
        plan["attempts"] = int(plan.get("attempts", 1)) + 1
        plan["next_at"] = float("inf")  # webhook re-schedules on another no-answer
        plan["dialed_ts"] = now
        save(phone, plan)
        status = _place_call(lead_rec)
        _log_call(phone, outcome="dialing", attempt=plan["attempts"])
        print(f"RETRY attempt {plan['attempts']} -> {phone}: {status}")
        if not str(status).startswith("initiated"):
            # the dial itself failed: no webhook will ever reschedule it
            if plan["attempts"] >= MAX_ATTEMPTS:
                save(phone, None)
                _touch_lead(phone, status="gave_up", next_at=None, attempts=plan["attempts"])
                _sms_owner(f"📵 ULISES: couldn't reach {lead_rec.get('name') or phone} {phone} "
                           f"after {plan['attempts']} tries (last dial failed). Follow up by hand.")
            else:
                plan["next_at"] = now + 30 * 60
                save(phone, plan)
                _touch_lead(phone, status="no_answer", attempts=plan["attempts"], next_at=plan["next_at"])
            continue
        _touch_lead(phone, status="calling", attempts=plan["attempts"])
        _bump_stat("calls_placed")


def email_followup_worker():
    """5-minute tick: one reminder to anyone who never sent their email, then
    give up and hand it to the owner. Working hours only, never after STOP."""
    if not _within_hours():
        return
    now = time.time()
    waiting = state.get("awaiting_email", []) or []
    keep = []
    for phone in waiting:
        lead = state.get(f"lead:{phone}", None)
        if not lead or not lead.get("awaiting_email") or _blocked(phone):
            continue
        asked = lead.get("email_asked_ts") or now
        nudges = int(lead.get("email_nudges") or 0)
        if nudges == 0 and now - asked >= EMAIL_REMIND_AFTER:
            _sms(phone, ("Hola, soy Sofía, asistente de Ulises Ortega. ¿Me comparte su correo electrónico para que Ulises le dé seguimiento? Responda STOP para no ser contactado."
                         if lead.get("lang") == "es" else
                         "Hi, it's Sofia, Ulises Ortega's assistant. Could you reply with your email so Ulises can follow up? Reply STOP to opt out."))
            _touch_lead(phone, email_nudges=1, email_reminded_ts=now)
            keep.append(phone)
        elif nudges >= 1 and now - asked >= EMAIL_GIVEUP_AFTER:
            _touch_lead(phone, awaiting_email=False, email_gave_up=True)
            _sms_owner(f"📭 NO EMAIL after 2 texts: {lead.get('name') or 'caller'} {phone}. "
                       "Not in Sierra — add by hand if you want them there.")
        else:
            keep.append(phone)
    dropped = set(waiting) - set(keep)
    if dropped:
        cur = state.get("awaiting_email", []) or []      # re-read: _ask_email may have added one
        state["awaiting_email"] = [p for p in cur if p not in dropped]


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
