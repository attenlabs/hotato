"""Shadow -> live promotion per job, with no person in the loop.

State (in the Store, safe under concurrent jobs):
  promotion/state.json  {"jobs": {job: {mode, since, reason, clean_days, consecutive_failures, day}}}
  promotion/log.jsonl   append-only: day_closed, promoted, demoted events

Rules (gates/policy.json: auto_promote, promote_after_clean_days[_by_job]):
- Every job starts in shadow.
- A day (UTC) is clean when it had at least one successful run and no failed run, no gate
  violation, no kill switch, no demotion and no promotion block (e.g. missing GitHub
  credential). Days with no run are neutral (a weekly job has six of them a week).
- With auto_promote true, a shadow job is promoted at the start of the first run after
  N consecutive clean days (N = promote_after_clean_days, default 7; per-job override).
- A live job is demoted to shadow on any gate violation, on two consecutive failed runs,
  or when it finds the kill-switch flag set (the budget function writes it). Demotion
  resets the clean-day count, so the job must earn N clean days again.
- Promotions and demotions log hotato_alert promoted/demoted at WARNING.
- DRY_RUN=1 from infra still forces shadow (common.effective_mode); the state machine
  keeps running underneath so the job is ready the moment infra sets DRY_RUN=auto.
- gates/policy.py stays authoritative for merges: a live job opens PRs and issues, but
  only policy.decide decides what may merge.
"""
import os
import sys

from common import alert, env_mode

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

STATE_KEY = "promotion/state.json"
LOG_KEY = "promotion/log.jsonl"
DEFAULT_CLEAN_DAYS = 7


def load_policy():
    from gates.policy import load_config
    return load_config()


def needed_days(job, cfg):
    by_job = cfg.get("promote_after_clean_days_by_job") or {}
    return int(by_job.get(job, cfg.get("promote_after_clean_days", DEFAULT_CLEAN_DAYS)))


def new_job_state():
    return {"mode": "shadow", "since": None, "reason": "initial", "clean_days": 0,
            "consecutive_failures": 0, "day": None}


def new_day(date):
    return {"date": date, "ok": 0, "failed": 0, "crashes": 0, "violations": 0, "blocked": 0, "dirty": False}


def day_clean(d):
    return bool(d) and d["ok"] > 0 and not (d["failed"] or d["violations"] or d["blocked"] or d["dirty"])


def roll_day(js, date, events):
    """Close the open day if `date` is later; open a day for `date`."""
    d = js.get("day")
    if d and d["date"] >= date:
        return
    if d:
        clean = day_clean(d)
        js["clean_days"] = js["clean_days"] + 1 if clean else 0
        events.append({"event": "day_closed", "date": d["date"], "clean": clean, "clean_days": js["clean_days"],
                       "day": dict(d)})
    js["day"] = new_day(date)


def set_mode(js, mode, at, reason, events):
    js.update(mode=mode, since=at.isoformat(), reason=reason)
    if mode == "shadow":
        js["clean_days"] = 0
        js["consecutive_failures"] = 0
        if js.get("day"):
            js["day"]["dirty"] = True
    events.append({"event": "promoted" if mode == "live" else "demoted", "reason": reason})


def _commit(store, job, at, mutate):
    """Apply mutate(js, events) to the job's state; log and alert the events once."""
    events = []

    def fn(state):
        events.clear()
        state.setdefault("jobs", {})
        js = state["jobs"].setdefault(job, new_job_state())
        mutate(js, events)
        return state

    state = store.update_json(STATE_KEY, fn, {"jobs": {}})
    js = state["jobs"][job]
    stamp = at.isoformat()
    store.append_jsonl(LOG_KEY, [dict(e, job=job, at=stamp, env=env_mode()) for e in events])
    for e in events:
        if e["event"] in ("promoted", "demoted"):
            note = ""
            if e["event"] == "promoted" and env_mode() == "shadow":
                note = " (held in shadow while infra sets DRY_RUN=1; live once it is DRY_RUN=auto)"
            alert(e["event"], "WARNING", f"{job} -> {js['mode']}: {e['reason']}{note}", job)
    return js


def begin_run(store, job, at, killed=False, kill_reason=None, cfg=None):
    """Roll the day, apply kill-switch demotion or earned promotion. -> state mode."""
    cfg = cfg if cfg is not None else load_policy()
    date = at.date().isoformat()

    def mutate(js, events):
        roll_day(js, date, events)
        if killed:
            js["day"]["dirty"] = True
            if js["mode"] == "live":
                set_mode(js, "shadow", at, f"kill switch is set ({(kill_reason or '').strip()[:100]})", events)
        elif cfg.get("auto_promote") and js["mode"] == "shadow":
            n = needed_days(job, cfg)
            if js["clean_days"] >= n:
                set_mode(js, "live", at, f"{js['clean_days']} consecutive clean days in shadow (needs {n})", events)

    return _commit(store, job, at, mutate)["mode"]


def end_run(store, job, rec, at, cfg=None):
    """Count the run into its day; demote a live job on a violation or a second failure."""
    date = (rec.get("started") or at.isoformat())[:10]

    def mutate(js, events):
        roll_day(js, date, events)
        d = js["day"]
        status = rec.get("status")
        violations = len(rec.get("gate_violations") or [])
        d["violations"] += violations
        if rec.get("promotion_block"):
            d["blocked"] += 1
        if status == "ok":
            d["ok"] += 1
            js["consecutive_failures"] = 0
        elif status == "error":
            d["failed"] += 1
            if rec.get("error_kind", "job_crash") == "job_crash":
                d["crashes"] += 1
            js["consecutive_failures"] += 1
        if js["mode"] == "live":
            if violations:
                kinds = ", ".join(sorted({v["kind"] for v in rec["gate_violations"]}))
                set_mode(js, "shadow", at, f"gate violation ({kinds})", events)
            elif js["consecutive_failures"] >= 2:
                set_mode(js, "shadow", at, f"{js['consecutive_failures']} consecutive failed runs", events)

    return _commit(store, job, at, mutate)["mode"]


def status(store):
    """Read-only view for the owner and the monitor."""
    return store.get_json(STATE_KEY, {"jobs": {}})


if __name__ == "__main__":  # print the promotion state: python3 jobs/promotion.py
    import json
    from common import Store
    print(json.dumps(status(Store()), indent=2))
