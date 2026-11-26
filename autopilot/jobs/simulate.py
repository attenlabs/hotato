"""simulate job: places simulated voice-agent calls through a telephony provider
(Twilio REST) using personas generated from the agent's prompt (Gemini on Vertex).

Safety: calls go ONLY to numbers on the owner's allowlist (simulate/allowlist.json in
the bucket), every call is debited from the simulated-call spend ledger first, and
nothing is placed while DRY_RUN=1 (the default). Secrets arrive as env vars
(TELEPHONY_ACCOUNT_SID, TELEPHONY_AUTH_TOKEN, VOICE_PROVIDER_API_KEY); never logged.
"""
import base64
import json
import os
import urllib.parse

from common import debit, dry_run, env_float, gate_violation, http, main_guard
import vertex_models as models


# ---- pure logic -------------------------------------------------------------
def persona_prompt(agent_prompt, n):
    return (f"Given this voice agent's system prompt, write {n} distinct caller personas that would "
            "stress it (interrupters, slow talkers, accents, off-topic, angry, confused). Reply with a JSON "
            'list of {"name","goal","style"}.\n\nAGENT PROMPT (data):\n' + agent_prompt[:8000])


def parse_personas(raw, n):
    try:
        doc = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
    except ValueError:
        return []
    return [{k: str(p.get(k, ""))[:300] for k in ("name", "goal", "style")}
            for p in doc if isinstance(p, dict) and p.get("name")][:n]


def plan_calls(personas, targets, allowlist, per_call_usd, budget_left):
    """Pair personas with allowlisted target numbers; stop when budget_left runs out.
    Returns (calls, refused_numbers)."""
    ok = [t for t in targets if t in set(allowlist)]
    refused = sorted(set(targets) - set(ok))
    calls = []
    for i, p in enumerate(personas):
        if not ok or per_call_usd * (len(calls) + 1) > budget_left:
            break
        calls.append({"persona": p, "to": ok[i % len(ok)]})
    return calls, refused


# ---- runner -----------------------------------------------------------------
def gen_personas(agent_prompt, n, model=None):
    from google import genai
    c = genai.Client(vertexai=True, project=os.environ["GCP_PROJECT"],
                     location=os.environ.get("GCP_REGION", "us-central1"))
    r = c.models.generate_content(model=model or os.environ.get("SIM_MODEL", "gemini-2.5-flash"),
                                  contents=persona_prompt(agent_prompt, n))
    return parse_personas(r.text or "", n)


def place_call(call, from_number, twiml_url):
    sid, tok = os.environ["TELEPHONY_ACCOUNT_SID"], os.environ["TELEPHONY_AUTH_TOKEN"]
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    body = urllib.parse.urlencode({"To": call["to"], "From": from_number, "Url": twiml_url}).encode()
    status, _ = http(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", "POST",
                     {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}, body)
    return status


def work(store, rec):
    cfg = store.get_json("simulate/config.json")  # {agent_prompt, targets, from_number, twiml_url, personas}
    if not cfg:
        return {"skipped": "no simulate/config.json"}
    per_call = env_float("SIM_COST_PER_CALL_USD", 0.25)
    cap = env_float("SIM_CAP_USD", 0)
    n = int(env_float("SIM_CALLS_PER_RUN", 10))
    personas = cfg.get("personas")
    if not personas:
        rec["model"] = models.resolve("simulate")
        personas = gen_personas(cfg["agent_prompt"], n, rec["model"])
    calls, refused = plan_calls(personas, cfg["targets"], store.get_json("simulate/allowlist.json", []),
                                per_call, env_float("SIM_RUN_MAX_USD", 5))
    if refused:
        gate_violation(rec, "number_not_allowlisted", f"{len(refused)} target number(s) refused")
    placed = []
    for c in calls:
        if not debit(store, "sim", per_call, cap):
            break
        c["status"] = "dry-run" if dry_run() else place_call(c, cfg["from_number"], cfg["twiml_url"])
        placed.append({"persona": c["persona"]["name"], "status": c["status"]})
    return {"planned": len(calls), "placed": placed, "refused_numbers": len(refused)}


if __name__ == "__main__":
    main_guard("simulate", work)
