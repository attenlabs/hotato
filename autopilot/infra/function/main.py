"""Budget kill switch. Triggered by the budget Pub/Sub topic. When spend reaches the
cap it writes the kill-switch flag object that every job reads first."""
import base64
import json
import os


def should_kill(msg, phase=None, cap_fraction=1.0):
    """msg: budget notification {budgetDisplayName, costAmount, budgetAmount}.
    With a phase, only that phase's budgets count: the monthly budget is ignored during the
    sprint (sprint spend would otherwise trip it), and the sprint budget after it."""
    name = msg.get("budgetDisplayName", "").lower()
    if phase and f"hotato {phase}" not in name:
        return False
    cost, budget = msg.get("costAmount", 0), msg.get("budgetAmount", 0)
    return budget > 0 and cost >= budget * cap_fraction


def on_budget(event, context=None):
    data = event["data"] if isinstance(event, dict) else event.data
    msg = json.loads(base64.b64decode(data["data"] if isinstance(data, dict) else data))
    if should_kill(msg, os.environ.get("PHASE")):
        from google.cloud import storage
        storage.Client().bucket(os.environ["STATE_BUCKET"]).blob("flags/kill").upload_from_string(
            json.dumps({"reason": "budget cap", "cost": msg.get("costAmount")}))
        return "killed"
    return "ok"
