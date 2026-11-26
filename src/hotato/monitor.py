"""``hotato monitor``: production call monitoring with alerts.

Connect a Vapi or Retell account and every new call is fetched, checked with
the autopsy engine, written as one event file, and judged against your alert
rules; a rule that fires posts to Slack, PagerDuty or a JSON webhook. A bad
call becomes a regression test with ``hotato monitor pin CALL_ID``.

Everything here is a thin layer over machinery that already exists:

  * listing and fetching recordings, credentials and the mono policy are
    :mod:`hotato.capture`'s (``list_calls`` / ``fetch_one`` / ``resolve_creds``);
  * the per-call check is :func:`hotato.autopsy.run_autopsy`, unchanged;
  * delivery reuses :func:`hotato.notify.post_notification` (http(s) only,
    fail-open, URLs sanitised in every log line);
  * pinning is :func:`hotato.pincmd.run_pin` against the envelopes this module
    keeps under ``<store>/autopsy``.

The store (one directory, ``--store``) holds::

    state.json            cursor, retry queue, call -> autopsy id index, rule state
    events/<call>.json    one ``hotato.monitorevent.v1`` document per call
    audio/<stack>__<call>.wav
    autopsy/autopsy-<id>.{html,json}

A call is scored once: an existing event file means the call is done, so a
restart resumes from the cursor without rescoring. Alerting is at-least-once:
a crash between a send and the event write can repeat that one alert after a
restart (the rule cooldown and the PagerDuty dedup key absorb it).

Nothing leaves the machine except (a) the vendor recording download the
capture adapters already make and (b) a POST to a destination you named with a
flag or an environment variable. A rule naming a channel with no destination is
refused before the first cycle. Audio, credentials and transcript text are
never part of a payload.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import capture as _capture
from . import errors as _errors
from . import notify as _notify

__all__ = [
    "EVENT_SCHEMA",
    "STACKS",
    "METRICS",
    "CHANNELS",
    "load_rules",
    "parse_every",
    "call_score",
    "Destinations",
    "Monitor",
    "run_monitor",
    "run_test_alert",
    "run_monitor_pin",
]

EVENT_SCHEMA = "hotato.monitorevent.v1"
STACKS = ("vapi", "retell")
CHANNELS = ("slack", "pagerduty", "webhook")
METRICS = ("score", "incident_count", "critical_count", "warning_count",
           "dead_air_s", "latency_gap_s", "talk_over_s", "duration_s")
OPS = {"<": lambda a, b: a < b, ">": lambda a, b: a > b,
       ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}
SEVERITIES = ("warning", "critical")

# Per-call score: 100 minus a fixed penalty per measured incident, floored at 0.
# Documented in docs/MONITOR.md; it is a per-call number for alert thresholds,
# not the fleet-level Voice Stability Score.
CRITICAL_PENALTY = 25.0
WARNING_PENALTY = 5.0

# A recording that is not downloadable yet (the call may still be running) is
# retried each cycle; after this many attempts the call is recorded unscored.
MAX_FETCH_ATTEMPTS = 5

DEFAULT_COOLDOWN_MIN = 15
DEFAULT_BACKFILL = "1h"
PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

ENV_SLACK = "HOTATO_SLACK_WEBHOOK_URL"
ENV_PAGERDUTY = "HOTATO_PAGERDUTY_ROUTING_KEY"
ENV_PAGERDUTY_URL = "HOTATO_PAGERDUTY_EVENTS_URL"
ENV_WEBHOOK = "HOTATO_MONITOR_WEBHOOK_URL"


def _log(msg: str) -> None:
    sys.stderr.write(f"[monitor] {_errors.sanitize_urls_in_text(msg)}\n")


def _now_iso(epoch: Optional[float] = None) -> str:
    t = time.time() if epoch is None else epoch
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_every(spec: str) -> float:
    """``5m`` / ``30s`` / ``1h`` -> seconds. A malformed value is a usage error."""
    m = re.fullmatch(r"\s*(\d+)\s*([smh])\s*", (spec or "").lower())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(
            f"--every {spec!r} is not an interval; use e.g. 30s, 5m, 1h.")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


# --- rules -------------------------------------------------------------------

def load_rules(path: str) -> List[dict]:
    """Read and validate a rules file (a JSON list, or ``{"rules": [...]}``).
    Every problem is a ``ValueError`` naming the rule, so it exits 2 before any
    call is touched."""
    try:
        with _errors.open_regular(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(f"rules file {path!r} is not readable JSON ({exc})") from exc
    if isinstance(doc, dict):
        doc = doc.get("rules")
    if not isinstance(doc, list) or not doc:
        raise ValueError(
            f"rules file {path!r} must hold a non-empty list of rules "
            '(or {"rules": [...]}); see docs/MONITOR.md')
    rules, seen = [], set()
    for i, raw in enumerate(doc, 1):
        rules.append(_validate_rule(raw, i, seen))
    return rules


def _validate_rule(raw: Any, i: int, seen: set) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"rule #{i} is not an object")
    rid = raw.get("id")
    if not isinstance(rid, str) or not rid.strip():
        raise ValueError(f"rule #{i} needs a non-empty string id")
    if rid in seen:
        raise ValueError(f"rule id {rid!r} appears twice")
    seen.add(rid)
    where = f"rule {rid!r}"
    when = raw.get("when")
    if not isinstance(when, dict):
        raise ValueError(f"{where} needs a when object")
    if "metric" in when:
        if when["metric"] not in METRICS:
            raise ValueError(
                f"{where}: unknown metric {when['metric']!r}; one of "
                f"{', '.join(METRICS)}")
        if when.get("op") not in OPS:
            raise ValueError(f"{where}: op must be one of {', '.join(OPS)}")
        v = when.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{where}: value must be a number")
        norm_when = {"metric": when["metric"], "op": when["op"], "value": float(v)}
    elif "incident_type" in when:
        itype = when["incident_type"]
        if not isinstance(itype, str) or not itype:
            raise ValueError(f"{where}: incident_type must be a string "
                             '(a kind such as "dead-air", or "any")')
        sev = when.get("severity_at_least", "warning")
        if not isinstance(sev, str) or sev.lower() not in SEVERITIES:
            raise ValueError(f"{where}: severity_at_least must be one of "
                             f"{', '.join(SEVERITIES)}")
        norm_when = {"incident_type": itype.lower(),
                     "severity_at_least": sev.lower()}
    else:
        raise ValueError(f"{where}: when needs a metric or an incident_type")
    window = raw.get("window")
    norm_window = None
    if window is not None:
        if (not isinstance(window, dict)
                or not isinstance(window.get("calls"), int)
                or not isinstance(window.get("min_hits"), int)
                or isinstance(window.get("calls"), bool)
                or window["calls"] < 1
                or not 1 <= window["min_hits"] <= window["calls"]):
            raise ValueError(
                f"{where}: window needs integer calls >= 1 and "
                "1 <= min_hits <= calls")
        norm_window = {"calls": window["calls"], "min_hits": window["min_hits"]}
    channels = raw.get("channels")
    if (not isinstance(channels, list) or not channels
            or any(c not in CHANNELS for c in channels)):
        raise ValueError(f"{where}: channels must be a non-empty list drawn "
                         f"from {', '.join(CHANNELS)}")
    cooldown = raw.get("cooldown_min", DEFAULT_COOLDOWN_MIN)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or cooldown < 0:
        raise ValueError(f"{where}: cooldown_min must be a number >= 0")
    return {"id": rid, "when": norm_when, "window": norm_window,
            "channels": list(dict.fromkeys(channels)),
            "cooldown_min": float(cooldown)}


# --- per-call measurement ----------------------------------------------------

def call_score(incidents: List[dict]) -> float:
    """100 minus 25 per critical and 5 per warning incident, floored at 0."""
    pen = sum(CRITICAL_PENALTY if i["severity"] == "critical" else WARNING_PENALTY
              for i in incidents)
    return round(max(0.0, 100.0 - pen), 1)


def _num(d: dict, *keys: str) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _event_incidents(result: dict) -> Tuple[List[dict], Dict[str, Optional[float]]]:
    """Map autopsy incidents to the event shape and collect the timing maxima
    the rule metrics read."""
    out = []
    dead_air = latency = talk_over = 0.0
    for inc in result["incidents"]:
        kind = inc["kind_key"]
        m = inc.get("measurements") or {}
        gap = _num(m, "gap_sec", "trailing_silence_sec", "silence_sec")
        over = _num(m, "overlap_sec")
        if kind == "dead-air" and gap is not None:
            dead_air = max(dead_air, gap)
        if kind in ("dead-air", "latency-spike") and gap is not None:
            latency = max(latency, gap)
        if kind in ("barge-in", "talk-over") and over is not None:
            talk_over = max(talk_over, over)
        out.append({"type": kind, "severity": inc["severity"].lower(),
                    "at_s": float(inc["t_sec"]),
                    "detail": str(inc.get("detail") or "")})
    return out, {"dead_air_s": dead_air, "latency_gap_s": latency,
                 "talk_over_s": talk_over}


def _metrics(event: dict, extra: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    inc = event["incidents"]
    scored = event["score"] is not None or bool(inc) or extra.get("scored")
    m: Dict[str, Optional[float]] = {
        "score": event["score"],
        "incident_count": float(len(inc)) if scored else None,
        "critical_count": float(sum(i["severity"] == "critical" for i in inc)) if scored else None,
        "warning_count": float(sum(i["severity"] == "warning" for i in inc)) if scored else None,
        "duration_s": event["duration_s"],
        "dead_air_s": extra.get("dead_air_s") if scored else None,
        "latency_gap_s": extra.get("latency_gap_s") if scored else None,
        "talk_over_s": extra.get("talk_over_s") if scored else None,
    }
    return m


def _hit(rule: dict, event: dict, metrics: Dict[str, Optional[float]]) -> Optional[bool]:
    """True/False, or None when the call carries no data for this rule (an
    unscored call, or a score on a mono call): neither a hit nor a clear."""
    when = rule["when"]
    if "metric" in when:
        v = metrics.get(when["metric"])
        if v is None:
            return None
        return bool(OPS[when["op"]](v, when["value"]))
    if not event["_analyzed"]:
        return None
    floor = SEVERITIES.index(when["severity_at_least"])
    for inc in event["incidents"]:
        if when["incident_type"] not in ("any", inc["type"]):
            continue
        if SEVERITIES.index(inc["severity"]) >= floor:
            return True
    return False


# --- destinations and delivery ----------------------------------------------

class Destinations:
    """Where alerts go. Each is explicit: a flag, else its environment
    variable. A destination never appears in a log line or a payload."""

    def __init__(self, slack_url: Optional[str] = None,
                 pagerduty_key: Optional[str] = None,
                 webhook_url: Optional[str] = None,
                 pagerduty_url: Optional[str] = None,
                 report_url_base: Optional[str] = None):
        self.slack_url = slack_url or os.environ.get(ENV_SLACK) or None
        self.pagerduty_key = pagerduty_key or os.environ.get(ENV_PAGERDUTY) or None
        self.webhook_url = webhook_url or os.environ.get(ENV_WEBHOOK) or None
        self.pagerduty_url = (pagerduty_url or os.environ.get(ENV_PAGERDUTY_URL)
                              or PAGERDUTY_EVENTS_URL)
        self.report_url_base = report_url_base
        for url in (self.slack_url, self.webhook_url, self.pagerduty_url):
            if url:
                _notify.validate_notify_url(url)

    def configured(self, channel: str) -> bool:
        return bool({"slack": self.slack_url, "pagerduty": self.pagerduty_key,
                     "webhook": self.webhook_url}[channel])

    def require(self, rules: List[dict]) -> None:
        for r in rules:
            for ch in r["channels"]:
                if not self.configured(ch):
                    flag = {"slack": f"--slack-webhook-url or {ENV_SLACK}",
                            "pagerduty": f"--pagerduty-routing-key or {ENV_PAGERDUTY}",
                            "webhook": f"--webhook-url or {ENV_WEBHOOK}"}[ch]
                    raise ValueError(
                        f"rule {r['id']!r} sends to {ch} but no destination is "
                        f"set; pass {flag}.")


def _report_ref(dest: Destinations, report_path: Optional[str]) -> Optional[str]:
    if not report_path:
        return None
    if dest.report_url_base:
        return dest.report_url_base.rstrip("/") + "/" + os.path.basename(report_path)
    return report_path


def _describe_hit(rule: dict, event: dict) -> str:
    w = rule["when"]
    if "metric" in w:
        what = f"{w['metric']} {w['op']} {w['value']:g}"
    else:
        what = (f"{w['incident_type']} incident, {w['severity_at_least']} or worse")
    if rule.get("window"):
        what += (f" in at least {rule['window']['min_hits']} of the last "
                 f"{rule['window']['calls']} calls")
    return what


def _incident_lines(event: dict, limit: int = 5) -> List[str]:
    lines = [f"{i['severity'].upper()} {i['type']} at {i['at_s']:.1f}s: {i['detail']}"
             for i in event["incidents"][:limit]]
    extra = len(event["incidents"]) - limit
    if extra > 0:
        lines.append(f"... and {extra} more")
    return lines


def slack_message(rule: dict, event: dict, ref: Optional[str],
                  prefix: str = "") -> dict:
    """A readable Block Kit message: what fired, which call, the score, the
    incidents, and where the report lives."""
    score = "n/a" if event["score"] is None else f"{event['score']:g}"
    agent = event.get("agent_id") or "unknown agent"
    head = f"{prefix}Hotato alert: {rule['id']}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": head[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*Rule:* {_describe_hit(rule, event)}\n*Call:* `{event['call_id']}` "
            f"on {event['stack']} ({agent})")}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Score*\n{score} / 100"},
            {"type": "mrkdwn", "text": f"*Incidents*\n{len(event['incidents'])}"},
        ]},
    ]
    lines = _incident_lines(event)
    if lines:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn", "text": "\n".join(f"- {l}" for l in lines)[:2900]}})
    if ref:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Report: {ref}"}]})
    return {"text": f"{head} on call {event['call_id']} (score {score})",
            "blocks": blocks}


def pagerduty_event(action: str, rule: dict, agent_id: Optional[str], key: str,
                    event: Optional[dict] = None, ref: Optional[str] = None,
                    source: str = "hotato-monitor") -> dict:
    body: Dict[str, Any] = {
        "routing_key": key,
        "event_action": action,
        "dedup_key": dedup_key(rule["id"], agent_id),
    }
    if action == "trigger" and event is not None:
        score = "n/a" if event["score"] is None else f"{event['score']:g}"
        sev = "critical" if any(i["severity"] == "critical"
                                for i in event["incidents"]) else "warning"
        body["payload"] = {
            "summary": (f"Hotato rule {rule['id']}: {_describe_hit(rule, event)} "
                        f"(call {event['call_id']}, score {score})")[:1024],
            "source": f"{source}:{event['stack']}",
            "severity": sev,
            "custom_details": {
                "call_id": event["call_id"], "agent_id": agent_id,
                "score": event["score"],
                "incidents": _incident_lines(event),
                "report": ref,
            },
        }
    return body


def dedup_key(rule_id: str, agent_id: Optional[str]) -> str:
    return f"hotato-{rule_id}-{agent_id or 'all-agents'}"


def _send_channel(dest: Destinations, channel: str, rule: dict, event: dict,
                  ref: Optional[str]) -> bool:
    if channel == "slack":
        return _notify.post_notification(dest.slack_url,
                                         slack_message(rule, event, ref))
    if channel == "pagerduty":
        return _notify.post_notification(
            dest.pagerduty_url,
            pagerduty_event("trigger", rule, event.get("agent_id"),
                            dest.pagerduty_key, event, ref))
    counts = {"incidents": len(event["incidents"])}
    payload = _notify.build_payload(
        kind="monitor-alert",
        text=(f"hotato monitor: rule {rule['id']} fired on call "
              f"{event['call_id']} ({_describe_hit(rule, event)})"),
        counts=counts,
        artifacts={"report": ref} if ref else {},
        rule_id=rule["id"], call_id=event["call_id"], stack=event["stack"],
        agent_id=event.get("agent_id"), score=event["score"],
        started_at=event.get("started_at"), incidents=event["incidents"])
    return _notify.post_notification(dest.webhook_url, payload)


# --- store -------------------------------------------------------------------

def _safe(ident: str) -> str:
    return _capture._safe_id(ident)


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.events_dir = os.path.join(self.root, "events")
        self.audio_dir = os.path.join(self.root, "audio")
        self.autopsy_dir = os.path.join(self.root, "autopsy")
        self.state_path = os.path.join(self.root, "state.json")
        for d in (self.events_dir, self.audio_dir, self.autopsy_dir):
            os.makedirs(d, exist_ok=True)

    def event_path(self, call_id: str) -> str:
        return os.path.join(self.events_dir, f"{_safe(call_id)}.json")

    def has_event(self, call_id: str) -> bool:
        return os.path.isfile(self.event_path(call_id))

    def load_state(self) -> dict:
        try:
            with _errors.open_regular(self.state_path, "r", encoding="utf-8") as fh:
                st = json.load(fh)
            if isinstance(st, dict):
                st.setdefault("cursor", None)
                st.setdefault("pending", {})
                st.setdefault("index", {})
                st.setdefault("rules", {})
                return st
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"{self.state_path!r} is not readable JSON ({exc}); fix or "
                "delete it (deleting it rescans from --since).") from exc
        return {"cursor": None, "pending": {}, "index": {}, "rules": {}}

    def save_state(self, st: dict) -> None:
        _atomic_write(self.state_path, json.dumps(st, indent=2, sort_keys=True) + "\n")

    def write_event(self, event: dict) -> str:
        doc = {k: v for k, v in event.items() if not k.startswith("_")}
        path = self.event_path(event["call_id"])
        _atomic_write(path, json.dumps(doc, indent=2) + "\n")
        return path


# --- the monitor -------------------------------------------------------------

class Monitor:
    """One monitored account. ``lister`` and ``fetcher`` default to the capture
    adapters; tests inject fakes."""

    def __init__(self, stack: str, store: Store, rules: List[dict],
                 dest: Destinations, *, creds: Optional[dict] = None,
                 allow_mono: bool = False, backfill: str = DEFAULT_BACKFILL,
                 limit: int = 50,
                 lister: Optional[Callable] = None,
                 fetcher: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time):
        if stack not in STACKS:
            raise ValueError(f"monitor supports {', '.join(STACKS)}; got {stack!r}")
        self.stack, self.store, self.rules, self.dest = stack, store, rules, dest
        self.creds = creds or {}
        self.allow_mono = allow_mono
        self.backfill = backfill
        self.limit = max(1, int(limit))
        self.lister = lister or _capture.list_calls
        self.fetcher = fetcher or _capture.fetch_one
        self.clock = clock

    # -- one cycle
    def run_cycle(self) -> dict:
        st = self.store.load_state()
        now = self.clock()
        since = st["cursor"]
        if since is None:
            since = _capture.since_epoch(self.backfill)
        items = self.lister(self.stack, self.creds, since=since, limit=self.limit)
        if len(items) >= self.limit:
            _log(f"{len(items)} calls listed (the --limit); raise --limit or "
                 "shorten --every if calls are arriving faster than that")
        todo: Dict[str, dict] = {}
        for it in items:
            if not self.store.has_event(it["id"]):
                todo[it["id"]] = dict(it)
        for cid, meta in st["pending"].items():
            if not self.store.has_event(cid):
                todo.setdefault(cid, {"id": cid, "created": meta.get("created"),
                                      "agent_id": meta.get("agent_id")})
        order = sorted(todo.values(), key=lambda it: (it.get("created") or 0.0))
        summary = {"listed": len(items), "scored": 0, "unscored": 0,
                   "retry": 0, "alerts": 0, "events": []}
        newest = st["cursor"]
        for it in order:
            res = self._handle(it, st)
            if res == "retry":
                summary["retry"] += 1
                continue
            summary["scored" if res["_analyzed"] else "unscored"] += 1
            summary["alerts"] += len(res["alerts_fired"])
            summary["events"].append(res["call_id"])
        for it in items:
            c = it.get("created")
            if c is not None and (newest is None or c > newest):
                newest = c
        st["cursor"] = newest
        self.store.save_state(st)
        return summary

    def _handle(self, it: dict, st: dict):
        cid = it["id"]
        dest_wav = os.path.join(self.store.audio_dir, f"{self.stack}__{_safe(cid)}.wav")
        try:
            wav = self.fetcher(self.stack, cid, self.creds, dest_wav,
                               allow_mono=self.allow_mono)
        except Exception as exc:  # noqa: BLE001 - one bad call never stops the cycle
            meta = st["pending"].setdefault(cid, {"attempts": 0})
            meta["attempts"] += 1
            meta["created"] = it.get("created")
            meta["agent_id"] = it.get("agent_id")
            _log(f"{cid}: not fetched (attempt {meta['attempts']}/"
                 f"{MAX_FETCH_ATTEMPTS}): {exc}")
            if meta["attempts"] < MAX_FETCH_ATTEMPTS:
                return "retry"
            return self._finish(it, st, None, None, None)
        try:
            from . import autopsy as _autopsy

            result, html = _autopsy.run_autopsy(wav)
        except ValueError as exc:
            _log(f"{cid}: not scorable: {exc}")
            return self._finish(it, st, None, None, None)
        apx = result["id"]
        report = os.path.join(self.store.autopsy_dir, f"autopsy-{apx}.html")
        envelope = os.path.join(self.store.autopsy_dir, f"autopsy-{apx}.json")
        result["report_path"], result["envelope_path"] = report, envelope
        _atomic_write(report, html)
        _atomic_write(envelope, json.dumps(
            _autopsy_envelope(result), indent=2, sort_keys=True) + "\n")
        st["index"][cid] = apx
        return self._finish(it, st, result, report, wav)

    def _finish(self, it, st, result, report, wav):
        cid = it["id"]
        created = it.get("created")
        if result is not None:
            incidents, extra = _event_incidents(result)
            mono = result.get("mode") == "mono"
            score = None if mono else call_score(incidents)
            extra["scored"] = True
            duration = float(result["duration_sec"])
        else:
            incidents, extra, score, duration = [], {"scored": False}, None, None
        event = {
            "schema": EVENT_SCHEMA, "call_id": cid, "stack": self.stack,
            "agent_id": it.get("agent_id"),
            "started_at": _now_iso(created) if created is not None else None,
            "duration_s": duration, "score": score, "incidents": incidents,
            "alerts_fired": [], "report_path": report,
            "scored_at": _now_iso(self.clock()),
            "_analyzed": result is not None,
        }
        metrics = _metrics(event, extra)
        event["alerts_fired"] = self._evaluate(event, metrics, st)
        self.store.write_event(event)
        st["pending"].pop(cid, None)
        return event

    # -- rules
    def _evaluate(self, event: dict, metrics: dict, st: dict) -> List[str]:
        fired: List[str] = []
        now = self.clock()
        agent = event.get("agent_id")
        for rule in self.rules:
            hit = _hit(rule, event, metrics)
            if hit is None:
                continue
            rs = st["rules"].setdefault(f"{rule['id']}|{agent or ''}", {})
            window = rule["window"]
            if window:
                hist = (rs.get("history") or []) + [bool(hit)]
                hist = hist[-window["calls"]:]
                rs["history"] = hist
                active = sum(hist) >= window["min_hits"]
            else:
                active = bool(hit)
            if active:
                cooled = (now - rs.get("last_fired", 0.0)) >= rule["cooldown_min"] * 60
                if hit and (not rs.get("open") or cooled):
                    if self._deliver(rule, event):
                        rs["last_fired"], rs["open"] = now, True
                        fired.append(rule["id"])
            elif rs.get("open"):
                self._resolve(rule, agent)
                rs["open"] = False
        return fired

    def _deliver(self, rule: dict, event: dict) -> bool:
        ref = _report_ref(self.dest, event.get("report_path"))
        ok = False
        for ch in rule["channels"]:
            if _send_channel(self.dest, ch, rule, event, ref):
                ok = True
        return ok

    def _resolve(self, rule: dict, agent: Optional[str]) -> None:
        if "pagerduty" in rule["channels"] and self.dest.pagerduty_key:
            _notify.post_notification(
                self.dest.pagerduty_url,
                pagerduty_event("resolve", rule, agent, self.dest.pagerduty_key))


def _autopsy_envelope(result: dict) -> dict:
    from . import autopsy as _autopsy

    return _autopsy.envelope_dict(result)


# --- commands ---------------------------------------------------------------

def run_monitor(stack: str, *, every: str = "5m", once: bool = False,
                rules_path: Optional[str] = None, store: str = "hotato-monitor",
                api_key: Optional[str] = None, allow_mono: bool = False,
                since: str = DEFAULT_BACKFILL, limit: int = 50,
                dest: Optional[Destinations] = None, fmt: str = "text",
                max_cycles: Optional[int] = None,
                lister: Optional[Callable] = None,
                fetcher: Optional[Callable] = None) -> int:
    stack = (stack or "").strip().lower()
    interval = parse_every(every)
    _capture.since_epoch(since)  # validate the window up front
    rules = load_rules(rules_path) if rules_path else []
    dest = dest or Destinations()
    dest.require(rules)
    creds = {}
    if lister is None or fetcher is None:
        try:
            creds = _capture.resolve_creds(
                stack, {"api_key": api_key} if api_key else None)
        except ValueError as exc:
            env = _capture.CONNECT_SPECS[stack]["env"]["api_key"]
            raise ValueError(
                f"monitor needs a {stack} API key: pass --api-key, export "
                f"{env}=YOUR_KEY, or run hotato connect {stack}.") from exc
    mon = Monitor(stack, Store(store), rules, dest, creds=creds,
                  allow_mono=allow_mono, backfill=since, limit=limit,
                  lister=lister, fetcher=fetcher)
    cycles = 0
    while True:
        cycles += 1
        try:
            s = mon.run_cycle()
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - a flaky vendor API must not kill a daemon
            if once:
                raise
            _log(f"cycle failed, will retry: {exc}")
            s = {"listed": 0, "scored": 0, "unscored": 0, "retry": 0,
                 "alerts": 0, "events": [], "error": str(exc)}
        if fmt == "json":
            print(_errors.safe_json_dumps(
                dict(s, kind="monitor-cycle", stack=stack,
                     at=_now_iso(time.time())), indent=None))
        else:
            print(f"hotato monitor {stack}: {s['listed']} listed, "
                  f"{s['scored']} scored, {s['unscored']} unscored, "
                  f"{s['retry']} waiting for a recording, "
                  f"{s['alerts']} alert(s) fired -> {mon.store.events_dir}")
        sys.stdout.flush()
        if once or (max_cycles is not None and cycles >= max_cycles):
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


def _synthetic_event() -> dict:
    return {
        "schema": EVENT_SCHEMA, "call_id": "test-call-000", "stack": "test",
        "agent_id": "test-agent", "started_at": _now_iso(),
        "duration_s": 60.0, "score": 50.0,
        "incidents": [{"type": "dead-air", "severity": "critical", "at_s": 12.0,
                       "detail": "TEST ALERT: no real call; silence=6.00s from "
                                 "t=12.00s to t=18.00s"}],
        "alerts_fired": [], "report_path": None, "scored_at": _now_iso(),
    }


def run_test_alert(rules_path: str, *, dest: Optional[Destinations] = None,
                   fmt: str = "text") -> int:
    """Send one clearly labelled test alert per rule and channel, so the wiring
    is proven before a real call needs it. PagerDuty gets a trigger followed
    by an immediate resolve of the same test dedup key. Exit 0 when every send
    was delivered, 1 when any failed."""
    rules = load_rules(rules_path)
    dest = dest or Destinations()
    dest.require(rules)
    event = _synthetic_event()
    rows, ok_all = [], True
    for rule in rules:
        for ch in rule["channels"]:
            if ch == "slack":
                ok = _notify.post_notification(
                    dest.slack_url, slack_message(rule, event, None, "TEST: "))
            elif ch == "pagerduty":
                trule = dict(rule, id=f"test-{rule['id']}")
                ok = _notify.post_notification(
                    dest.pagerduty_url,
                    pagerduty_event("trigger", trule, event["agent_id"],
                                    dest.pagerduty_key, event))
                if ok:
                    ok = _notify.post_notification(
                        dest.pagerduty_url,
                        pagerduty_event("resolve", trule, event["agent_id"],
                                        dest.pagerduty_key))
            else:
                ok = _send_channel(dest, "webhook", rule, event, None)
            ok_all = ok_all and ok
            rows.append({"rule": rule["id"], "channel": ch, "delivered": ok})
    if fmt == "json":
        print(_errors.safe_json_dumps({"kind": "monitor-test-alert",
                                       "results": rows}, indent=2))
    else:
        for r in rows:
            print(f"  {r['rule']} -> {r['channel']}: "
                  f"{'delivered' if r['delivered'] else 'FAILED'}")
        print("hotato monitor test-alert: "
              + ("every test alert was delivered" if ok_all
                 else "at least one test alert failed (see the lines above)"))
    return 0 if ok_all else 1


def run_monitor_pin(call_id: str, *, store: str = "hotato-monitor",
                    incident: Optional[int] = None, out_dir: str = "contracts",
                    expect: Optional[str] = None,
                    contract_id: Optional[str] = None,
                    reviewer: Optional[str] = None, force: bool = False) -> dict:
    """Turn one monitored call into a failure contract through the existing pin
    path. ``incident`` is the 1-based rank; omitted pins the call's top
    critical incident."""
    from . import pincmd as _pincmd

    st = Store(store).load_state()
    apx = st["index"].get(call_id)
    if not apx:
        raise ValueError(
            f"call {call_id!r} has no scored recording in {store!r}; run "
            "hotato monitor run first, or check the id against the event "
            "files in <store>/events.")
    ref = apx if incident is None else f"{apx}#{int(incident)}"
    return _pincmd.run_pin(
        ref, from_dir=os.path.join(os.path.abspath(store), "autopsy"),
        out_dir=out_dir, expect=expect, contract_id=contract_id,
        reviewer=reviewer, force=force)
