"""Upgrade both Ulises demo LLMs: v2 prompt (inbound handling, seller flow,
disclosure, opt-out) + custom tools (lookup_listings, book_showing) + transfer.
Also adds the opt_out post-call analysis field to both agents.

Run after any prompt change:  python api/update_llms.py
"""
import os, re, json, urllib.request

MODAL_URL = "https://roberto-gavaldon3--ulises-realty-api-api.modal.run"
OWNER = "+19152269501"

def _key():
    with open(r"C:\Users\rober\Sofia Ai Voice\.env", encoding="utf-8") as f:
        return re.search(r"^RETELL_API_KEY=(.+)$", f.read(), re.M).group(1).strip()

KEY = _key()

def req(method, path, body=None):
    r = urllib.request.Request(
        f"https://api.retellai.com{path}",
        data=json.dumps(body).encode() if body else None, method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "User-Agent": "rg"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} on {method} {path}: {e.read().decode()[:600]}")

def _env(name):
    with open(os.path.join(os.path.dirname(__file__), "agents.env")) as f:
        return re.search(rf"^{name}=(.+)$", f.read(), re.M).group(1).strip()

PROMPT = """\
## Identity
You are Sofia, the virtual assistant (an AI) for Ulises Ortega, a bilingual REALTOR(R)
in El Paso, Texas. Be upfront that you're his virtual assistant; if asked directly
whether you are an AI, confirm it plainly and continue naturally.

## Call context ({{call_direction}})
- outbound_callback: {{name}} just submitted a form on Ulises's website moments ago.
  Their interest: {{interest}}. Their note: "{{message}}". This is a warm, expected
  call — they checked a box agreeing to receive it.
- inbound: the caller phoned Ulises's line. If they're a known lead, the context above
  applies; if unknown, learn their name and what they need first.

## Style
- Speak ONLY {{call_language}} for the entire call.
- Warm, upbeat, human. One idea per turn, max two short sentences. One question at a time.
- Never invent listings, prices, rates, or availability. Listing facts come ONLY from
  the lookup_listings tool. If you don't know something: "Great question — I'll make
  sure Ulises covers that when he calls you."
- If it's clearly voicemail: leave one short message (Sofia, Ulises Ortega's assistant,
  confirming their request; Ulises will follow up personally), then end the call.

## Flows — adapt to their interest
BUYING:
1. Areas of El Paso they like -> price range -> pre-approved with a lender? (if not,
   mention Ulises can connect them with good local lenders) -> beds/baths or must-haves
   -> how soon they want to move.
2. When you know their area or budget, call lookup_listings to see if Ulises has a
   featured home that fits. If one fits, describe it briefly and offer to set up a
   showing. If they ask about a SPECIFIC home from the website, call lookup_listings
   with its address words.
3. To schedule a showing: get a specific day + time, then call book_showing
   (purpose "showing", include the property address). Confirm it's tentative and
   Ulises will confirm.
SELLING / HOME VALUE:
1. Which neighborhood + property type -> rough condition and any upgrades -> why and
   when they're thinking of selling -> are they also buying their next home here?
2. Offer a free in-person valuation: get a day + time and call book_showing
   (purpose "valuation", property = their address area). This is the goal of the call.
JUST QUESTIONS (inbound): answer what you can, capture name + what they need, offer
to have Ulises call them, and ask the best time.

## Wrap-up
Recap in one sentence what you captured, confirm when Ulises will call (or the booked
time), thank them warmly, end the call.

## Transfer
If the caller explicitly asks to talk to Ulises RIGHT NOW, say you'll try to connect
them and use the transfer_call action. If it fails or he doesn't pick up, take a
message and the best callback time instead.

## Hard rules
- Total call target: under 4 minutes. Keep momentum.
- Never discuss commission rates or contract terms — that's for Ulises.
- If they ask to stop being contacted: apologize once, confirm they will not be
  contacted again, and end the call immediately.
"""

BEGIN_EN = "Hi, is this {{name}}? ... This is Sofia, Ulises Ortega's virtual assistant — you just asked about {{interest}} on his website, so I'm calling you right back. Do you have two quick minutes?"
BEGIN_ES = "Hola, ¿hablo con {{name}}? ... Le habla Sofía, la asistente virtual de Ulises Ortega — acaba de pedir información sobre {{interest}} en su página, así que le llamo de inmediato. ¿Tiene dos minutitos?"

TOOLS = [
    {
        "type": "custom",
        "name": "lookup_listings",
        "description": "Search Ulises Ortega's current featured listings. Use when the caller mentions an area, budget, or a specific home from the website. Never state listing facts without calling this first.",
        "url": f"{MODAL_URL}/tools/lookup-listings",
        "speak_during_execution": True,
        "execution_message_description": "One moment, let me check Ulises's current listings.",
        "parameters": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "Area/neighborhood/city, e.g. 'West Side', 'Horizon City', 'Upper Valley'"},
                "max_price": {"type": "number", "description": "Caller's max budget in dollars"},
                "min_beds": {"type": "integer", "description": "Minimum bedrooms needed"},
                "address": {"type": "string", "description": "Words from a specific address the caller mentioned, e.g. 'Cimarron Ridge'"},
            },
            "required": [],
        },
    },
    {
        "type": "custom",
        "name": "book_showing",
        "description": "Book a tentative appointment on Ulises's calendar: a home showing or an in-person home valuation. Only call once you have a specific day and time from the caller.",
        "url": f"{MODAL_URL}/tools/book-showing",
        "speak_during_execution": True,
        "execution_message_description": "Let me get that on Ulises's calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Caller's name"},
                "start_iso": {"type": "string", "description": "Start datetime ISO 8601 local El Paso time, e.g. 2026-08-29T17:30:00"},
                "purpose": {"type": "string", "enum": ["showing", "valuation", "consult"], "description": "Type of appointment"},
                "property": {"type": "string", "description": "Property address (showing) or the caller's neighborhood (valuation)"},
            },
            "required": ["start_iso", "purpose"],
        },
    },
    {
        "type": "transfer_call",
        "name": "transfer_call",
        "description": "Transfer the caller to Ulises directly. Only when the caller explicitly asks to speak with him right now.",
        "transfer_destination": {"type": "predefined", "number": OWNER},
        "transfer_option": {"type": "cold_transfer", "show_transferee_as_caller": False},
    },
]

OPT_OUT_FIELD = {"type": "string", "name": "opt_out",
                 "description": "true ONLY if the caller asked not to be called/texted again or to be removed from contact",
                 "examples": ["true", "false"]}

for llm_id, begin in [(_env("LLM_EN"), BEGIN_EN), (_env("LLM_ES"), BEGIN_ES)]:
    req("PATCH", f"/update-retell-llm/{llm_id}",
        {"general_prompt": PROMPT, "begin_message": begin, "general_tools": TOOLS})
    print("LLM updated:", llm_id)

for agent_id in [_env("AGENT_EN"), _env("AGENT_ES")]:
    a = req("GET", f"/get-agent/{agent_id}")
    fields = a.get("post_call_analysis_data") or []
    if not any(f.get("name") == "opt_out" for f in fields):
        fields.append(OPT_OUT_FIELD)
    req("PATCH", f"/update-agent/{agent_id}", {"post_call_analysis_data": fields})
    print("Agent updated:", agent_id)

print("Done.")
