"""Ulises Realty demo API — isolated Modal app (fully separate from Sofia prod).

POST /lead           form submit -> instant Retell outbound call + SMS lead card to owner
POST /retell-webhook Retell call_analyzed -> SMS call summary to owner
GET  /health

Deploy:  modal deploy api/main.py
Secret:  ulises-realty (RETELL_API_KEY, TELNYX_API_KEY, AGENT_EN, AGENT_ES,
         FROM_NUMBER, OWNER_CELL)
"""
import os
import time

import modal

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "fastapi[standard]==0.115.*", "httpx", "retell-sdk"
)
app = modal.App("ulises-realty-api")

# naive in-container abuse guards (public endpoint that dials phones)
_seen_phone: dict = {}   # phone -> [timestamps]
_total_calls: list = []  # timestamps


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("ulises-realty")],
    region="us-east",
)
@modal.asgi_app()
def api():
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from retell import Retell

    web = FastAPI()
    web.add_middleware(
        CORSMiddleware,
        allow_origins=["https://rcgavaldon.github.io"],
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )

    FROM = os.environ["FROM_NUMBER"]
    OWNER = os.environ["OWNER_CELL"]

    def sms_owner(text: str):
        try:
            httpx.post(
                "https://api.telnyx.com/v2/messages",
                headers={"Authorization": f"Bearer {os.environ['TELNYX_API_KEY']}"},
                json={"from": FROM, "to": OWNER, "text": text[:1500]},
                timeout=15,
            )
        except Exception:
            pass

    def norm_phone(raw: str) -> str | None:
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return None

    INTEREST = {
        "buy":   {"en": "buying a home",             "es": "comprar casa"},
        "sell":  {"en": "selling their home",        "es": "vender su casa"},
        "value": {"en": "a free home valuation",     "es": "un avaluo gratis de su casa"},
        "other": {"en": "El Paso real estate",       "es": "bienes raices en El Paso"},
    }

    @web.get("/health")
    def health():
        return {"ok": True, "app": "ulises-realty-api"}

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

        lang = "es" if str(body.get("language", "en")).lower().startswith("es") else "en"
        interest_key = str(body.get("interest", "other"))
        interest = INTEREST.get(interest_key, INTEREST["other"])[lang]
        message = str(body.get("message", "")).strip()[:300] or ("ninguno" if lang == "es" else "none")
        email = str(body.get("email", "")).strip()[:120]

        # rate limits: 3 calls/phone/hour, 30 calls/day total
        now = time.time()
        _seen_phone.setdefault(phone, [])
        _seen_phone[phone] = [t for t in _seen_phone[phone] if now - t < 3600]
        _total_calls[:] = [t for t in _total_calls if now - t < 86400]
        if len(_seen_phone[phone]) >= 3 or len(_total_calls) >= 30:
            sms_owner(f"⚠️ ULISES DEMO: rate-limited lead {name} {phone}")
            return JSONResponse({"ok": True, "call": "rate-limited"})
        _seen_phone[phone].append(now)
        _total_calls.append(now)

        agent_id = os.environ["AGENT_ES"] if lang == "es" else os.environ["AGENT_EN"]
        call_status = "initiated"
        try:
            client = Retell(api_key=os.environ["RETELL_API_KEY"])
            client.call.create_phone_call(
                from_number=FROM,
                to_number=phone,
                override_agent_id=agent_id,
                retell_llm_dynamic_variables={
                    "name": name,
                    "interest": interest,
                    "message": message,
                    "call_language": "Spanish" if lang == "es" else "English",
                },
                metadata={"source": "ulises-realty", "email": email, "interest": interest_key},
            )
        except Exception as e:
            call_status = f"failed: {e}"

        sms_owner(
            f"🏠 ULISES DEMO LEAD\n{name}\n{phone}\n"
            f"Wants: {interest_key} · Lang: {lang.upper()}\n"
            f"Note: {message[:120]}\n"
            f"Sofia call: {call_status[:80]}"
        )
        return JSONResponse({"ok": True, "call": call_status})

    @web.post("/retell-webhook")
    async def retell_webhook(req: Request):
        try:
            body = await req.json()
        except Exception:
            return {"ok": True}
        if body.get("event") != "call_analyzed":
            return {"ok": True}
        call = body.get("call", {}) or {}
        if (call.get("metadata") or {}).get("source") != "ulises-realty":
            return {"ok": True}
        analysis = call.get("call_analysis", {}) or {}
        custom = analysis.get("custom_analysis_data", {}) or {}
        to_num = call.get("to_number", "?")
        dur = int((call.get("duration_ms") or 0) / 1000)
        lines = [f"📋 SOFIA CALL DONE ({dur}s) — {to_num}"]
        for k in ("areas", "budget", "preapproved", "timeline", "callback_time", "must_haves"):
            v = (custom.get(k) or "").strip()
            if v and v.lower() not in ("unknown", "n/a", "none"):
                lines.append(f"{k}: {v}")
        summary = (analysis.get("call_summary") or "").strip()
        if summary:
            lines.append(f"Summary: {summary[:400]}")
        sms_owner("\n".join(lines))
        return {"ok": True}

    return web
