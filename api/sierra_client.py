"""Sierra Interactive CRM bridge — one job: a website lead becomes a Sierra
lead under THIS agent. Nothing else.

Scope, on purpose (ClearView's key is account-wide, so we police ourselves):
  * CREATE only. Never update, reassign, or delete anything.
  * Every lead is sent in EXACTLY the format proven live on 9/14 (source
    "Sofia-AI", no assignTo). ClearView's routing rule puts it on Ulises and
    we read the lead back to confirm; if it is anywhere else, the owner is
    alerted instead of us hoping.
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


def _existing(email: str):
    """The existing Sierra lead with EXACTLY this email (as a dict), else None.
    Read-only, and the same call as the proven 9/14 script: GET /leads/find with
    only email + pageSize. Sierra's search is a partial match ("ortega@gmail"
    would hit julio.ortega@gmail.com), so each hit is compared exactly before it
    counts. Returns "error" if Sierra couldn't be asked."""
    import httpx
    want = email.lower()
    try:
        r = httpx.get(f"{BASE}/leads/find", headers=_headers(),
                      params={"email": email, "pageSize": 25}, timeout=TIMEOUT)
    except Exception as e:
        _note_fail(f"find: {str(e)[:120]}")
        return "error"
    body = _json_or_none(r)
    if body is None:
        _note_fail(f"find HTTP {r.status_code}: {r.text[:120]}")
        return "error"
    for hit in (body.get("data") or {}).get("leads") or []:
        if isinstance(hit, dict) and (hit.get("email") or "").strip().lower() == want:
            return hit
    return None


def push_lead(lead: dict, adopt: bool = False) -> dict:
    """Website lead -> Sierra lead under our agent.

    Returns {"status": one of
        "sent"        (+ lead_id)          created and confirmed on our agent
        "routing"     (+ lead_id)          created unassigned; ClearView's rule assigns it
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

    found = _existing(email)
    if found == "error":
        return {"status": "error"}
    if found is not None:
        fid = found.get("id") or -1
        # After a create that timed out, the lead may be ours: same email AND
        # our source. Adopt it (then confirm routing) instead of re-posting.
        src = (found.get("source") or "").lower().replace("-", "").replace(" ", "")
        if adopt and src == SOURCE.lower().replace("-", "") and fid != -1:
            return {"status": "routing", "lead_id": fid, "adopted": True}
        return {"status": "exists", "lead_id": fid}

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

    # EXACTLY the fields of the lead that was proven live on 9/14 (#5562855):
    # no assignTo, no shortSummary, 10-digit phone, leadType 1, tags [Sofia-AI].
    # ClearView's routing rule (source Sofia-AI -> Ulises) assigns it; we then
    # read it back to confirm, the same check that proved it by hand.
    body = {
        "firstName": first or name or ("Phone" if by_phone else "Website"),
        "lastName": last or "Lead",
        "email": email,
        "phone": _digits(phone),
        "password": secrets.token_urlsafe(12),   # Sierra requires one (lead's site login)
        "sendRegistrationEmail": False,           # no surprise welcome email
        "leadType": 1,
        "source": SOURCE,
        "tags": [SOURCE],
        "note": (("Sofia AI - called in on Ulises's line. " if by_phone else "Sofia AI - website inquiry. ")
                 + " | ".join(bits))[:900],
    }

    try:
        r = httpx.post(f"{BASE}/leads", headers=_headers(), json=body, timeout=15)
    except Exception as e:
        # A timeout doesn't mean it failed: Sierra may have created it. The
        # retry looks for it first and adopts it rather than creating twice.
        _note_fail(f"create (unknown result): {str(e)[:100]}")
        return {"status": "create_unknown"}
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
    _state["sierra_fail_note"] = ""
    if got == _agent_id():
        return {"status": "sent", "lead_id": lead_id}
    if isinstance(got, int) and got > 0:
        who = f"{data.get('agentUserFirstName', '')} {data.get('agentUserLastName', '')}".strip() or str(got)
        _note_fail(f"lead {lead_id} landed on {who}, not agent {_agent_id()}")
        return {"status": "wrong_agent", "lead_id": lead_id, "agent": who}
    # -1 = unassigned at creation: ClearView's routing rule assigns it next
    return {"status": "routing", "lead_id": lead_id}


def assigned_agent(lead_id):
    """Read-only: who a lead is assigned to now. Returns (agentUserId, name),
    or None if Sierra couldn't be asked."""
    if not configured() or not lead_id:
        return None
    import httpx
    try:
        r = httpx.get(f"{BASE}/leads/get/{int(lead_id)}", headers=_headers(), timeout=TIMEOUT)
    except Exception as e:
        _note_fail(f"get: {str(e)[:120]}")
        return None
    body = _json_or_none(r)
    if body is None:
        _note_fail(f"get HTTP {r.status_code}: {r.text[:120]}")
        return None
    a = (body.get("data") or {}).get("assignedTo") or {}
    aid = a.get("agentUserId")
    name = f"{a.get('agentUserFirstName', '')} {a.get('agentUserLastName', '')}".strip()
    return (aid if isinstance(aid, int) else -1, name)


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
    if s == "routing":
        return f"-> Sierra #{result.get('lead_id')} (ClearView routing to Ulises)"
    if s == "exists":
        return "Sierra: already in CRM, not re-added"
    if s == "already":
        return f"-> Sierra #{result.get('lead_id')} (already there)"
    if s == "inflight":
        return "Sierra: already being added"
    if s == "no_email":
        return "Sierra: no email given, add by hand"
    if s == "error":
        return "Sierra: failed, retrying"
    if s == "create_unknown":
        return "Sierra: slow reply, re-checking it saved"
    return ""
