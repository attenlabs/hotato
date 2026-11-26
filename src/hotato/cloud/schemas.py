"""Strict validation of the artifacts the app ingests.

Required fields and types are enforced exactly; an unknown ``schema`` string is
rejected. Extra keys are kept out of the stored shape (only the documented
fields are copied), so a newer producer adding a field does not break ingest.
Every function raises :class:`SchemaError` with a path-qualified message.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Optional

TESTRUN_SCHEMA = "hotato.testrun.v1"
EVENT_SCHEMA = "hotato.monitorevent.v1"
SEVERITIES = ("info", "low", "medium", "warning", "high", "critical")
CHANNELS = ("slack", "pagerduty", "webhook")
OPS = ("<", "<=", ">", ">=", "==", "!=")
MAX_STR = 20000


class SchemaError(ValueError):
    pass


def _fail(path: str, msg: str):
    raise SchemaError(f"{path}: {msg}")


def _obj(v: Any, path: str) -> dict:
    if not isinstance(v, dict):
        _fail(path, "must be an object")
    return v


def _get(o: dict, key: str, path: str, *, nullable: bool = False):
    if key not in o:
        _fail(f"{path}.{key}", "is required")
    v = o[key]
    if v is None and not nullable:
        _fail(f"{path}.{key}", "must not be null")
    return v


def _str(o, key, path, *, nullable=False, max_len=MAX_STR) -> Optional[str]:
    v = _get(o, key, path, nullable=nullable)
    if v is None:
        return None
    if not isinstance(v, str):
        _fail(f"{path}.{key}", "must be a string")
    if len(v) > max_len:
        _fail(f"{path}.{key}", f"is longer than {max_len} characters")
    return v


def _num(o, key, path, *, nullable=False, lo=None, hi=None) -> Optional[float]:
    v = _get(o, key, path, nullable=nullable)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        _fail(f"{path}.{key}", "must be a finite number")
    if lo is not None and v < lo:
        _fail(f"{path}.{key}", f"must be >= {lo}")
    if hi is not None and v > hi:
        _fail(f"{path}.{key}", f"must be <= {hi}")
    return float(v)


def _int(o, key, path, *, lo=0) -> int:
    v = _get(o, key, path)
    if isinstance(v, bool) or not isinstance(v, int):
        _fail(f"{path}.{key}", "must be an integer")
    if v < lo:
        _fail(f"{path}.{key}", f"must be >= {lo}")
    return v


def _bool(o, key, path) -> bool:
    v = _get(o, key, path)
    if not isinstance(v, bool):
        _fail(f"{path}.{key}", "must be true or false")
    return v


def _list(o, key, path, *, nullable=False, max_len=100000):
    v = _get(o, key, path, nullable=nullable)
    if v is None:
        return None
    if not isinstance(v, list):
        _fail(f"{path}.{key}", "must be a list")
    if len(v) > max_len:
        _fail(f"{path}.{key}", f"has more than {max_len} items")
    return v


def parse_ts(o, key, path, *, nullable=False) -> Optional[dt.datetime]:
    """ISO 8601 to naive UTC."""
    s = _str(o, key, path, nullable=nullable, max_len=64)
    if s is None:
        return None
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        _fail(f"{path}.{key}", "must be an ISO 8601 timestamp")
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t


def _schema(o, expected, path="$"):
    s = _get(o, "schema", path)
    if s != expected:
        _fail(f"{path}.schema", f"unknown schema {s!r}; this endpoint takes {expected!r}")


# ----------------------------------------------------------------- test run

def validate_testrun(body: Any) -> dict:
    p = "$"
    o = _obj(body, p)
    _schema(o, TESTRUN_SCHEMA)
    agent = _obj(_get(o, "agent", p), "$.agent")
    judge = _obj(_get(o, "judge", p), "$.judge")
    summary = _obj(_get(o, "summary", p), "$.summary")
    out = {
        "run_id": _str(o, "run_id", p, max_len=200),
        "started_at": parse_ts(o, "started_at", p),
        "finished_at": parse_ts(o, "finished_at", p),
        "agent": {"stack": _str(agent, "stack", "$.agent", max_len=100),
                  "id": _str(agent, "id", "$.agent", max_len=200)},
        "judge": {"provider": _str(judge, "provider", "$.judge", max_len=100),
                  "model": _str(judge, "model", "$.judge", max_len=200)},
        "summary": {"cases": _int(summary, "cases", "$.summary"),
                    "pass_rate": _num(summary, "pass_rate", "$.summary", lo=0, hi=1),
                    "pass_all": _int(summary, "pass_all", "$.summary")},
        "cases": [],
    }
    if not out["run_id"]:
        _fail("$.run_id", "must not be empty")
    seen = set()
    for i, c in enumerate(_list(o, "cases", p, max_len=5000)):
        cp = f"$.cases[{i}]"
        c = _obj(c, cp)
        case = {
            "case_id": _str(c, "case_id", cp, max_len=200),
            "name": _str(c, "name", cp, max_len=300),
            "persona": _str(c, "persona", cp),
            "goal": _str(c, "goal", cp),
            "repeats": _int(c, "repeats", cp, lo=0),
            "passed": _int(c, "passed", cp),
            "pass_rate": _num(c, "pass_rate", cp, lo=0, hi=1),
            "pass_all": _bool(c, "pass_all", cp),
            "criteria": [], "runs": [],
        }
        if case["case_id"] in seen:
            _fail(f"{cp}.case_id", "duplicate case_id in this run")
        seen.add(case["case_id"])
        for j, cr in enumerate(_list(c, "criteria", cp)):
            crp = f"{cp}.criteria[{j}]"
            cr = _obj(cr, crp)
            case["criteria"].append({"name": _str(cr, "name", crp, max_len=300),
                                     "pass_rate": _num(cr, "pass_rate", crp, lo=0, hi=1)})
        for j, r in enumerate(_list(c, "runs", cp)):
            case["runs"].append(_validate_case_run(r, f"{cp}.runs[{j}]"))
        out["cases"].append(case)
    return out


def _validate_case_run(r, p) -> dict:
    r = _obj(r, p)
    run = {"run_index": _int(r, "run_index", p), "transcript": [], "verdicts": [],
           "tool_calls": [], "error": _str(r, "error", p, nullable=True)}
    for k, t in enumerate(_list(r, "transcript", p)):
        tp = f"{p}.transcript[{k}]"
        t = _obj(t, tp)
        role = _str(t, "role", tp, max_len=20)
        if role not in ("caller", "agent"):
            _fail(f"{tp}.role", "must be 'caller' or 'agent'")
        run["transcript"].append({"role": role, "text": _str(t, "text", tp),
                                  "t": _num(t, "t", tp)})
    tc = _list(r, "tool_calls", p)
    # Tool calls are opaque to the app; keep them as JSON, bounded.
    run["tool_calls"] = tc
    for k, v in enumerate(_list(r, "verdicts", p)):
        vp = f"{p}.verdicts[{k}]"
        v = _obj(v, vp)
        verdict = _str(v, "verdict", vp, max_len=10)
        if verdict not in ("pass", "fail", "unknown"):
            _fail(f"{vp}.verdict", "must be 'pass', 'fail' or 'unknown'")
        run["verdicts"].append({
            "criterion": _str(v, "criterion", vp, max_len=300), "verdict": verdict,
            "confidence": _num(v, "confidence", vp, lo=0, hi=1),
            "evidence": _str(v, "evidence", vp), "explanation": _str(v, "explanation", vp)})
    return run


# ------------------------------------------------------------ monitor event

def validate_event(body: Any, path: str = "$") -> dict:
    o = _obj(body, path)
    _schema(o, EVENT_SCHEMA, path)
    ev = {
        "call_id": _str(o, "call_id", path, max_len=200),
        "stack": _str(o, "stack", path, max_len=100),
        "agent_id": _str(o, "agent_id", path, nullable=True, max_len=200),
        "started_at": parse_ts(o, "started_at", path, nullable=True),
        "duration_s": _num(o, "duration_s", path, nullable=True, lo=0),
        "score": _num(o, "score", path, nullable=True),
        "incidents": [], "alerts_fired": [],
        "report_path": _str(o, "report_path", path, nullable=True, max_len=2000),
        "scored_at": parse_ts(o, "scored_at", path),
    }
    if not ev["call_id"]:
        _fail(f"{path}.call_id", "must not be empty")
    for i, inc in enumerate(_list(o, "incidents", path, max_len=1000)):
        ip = f"{path}.incidents[{i}]"
        inc = _obj(inc, ip)
        ev["incidents"].append({
            "type": _str(inc, "type", ip, max_len=100),
            "severity": _str(inc, "severity", ip, max_len=50),
            "at_s": _num(inc, "at_s", ip), "detail": _str(inc, "detail", ip)})
    for i, a in enumerate(_list(o, "alerts_fired", path, max_len=1000)):
        if not isinstance(a, str):
            _fail(f"{path}.alerts_fired[{i}]", "must be a string")
        ev["alerts_fired"].append(a[:200])
    return ev


def validate_events(body: Any, max_batch: int = 1000) -> list:
    if isinstance(body, list):
        if len(body) > max_batch:
            raise SchemaError(f"$: at most {max_batch} events per request")
        return [validate_event(b, f"$[{i}]") for i, b in enumerate(body)]
    return [validate_event(body)]


# -------------------------------------------------------------------- rules

def validate_rules(body: Any) -> list:
    if not isinstance(body, list):
        raise SchemaError("$: rules must be a list")
    if len(body) > 200:
        raise SchemaError("$: at most 200 rules")
    out, ids = [], set()
    for i, r in enumerate(body):
        p = f"$[{i}]"
        r = _obj(r, p)
        rule = {"id": _str(r, "id", p, max_len=100)}
        if not rule["id"]:
            _fail(f"{p}.id", "must not be empty")
        if rule["id"] in ids:
            _fail(f"{p}.id", "duplicate rule id")
        ids.add(rule["id"])
        when = _obj(_get(r, "when", p), f"{p}.when")
        wp = f"{p}.when"
        if "metric" in when:
            op = _str(when, "op", wp, max_len=3)
            if op not in OPS:
                _fail(f"{wp}.op", "must be one of " + ", ".join(OPS))
            rule["when"] = {"metric": _str(when, "metric", wp, max_len=100), "op": op,
                            "value": _num(when, "value", wp)}
        elif "incident_type" in when:
            sev = _str(when, "severity_at_least", wp, max_len=50)
            rule["when"] = {"incident_type": _str(when, "incident_type", wp, max_len=100),
                            "severity_at_least": sev}
        else:
            _fail(wp, "needs {metric, op, value} or {incident_type, severity_at_least}")
        win = _get(r, "window", p, nullable=True)
        if win is None:
            rule["window"] = None
        else:
            wn = f"{p}.window"
            win = _obj(win, wn)
            calls, hits = _int(win, "calls", wn, lo=1), _int(win, "min_hits", wn, lo=1)
            if hits > calls:
                _fail(f"{wn}.min_hits", "must not exceed calls")
            rule["window"] = {"calls": calls, "min_hits": hits}
        chans = _list(r, "channels", p, max_len=10)
        for j, ch in enumerate(chans):
            if ch not in CHANNELS:
                _fail(f"{p}.channels[{j}]", "must be one of " + ", ".join(CHANNELS))
        rule["channels"] = list(chans)
        rule["cooldown_min"] = _int(r, "cooldown_min", p, lo=0)
        out.append(rule)
    return out
