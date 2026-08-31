"""Sierra Interactive CRM bridge — pushes our leads into the agent's Sierra
account so his brokerage CRM stays the system of record.

STATUS: wired but DORMANT until SIERRA_API_KEY is set in the Modal secret.
Every call is fail-safe: a Sierra outage or schema mismatch can never break
the lead flow — we record the failure in state["sierra_fail_note"] (shown on
the admin panel) and move on.

Key comes from the agent's Sierra account: Gear icon → Integrations →
Sierra Interactive → copy API key. Note Sierra says API access may depend on
the subscription plan — if the key isn't there, the account admin (probably
the brokerage) has to enable it or email support@sierrainteractive.com.

Confirmed from Sierra's public docs: base https://api.sierrainteractivedev.com,
auth header `Sierra-ApiKey`, POST /leads registers a lead, GET /leads/find
searches. ⚠️ Exact BODY FIELD NAMES below are written from integration-doc
conventions and are UNVERIFIED until the first real key — run one test push,
read the error body, and correct in minutes (same drill as spark_client).
"""
import json
import os

import modal

BASE = "https://api.sierrainteractivedev.com"
TIMEOUT = 20

_state = modal.Dict.from_name("ulises-realty-state", create_if_missing=True)


def configured() -> bool:
    return bool(os.environ.get("SIERRA_API_KEY"))


def _headers():
    return {
        "Sierra-ApiKey": os.environ["SIERRA_API_KEY"],
        "Content-Type": "application/json",
        # lets Sierra label where these leads came from
        "Sierra-OriginatingSystemName": "RG-Automations-Sofia",
    }


def _note_fail(err: str):
    _state["sierra_fail_note"] = err[:200]


def push_lead(lead: dict) -> bool:
    """Form submit -> Sierra lead. Returns True when Sierra accepted it."""
    if not configured():
        return False
    import httpx
    name = (lead.get("name") or "").strip()
    first, _, last = name.partition(" ")
    note_bits = [f"Wants: {lead.get('interest', '?')}"]
    for label, key in (("Pre-qualified", "prequalified"), ("Currently", "own_rent"),
                       ("Move date", "move_date"), ("Property", "address")):
        if lead.get(key):
            note_bits.append(f"{label}: {lead[key]}")
    if lead.get("valuation_line"):
        note_bits.append(f"Value tool: {lead['valuation_line']}")
    if lead.get("message"):
        note_bits.append(f"Note: {lead['message']}")
    body = {
        # ⚠️ UNVERIFIED field names — confirm against the live API on first push.
        "firstName": first or name or "Website Lead",
        "lastName": last or "",
        "phone": lead.get("phone", ""),
        "email": lead.get("email", ""),
        "source": "Sofia AI — ulises-realty site",
        "note": " | ".join(note_bits)[:900],
        "tags": ["Sofia-AI", f"lang-{lead.get('lang', 'en')}"],
    }
    if os.environ.get("SIERRA_AGENT_ID"):
        body["assignTo"] = os.environ["SIERRA_AGENT_ID"]
    try:
        r = httpx.post(f"{BASE}/leads", headers=_headers(), json=body, timeout=TIMEOUT)
        if r.status_code < 300:
            _state["sierra_fail_note"] = ""
            return True
        _note_fail(f"HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        _note_fail(str(e))
    return False


def add_call_note(phone: str, note: str) -> bool:
    """Post-call summary -> a note on the Sierra lead matching this phone."""
    if not configured() or not phone:
        return False
    import httpx
    try:
        r = httpx.get(f"{BASE}/leads/find", headers=_headers(),
                      params={"phone": phone[-10:]}, timeout=TIMEOUT)
        if r.status_code >= 300:
            _note_fail(f"find HTTP {r.status_code}")
            return False
        data = r.json()
        items = (data.get("data") or {}).get("items") or data.get("items") or []
        if not items:
            return False
        lead_id = items[0].get("id") or items[0].get("leadId")
        if not lead_id:
            return False
        # ⚠️ UNVERIFIED path — some accounts use /leads/{id}/notes
        r = httpx.post(f"{BASE}/leads/{lead_id}/note", headers=_headers(),
                       json={"message": note[:900]}, timeout=TIMEOUT)
        if r.status_code < 300:
            return True
        _note_fail(f"note HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        _note_fail(str(e))
    return False
