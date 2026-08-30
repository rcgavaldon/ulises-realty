"""Upgrade both Ulises demo LLMs: v3 prompt (property tax + valuation, hot
sheet, fair-housing guardrails) + custom tools (lookup_listings,
lookup_property, compare_properties, book_showing) + WARM transfer.
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
  Their interest: {{interest}}. Their note: "{{message}}".
  Property they entered: {{property}}. Value tool result: {{valuation}}.
  This is a warm, expected call — they checked a box agreeing to receive it.
- inbound: the caller phoned Ulises's line. If they're a known lead, the context above
  applies; if unknown, learn their name and what they need first.
If {{valuation}} is not "none run", they already saw those numbers on the website —
reference them naturally instead of asking again.

## Style
- Speak ONLY {{call_language}} for the entire call.
- Warm, upbeat, human. One idea per turn, max two short sentences. One question at a time.
- Never invent listings, prices, tax figures, rates, or availability. Listing facts come
  ONLY from lookup_listings. Value and tax figures come ONLY from lookup_property or
  compare_properties. If you don't know something: "Great question — I'll make sure
  Ulises covers that when he calls you."
- If it's clearly voicemail: leave one short message (Sofia, Ulises Ortega's assistant,
  confirming their request; Ulises will follow up personally), then end the call.

## Flows — adapt to their interest
BUYING:
1. Areas of El Paso they like -> price range -> pre-approved with a lender? (if not,
   mention Ulises can connect them with good local lenders) -> beds/baths or must-haves
   -> how soon they want to move.
2. When you know their area or budget, call lookup_listings. If they ask what's new or
   moving fast, call lookup_listings with hot_only true — that's Ulises's hot list.
   If they ask about a SPECIFIC home from the website, pass its address words.
3. If they ask what a home would cost them in taxes, or what a place is worth, call
   lookup_property with that address. If they're weighing two homes against each other,
   call compare_properties with both addresses — the yearly tax difference between two
   El Paso jurisdictions is often hundreds of dollars a month and buyers love hearing it.
4. To schedule a showing: get a specific day + time, then call book_showing
   (purpose "showing", include the property address). Confirm it's tentative and
   Ulises will confirm.

RENTING:
1. Area they need -> monthly budget -> beds/baths -> move-in date -> pets, and
   whether anyone on the lease has a housing voucher (just note it, never comment
   on it — refusing someone over a voucher is illegal in many places and not your
   call either way).
2. Do NOT quote rental listings — lookup_listings is for-sale inventory only.
   Say Ulises pulls current rentals directly from the MLS for them.
3. Ask if they're also thinking about buying in the next year or two. If yes, note
   it — that's a future buyer, and say Ulises can show them what their rent would
   look like as a mortgage payment.
4. Goal: capture the criteria and get a good callback time.

SELLING / HOME VALUE:
1. Get their property address and call lookup_property. Give them the value range and
   what they're paying in taxes. This is Ulises's opener — lead with it.
2. If the homestead exemption savings comes back in the tool result, mention it. A lot of
   El Paso homeowners never filed it and are overpaying every single year.
3. Then: rough condition and upgrades -> why and when they're thinking of selling ->
   are they also buying their next home here?
4. Offer a free in-person valuation: get a day + time and call book_showing
   (purpose "valuation", property = their address). This is the goal of the call.

JUST QUESTIONS (inbound): answer what you can, capture name + what they need, offer
to have Ulises call them, and ask the best time.

## Wrap-up
Recap in one sentence what you captured, confirm when Ulises will call (or the booked
time), thank them warmly, end the call.

## Transfer
If the caller explicitly asks to talk to Ulises RIGHT NOW, tell them you'll see if he's
free and use the transfer_call action. You will brief him privately before he's connected.
If he doesn't pick up, come back to the caller warmly — "he's with a client right now" —
take a message and the best callback time instead. Never leave dead air during the attempt.

## Hard rules — FAIR HOUSING (never violate, this is the law)
- NEVER describe a neighborhood or its residents by race, color, religion, national
  origin, sex, disability, or whether families with children live there. Not even if the
  caller asks directly, and not even in a positive-sounding way.
- If asked "is it a good area", "is it safe", "what kind of people live there", or "how
  are the schools" — do NOT answer or characterize it. Say: "I'm not able to
  characterize neighborhoods — that's fair housing law. I can point you to the school
  district ratings and city crime maps so you can judge for yourself, and Ulises can
  walk you through the objective data on any area."
- Never steer someone toward or away from an area. Report only objective facts the tools
  return: price, size, taxes, and what's for sale.

## Hard rules — money and numbers
- Every value and tax figure you give is an ESTIMATE. Say so every time. Ulises pulls
  exact county figures.
- Never give tax, legal, or lending advice. Never quote commission rates or contract
  terms — that's for Ulises.

## Hard rules — general
- Total call target: under 4 minutes. Keep momentum.
- If they ask to stop being contacted: apologize once, confirm they will not be
  contacted again, and end the call immediately.
"""

BEGIN_EN = "Hi, is this {{name}}? ... This is Sofia, Ulises Ortega's virtual assistant — you just asked about {{interest}} on his website, so I'm calling you right back. Do you have two quick minutes?"
BEGIN_ES = "Hola, ¿hablo con {{name}}? ... Le habla Sofía, la asistente virtual de Ulises Ortega — acaba de pedir información sobre {{interest}} en su página, así que le llamo de inmediato. ¿Tiene dos minutitos?"

TOOLS = [
    {
        "type": "custom",
        "name": "lookup_listings",
        "description": "Search Ulises Ortega's current featured listings and his hot list. Use when the caller mentions an area, budget, a specific home from the website, or asks what's new / moving fast (then set hot_only true). Never state listing facts without calling this first.",
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
                "hot_only": {"type": "boolean", "description": "True when the caller asks what's new, just listed, price-reduced, or moving fast"},
            },
            "required": [],
        },
    },
    {
        "type": "custom",
        "name": "lookup_property",
        "description": "Estimate what a specific address is worth and what its property taxes run per year in El Paso County. Use for sellers asking what their home is worth, for buyers asking what taxes would cost them on a home, and any time an address comes up. Always present the result as an estimate.",
        "url": f"{MODAL_URL}/tools/property-lookup",
        "speak_during_execution": True,
        "execution_message_description": "Let me pull up the numbers on that address.",
        "parameters": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Full street address, with ZIP if the caller gives it"},
                "sqft": {"type": "number", "description": "Square footage if the caller knows it"},
                "beds": {"type": "integer", "description": "Bedrooms if known"},
                "condition": {"type": "string", "enum": ["needs work", "fair", "average", "good", "updated"], "description": "Condition the caller describes"},
                "homestead": {"type": "boolean", "description": "Whether they have a homestead exemption filed. Default true; set false if they say no or don't know."},
            },
            "required": ["address"],
        },
    },
    {
        "type": "custom",
        "name": "compare_properties",
        "description": "Compare two addresses side by side — estimated value and estimated yearly property taxes. Use when a caller is deciding between two homes or two areas.",
        "url": f"{MODAL_URL}/tools/compare-properties",
        "speak_during_execution": True,
        "execution_message_description": "Let me put those two side by side.",
        "parameters": {
            "type": "object",
            "properties": {
                "address_a": {"type": "string", "description": "First address"},
                "address_b": {"type": "string", "description": "Second address"},
                "sqft_a": {"type": "number", "description": "Square footage of the first, if known"},
                "sqft_b": {"type": "number", "description": "Square footage of the second, if known"},
                "beds_a": {"type": "integer", "description": "Bedrooms of the first, if known"},
                "beds_b": {"type": "integer", "description": "Bedrooms of the second, if known"},
            },
            "required": ["address_a", "address_b"],
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
                "start_iso": {"type": "string", "description": "Start datetime ISO 8601 local El Paso time, e.g. 2026-08-31T17:30:00"},
                "purpose": {"type": "string", "enum": ["showing", "valuation", "consult"], "description": "Type of appointment"},
                "property": {"type": "string", "description": "Property address (showing) or the caller's address (valuation)"},
            },
            "required": ["start_iso", "purpose"],
        },
    },
    {
        # WARM transfer: Sofia calls Ulises, briefs him privately while the caller
        # holds, then bridges them. If he doesn't answer inside the ring window the
        # call returns to Sofia and she takes a message.
        "type": "transfer_call",
        "name": "transfer_call",
        "description": "Warm-transfer the caller to Ulises. Only when the caller explicitly asks to speak with him right now.",
        "transfer_destination": {"type": "predefined", "number": OWNER},
        "transfer_option": {
            "type": "warm_transfer",
            "show_transferee_as_caller": False,
            "agent_detection_timeout_ms": 30000,
            "transfer_ring_duration_ms": 30000,
            "on_hold_music": "relaxing_sound",
            "private_handoff_option": {
                "type": "prompt",
                "prompt": (
                    "You are briefing Ulises before you connect him. In ONE short sentence: "
                    "who is on the line, what they want, the property or area they're asking "
                    "about, their budget or timeline if you have it, and anything urgent. "
                    "Then say 'connecting you now.' Do not greet him at length, do not ask "
                    "him questions, and do not repeat the whole conversation."
                ),
            },
        },
        "speak_during_execution": True,
        "execution_message_description": "Tell the caller you're seeing if Ulises is free right now and to hold for just a moment.",
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
