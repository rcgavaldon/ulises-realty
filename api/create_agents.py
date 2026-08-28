"""Create the Ulises Realty demo Retell agents (EN + ES) sharing one LLM.

Run once:  python api/create_agents.py
Reads RETELL_API_KEY from Sofia's .env (same Retell account).
Writes the resulting IDs to api/agents.env for the Modal secret.
"""
import os, re, json
from retell import Retell

MODAL_URL = "https://roberto-gavaldon3--ulises-realty-api-api.modal.run"

def _read_key():
    with open(r"C:\Users\rober\Sofia Ai Voice\.env", encoding="utf-8") as f:
        m = re.search(r"^RETELL_API_KEY=(.+)$", f.read(), re.M)
    return m.group(1).strip()

client = Retell(api_key=_read_key())

PROMPT = """\
## Identity
You are Sofia, the intake assistant for Ulises, a bilingual REALTOR(R) in El Paso, Texas.
You are calling {{name}} back RIGHT NOW because moments ago they submitted a form on
Ulises's website. Their interest: {{interest}}. Their note: "{{message}}".
This is a warm, expected call — they checked a box agreeing to receive it.

## Style
- Speak ONLY {{call_language}} for the entire call.
- Warm, upbeat, human. One idea per turn, max two short sentences.
- One question at a time. Never a questionnaire tone.
- Never invent listings, prices, rates, or availability. If asked something you don't
  know: "Great question — I'll make sure Ulises covers that when he calls you."
- If asked whether you are an AI, answer honestly and continue naturally.
- If it's clearly voicemail: leave one short message — Ulises's assistant confirming
  their request, Ulises will follow up personally — then end the call.

## Call flow
1. Confirm you're speaking with {{name}}. If wrong person, apologize briefly and end.
2. Remind them why you're calling: they just asked about {{interest}} on Ulises's site.
3. Qualify, adapting to their interest:
   BUYING: areas of El Paso they like -> price range -> pre-approved with a lender?
   (if not, mention Ulises can connect them with good local lenders) -> beds/baths or
   must-haves -> how soon they want to move.
   SELLING / HOME VALUE: which neighborhood + property type -> rough condition ->
   why/when they're selling -> whether they also need to buy their next home.
4. Ask the best time today or tomorrow for Ulises to call them personally.
5. Recap what you captured in one sentence, tell them Ulises will call at that time,
   thank them warmly, end the call.

## Hard rules
- Total call target: under 4 minutes. Keep momentum.
- Never discuss commission rates or contract terms — that's for Ulises.
- If they ask to stop being contacted, apologize, confirm they won't be contacted, end.
"""

BEGIN_EN = "Hi, is this {{name}}? ... This is Sofia, Ulises's assistant — you just asked about {{interest}} on his website, so I'm calling you right back. Do you have two quick minutes?"
BEGIN_ES = "Hola, ¿hablo con {{name}}? ... Le habla Sofía, la asistente de Ulises — acaba de pedir información sobre {{interest}} en su página, así que le llamo de inmediato. ¿Tiene dos minutitos?"

print("Creating shared LLM...")
llm = client.llm.create(
    model="gpt-4.1",
    general_prompt=PROMPT,
    begin_message=BEGIN_EN,
)
print("  llm:", llm.llm_id)

print("Creating ES LLM (same prompt, ES begin message)...")
llm_es = client.llm.create(
    model="gpt-4.1",
    general_prompt=PROMPT,
    begin_message=BEGIN_ES,
)
print("  llm_es:", llm_es.llm_id)

common = dict(
    interruption_sensitivity=0.9,
    responsiveness=0.65,
    normalize_for_speech=True,
    webhook_url=f"{MODAL_URL}/retell-webhook",
    post_call_analysis_data=[
        {"type": "string", "name": "areas", "description": "Areas/neighborhoods of interest or the property's neighborhood", "examples": ["West Side", "Horizon City"]},
        {"type": "string", "name": "budget", "description": "Budget or price range mentioned", "examples": ["$300k-$350k"]},
        {"type": "string", "name": "preapproved", "description": "Pre-approval status: yes/no/unknown", "examples": ["yes", "no"]},
        {"type": "string", "name": "timeline", "description": "How soon they want to buy/sell", "examples": ["ASAP", "3 months"]},
        {"type": "string", "name": "callback_time", "description": "Best time for Ulises to call them", "examples": ["today 6pm"]},
        {"type": "string", "name": "must_haves", "description": "Must-have features or key notes", "examples": ["4 bed, pool"]},
    ],
)

print("Creating EN agent...")
agent_en = client.agent.create(
    agent_name="Ulises Realty Demo — Sofia EN",
    voice_id="11labs-Lily",
    language="en-US",
    response_engine={"type": "retell-llm", "llm_id": llm.llm_id},
    **common,
)
print("  agent_en:", agent_en.agent_id)

print("Creating ES agent...")
agent_es = client.agent.create(
    agent_name="Ulises Realty Demo — Sofia ES",
    voice_id="retell-Claudia",
    language="es-419",
    response_engine={"type": "retell-llm", "llm_id": llm_es.llm_id},
    **common,
)
print("  agent_es:", agent_es.agent_id)

with open(os.path.join(os.path.dirname(__file__), "agents.env"), "w") as f:
    f.write(f"AGENT_EN={agent_en.agent_id}\nAGENT_ES={agent_es.agent_id}\n"
            f"LLM_EN={llm.llm_id}\nLLM_ES={llm_es.llm_id}\n")
print("Wrote api/agents.env")
