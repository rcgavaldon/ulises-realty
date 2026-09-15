"""Sierra Interactive CRM bridge — one job: a website lead becomes a Sierra
lead under THIS agent. Nothing else.

Scope, on purpose (ClearView's key is account-wide, so we police ourselves):
  * CREATE only. Never update, reassign, or delete anything.
  * Every lead is created with assignTo={agentUserId: SIERRA_AGENT_ID} AND
    source "Sofia-AI" (ClearView's routing rule keys on that source) — two
    independent ways it lands on the right agent. If Sierra still reports a
    different agent, we flag it loudly instead of hoping.
  * If the email or phone already exists in Sierra we do NOT touch that lead —
    we report "already in Sierra" and let humans decide. (Sierra keys leads on
    email; re-posting an existing email is undocumented behaviour.)
  * Call notes go ONLY to leads we created (by the leadId Sierra handed back),
    never by phone search — a phone search could match another agent's lead.
  * Demo leads never reach Sierra (main.py guards; we double-check `demo`).
  * Sierra REQUIRES email + password on create. No email -> not sent, reported.

DORMANT until SIERRA_API_KEY and SIERRA_AGENT_ID are in the Modal secret.
Fail-safe: a Sierra outage can never break the lead flow — failures land in
state["sierra_fail_note"] (admin panel) and we move on.

Verified 2026-09-14 against api.sierrainteractivedev.com docs + a live key:
  POST /leads (needs email+password; assignTo is an OBJECT), GET /leads/find
  (email/phone params, data.leads[]), POST /leads/{id}/note ({message,
  shouldNotify}). Cloudflare 403/1010s requests with no User-Agent.
"""
import os
import re
import secrets

import modal

BASE = "https://api.sierrainteractivedev.com"
TIMEOUT = 6                    # x3 sequential calls worst case; runs off the event loop
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SOURCE = "Sofia-AI"            # ClearView's routing rule matches this (case-insensitive)
UA = "RG-Automations-Sofia/1.0 (+https://rcgavaldon.github.io/ulises-realty/)"

_state = modal.Dict.from_name("ulises-realty-state", create_if_missing=True)


def configured() -> bool:
    return bool(os.environ.get("SIERRA_API_KEY")) and bool(_agent_id())


def _agent_id():
    try:
        return int(os.environ.get("SIERRA_AGENT_ID", "") or 0) or None
    except ValueError:
        return None


def _headers():
    return {
        "Sierra-ApiKey": os.environ["SIERRA_API_KEY"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
        "Sierra-OriginatingSystemName": "RG-Automations-Sofia",
    }


def _note_fail(err: str):
    _state["sierra_fail_note"] = err[:200]


def _json_or_none(r):
    """Sierra's reply as a dict, else None (non-2xx, non-JSON, or not an object)."""
    if r.status_code >= 300:
        return None
    try:
        body = r.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())[-10:]


def _existing(email: str, phone: str):
    """leadId of an existing Sierra lead with EXACTLY this email or phone,
    else None. Read-only. Sierra's search is a partial match ("ortega@gmail"
    would hit julio.ortega@gmail.com), so every hit is compared field for
    field before it counts. Any status except deleted counts (an archived lead
    is still someone's lead). Returns "error" if Sierra couldn't be asked."""
    import httpx
    want_email, want_phone = email.lower(), _digits(phone)
    for params in ({"email": email} if email else None, {"phone": phone} if phone else None):
        if not params:
            continue
        params["leadStatus"] = "AllExceptDeleted"
        params["pageSize"] = 25
        try:
            r = httpx.get(f"{BASE}/leads/find", headers=_headers(), params=params, timeout=TIMEOUT)
        except Exception as e:
            _note_fail(f"find: {str(e)[:120]}")
            return "error"
        body = _json_or_none(r)
        if body is None:
            _note_fail(f"find HTTP {r.status_code}: {r.text[:120]}")
            return "error"
        for hit in (body.get("data") or {}).get("leads") or []:
            if not isinstance(hit, dict):
                continue
            if want_email and (hit.get("email") or "").strip().lower() == want_email:
                return hit.get("id") or -1
            if want_phone and _digits(hit.get("phone")) == want_phone:
                return hit.get("id") or -1
    return None


def _lead_type(interest: str) -> int:
    k = (interest or "").lower()
    if "buysell" in k or ("buy" in k and "sell" in k):
        return 3
    if "sell" in k or "value" in k:
        return 2
    return 1


def push_lead(lead: dict) -> dict:
    """Website lead -> Sierra lead under our agent.

    Returns {"status": one of
        "sent"        (+ lead_id)          created and confirmed on our agent
        "wrong_agent" (+ lead_id, agent)   created but Sierra put it elsewhere
        "exists"      (+ lead_id)          already in Sierra, NOT touched
        "no_email"                         Sierra requires email; not sent
        "off" | "demo" | "error"           not sent
    }"""
    if not configured():
        return {"status": "off"}
    if lead.get("demo"):
        return {"status": "demo"}
    email = (lead.get("email") or "").strip().lower()
    phone = (lead.get("phone") or "").strip()
    if not _EMAIL_RE.fullmatch(email):        # Sierra requires a valid one
        return {"status": "no_email"}

    found = _existing(email, phone)
    if found == "error":
        return {"status": "error"}
    if found is not None:
        return {"status": "exists", "lead_id": found}

    import httpx
    name = (lead.get("name") or "").strip()
    first, _, last = name.partition(" ")
    bits = [f"Wants: {lead.get('interest_desc') or lead.get('interest') or '?'}"]
    for label, key in (("Pre-qualified", "prequalified"), ("Currently", "own_rent"),
                       ("Move date", "move_date"), ("Property", "address")):
        if lead.get(key):
            bits.append(f"{label}: {lead[key]}")
    if lead.get("valuation_line"):
        bits.append(f"Value tool: {lead['valuation_line']}")
    msg = (lead.get("message") or "").strip()
    if msg and msg.lower() not in ("none", "ninguno"):
        bits.append(f"Note: {msg}")
    lang = "Spanish" if (lead.get("lang") or "en") == "es" else "English"
    bits.append(f"Language: {lang}")
    if lead.get("last_summary"):
        bits.append("Call: " + " ".join(str(lead["last_summary"]).split())[:400])
    by_phone = lead.get("source") == "inbound_call"

    body = {
        "firstName": first or name or ("Phone" if by_phone else "Website"),
        "lastName": last or "Lead",
        "email": email,
        "phone": phone,
        "password": secrets.token_urlsafe(12),   # Sierra requires one (lead's site login)
        "sendRegistrationEmail": False,           # no surprise welcome email
        "leadType": _lead_type(lead.get("interest", "")),
        "source": SOURCE,
        "tags": [SOURCE, f"lang-{(lead.get('lang') or 'en')}"],
        "shortSummary": bits[0][:100],
        "note": (("Sofia AI - called in on Ulises's line. " if by_phone else "Sofia AI - website inquiry. ")
                 + " | ".join(bits))[:900],
        "assignTo": {"agentUserId": _agent_id()},
    }
    if os.environ.get("SIERRA_AGENT_EMAIL"):
        body["assignTo"]["agentUserEmail"] = os.environ["SIERRA_AGENT_EMAIL"]

    try:
        r = httpx.post(f"{BASE}/leads", headers=_headers(), json=body, timeout=TIMEOUT)
    except Exception as e:
        _note_fail(f"create: {str(e)[:120]}")
        return {"status": "error"}
    res = _json_or_none(r)
    if res is None:
        _note_fail(f"create HTTP {r.status_code}: {r.text[:150]}")
        return {"status": "error"}
    if not res.get("success"):
        _note_fail(f"create refused: {str(res)[:150]}")
        return {"status": "error"}
    data = res.get("data") or {}
    lead_id = data.get("leadId")
    if not lead_id:
        _note_fail("create: success but no leadId in reply")
        return {"status": "error"}
    got = data.get("agentUserId")
    if got != _agent_id():
        who = f"{data.get('agentUserFirstName', '')} {data.get('agentUserLastName', '')}".strip() or str(got)
        _note_fail(f"lead {lead_id} landed on {who}, not agent {_agent_id()}")
        return {"status": "wrong_agent", "lead_id": lead_id, "agent": who}
    _state["sierra_fail_note"] = ""
    return {"status": "sent", "lead_id": lead_id}


def add_call_note(lead_id, note: str) -> bool:
    """Post-call summary -> a note on a lead WE created (by Sierra leadId only)."""
    if not configured() or not lead_id:
        return False
    import httpx
    try:
        r = httpx.post(f"{BASE}/leads/{int(lead_id)}/note", headers=_headers(),
                       json={"message": note[:900], "shouldNotify": True}, timeout=TIMEOUT)
        res = _json_or_none(r)
        if res is not None and res.get("success", True):
            return True
        _note_fail(f"note HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        _note_fail(f"note: {str(e)[:120]}")
    return False


def describe(result: dict) -> str:
    """One line for the owner's lead-card SMS."""
    s = result.get("status")
    if s == "sent":
        return f"-> Sierra #{result.get('lead_id')} (assigned to Ulises)"
    if s == "wrong_agent":
        return f"!! Sierra #{result.get('lead_id')} went to {result.get('agent')} - NOT Ulises, fix in Sierra"
    if s == "exists":
        return "Sierra: already in CRM, not re-added"
    if s == "already":
        return f"-> Sierra #{result.get('lead_id')} (already there)"
    if s == "inflight":
        return "Sierra: already being added"
    if s == "no_email":
        return "Sierra: no email given, add by hand"
    if s == "error":
        return "Sierra: failed, see dashboard"
    return ""
