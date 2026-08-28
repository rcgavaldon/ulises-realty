"""Ulises Realty demo API — isolated Modal app (fully separate from Sofia prod).

POST /lead            form submit -> instant Retell outbound call + SMS lead card to owner
POST /retell-webhook  call_ended (no-answer -> SMS lead + schedule redial) and
                      call_analyzed (SMS summary to owner, honor opt-out)
GET|POST /telnyx-inbound  lead calls the 505 back -> Sofia answers (TeXML -> Retell SIP)
POST /tools/lookup-listings  Retell tool: Sofia searches Ulises's listings mid-call
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
    .add_local_python_source("listings_data")
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


INTEREST = {
    "buy":   {"en": "buying a home",         "es": "comprar casa"},
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
        return {"ok": True, "app": "ulises-realty-api"}

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
        lead_rec = {
            "phone": phone, "name": name, "lang": lang,
            "interest": interest_key,
            "interest_desc": INTEREST.get(interest_key, INTEREST["other"])[lang],
            "message": str(body.get("message", "")).strip()[:300] or ("ninguno" if lang == "es" else "none"),
            "email": str(body.get("email", "")).strip()[:120],
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
        call_status = _place_call(lead_rec)
        _bump_stat("leads")
        _bump_stat("calls_placed")

        _sms_owner(
            f"🏠 ULISES DEMO LEAD\n{name}\n{phone}\n"
            f"Wants: {interest_key} · Lang: {lang.upper()}\n"
            f"Note: {lead_rec['message'][:120]}\n"
            f"Sofia call: {call_status[:80]}"
        )
        return JSONResponse({"ok": True, "call": call_status})

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

    # ── Retell custom tools ──────────────────────────────────────────────────
    @web.post("/tools/lookup-listings")
    async def lookup_listings(req: Request):
        from listings_data import search
        try:
            body = await req.json()
        except Exception:
            body = {}
        args = body.get("args", body) or {}
        res = search(
            area=args.get("area"), max_price=args.get("max_price"),
            min_beds=args.get("min_beds"), address=args.get("address"),
        )[:3]
        if not res:
            return {"result": "No exact matches in Ulises's current featured listings. Tell the caller Ulises has full MLS access and will pull matching homes for them personally."}
        out = [
            f"{l['address']} ({l['area']}): ${l['price']:,}, {l['beds']} bed / {l['baths']} bath, "
            f"{l['sqft']:,} sqft, status {l['status']}. {l['highlights']}"
            for l in res
        ]
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
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            info = json.loads(os.environ["GCP_SA_JSON"])
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/calendar"])
            svc = build("calendar", "v3", credentials=creds)
            start = datetime.fromisoformat(start_iso)
            end = start + timedelta(minutes=45)
            title = f"[TENTATIVE] {purpose.title()} — {name}"
            if prop:
                title += f" @ {prop}"
            svc.events().insert(calendarId=os.environ["CAL_ID"], body={
                "summary": title,
                "description": f"Booked by Sofia (AI). Lead: {name} {phone}. Purpose: {purpose}. Property: {prop or 'n/a'}.",
                "start": {"dateTime": start.isoformat(), "timeZone": TZ},
                "end": {"dateTime": end.isoformat(), "timeZone": TZ},
            }).execute()
            _bump_stat("booked")
            _sms_owner(f"📅 ULISES DEMO BOOKED\n{purpose} — {name} {phone}\n{start.strftime('%a %b %d %I:%M %p')} MT\n{prop or ''}\n(tentative — confirm with lead)")
            return {"result": f"Booked tentatively for {start.strftime('%A %B %d at %I:%M %p')}. Tell the caller Ulises will confirm shortly."}
        except Exception as e:
            return {"result": f"Could not book ({str(e)[:80]}). Take their preferred time and tell them Ulises will confirm it personally."}

    return web


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
        f"Appointments booked: {s.get('booked', 0)}"
    )
