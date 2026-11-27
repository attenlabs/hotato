"""``hotato monitor``: score-on-arrival, rules, alert channels, pin.

Fully offline: the vendor side is a fake lister/fetcher (the capture adapters'
own seams) copying bundled example recordings, and every alert goes to a local
HTTP receiver on 127.0.0.1 that stands in for Slack, PagerDuty and a webhook.
"""

import json
import math
import os
import random
import shutil
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hotato import cli
from hotato import monitor as mon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO = os.path.join(ROOT, "examples", "autopsy", "audio")
BARGE = os.path.join(AUDIO, "autopsy-01-barge-in-say-do.example.wav")
DEAD = os.path.join(AUDIO, "autopsy-02-latency-dead-air.example.wav")
MONO = os.path.join(AUDIO, "autopsy-02-latency-dead-air.caller.wav")

EVENT_KEYS = ["schema", "call_id", "stack", "agent_id", "started_at",
              "duration_s", "score", "incidents", "alerts_fired",
              "report_path", "scored_at"]


def _clean_wav(path):
    sr, rng = 16000, random.Random(3)
    frames = []
    for i in range(sr * 10):
        t = i / sr

        def tone(f, on):
            if on:
                return int((0.4 * math.sin(2 * math.pi * f * t)
                            + 0.1 * rng.uniform(-1, 1)) * 32767)
            return int(rng.uniform(-1, 1) * 20)
        frames += [tone(220, 0.5 < t < 3.0 or 6.5 < t < 8.5),
                   tone(330, 3.5 < t < 6.0 or 8.9 < t < 9.8)]
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % len(frames), *frames))


class Receiver:
    """A local HTTP server recording every POST body; ``status`` is settable."""

    def __init__(self):
        self.posts = []
        self.status = 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.posts.append((self.path, json.loads(self.rfile.read(n))))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def bodies(self, path):
        return [b for p, b in self.posts if p == path]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def rx():
    r = Receiver()
    yield r
    r.close()


class FakeVendor:
    """Stands in for capture.list_calls / capture.fetch_one."""

    def __init__(self, tmp_path):
        self.calls = {}      # id -> (created, agent, wav path or Exception)
        self.fetches = []
        self.tmp = tmp_path

    def add(self, cid, created, agent, wav):
        self.calls[cid] = (created, agent, wav)

    def lister(self, stack, creds, since=None, limit=50):
        out = [{"id": c, "created": v[0], "agent_id": v[1]}
               for c, v in self.calls.items()
               if since is None or v[0] >= since]
        return sorted(out, key=lambda i: -i["created"])[:limit]

    def fetcher(self, stack, ident, creds, dest, allow_mono=False):
        self.fetches.append(ident)
        wav = self.calls[ident][2]
        if isinstance(wav, Exception):
            raise wav
        shutil.copyfile(wav, dest)
        return dest


def _rules(tmp_path, rules):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(rules))
    return str(p)


def _monitor(tmp_path, vendor, rules, dest, **kw):
    store = mon.Store(str(tmp_path / "store"))
    return mon.Monitor("vapi", store, rules, dest, lister=vendor.lister,
                       fetcher=vendor.fetcher, backfill="30d",
                       clock=kw.pop("clock", None) or (lambda: 2_000_000_000.0),
                       **kw), store


def _event(store, cid):
    with open(store.event_path(cid)) as fh:
        return json.load(fh)


# --- rules -------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"id": "a", "when": {"metric": "nope", "op": "<", "value": 1}, "channels": ["slack"]},
    {"id": "a", "when": {"metric": "score", "op": "=", "value": 1}, "channels": ["slack"]},
    {"id": "a", "when": {"metric": "score", "op": "<", "value": "x"}, "channels": ["slack"]},
    {"id": "a", "when": {"incident_type": "dead-air", "severity_at_least": "bad"}, "channels": ["slack"]},
    {"id": "a", "when": {"metric": "score", "op": "<", "value": 1}, "channels": ["sms"]},
    {"id": "a", "when": {"metric": "score", "op": "<", "value": 1}, "channels": []},
    {"id": "a", "when": {"metric": "score", "op": "<", "value": 1}, "channels": ["slack"],
     "window": {"calls": 3, "min_hits": 4}},
    {"id": "a", "when": {}, "channels": ["slack"]},
    {"when": {"metric": "score", "op": "<", "value": 1}, "channels": ["slack"]},
])
def test_bad_rules_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        mon.load_rules(_rules(tmp_path, [bad]))


def test_duplicate_rule_ids_refused(tmp_path):
    r = {"id": "a", "when": {"metric": "score", "op": "<", "value": 1}, "channels": ["slack"]}
    with pytest.raises(ValueError, match="twice"):
        mon.load_rules(_rules(tmp_path, [r, r]))


def test_rules_accept_object_wrapper_and_defaults(tmp_path):
    r = {"id": "a", "when": {"incident_type": "Dead-Air"}, "channels": ["slack", "slack"]}
    rules = mon.load_rules(_rules(tmp_path, {"rules": [r]}))
    assert rules[0]["when"] == {"incident_type": "dead-air", "severity_at_least": "warning"}
    assert rules[0]["channels"] == ["slack"]
    assert rules[0]["cooldown_min"] == mon.DEFAULT_COOLDOWN_MIN


def test_missing_destination_is_refused(tmp_path, monkeypatch):
    for v in (mon.ENV_SLACK, mon.ENV_PAGERDUTY, mon.ENV_WEBHOOK):
        monkeypatch.delenv(v, raising=False)
    rules = mon.load_rules(_rules(tmp_path, [
        {"id": "a", "when": {"metric": "score", "op": "<", "value": 1},
         "channels": ["slack"]}]))
    with pytest.raises(ValueError, match="no destination"):
        mon.Destinations().require(rules)


def test_non_http_destination_is_refused():
    with pytest.raises(ValueError):
        mon.Destinations(slack_url="file:///etc/passwd")


def test_parse_every():
    assert mon.parse_every("5m") == 300 and mon.parse_every("30s") == 30
    with pytest.raises(ValueError):
        mon.parse_every("soon")


# --- scoring, events, cursor -------------------------------------------------

def test_cycle_writes_schema_exact_events_and_is_idempotent(tmp_path):
    v = FakeVendor(tmp_path)
    v.add("c1", 1_999_999_000.0, "asst-1", BARGE)
    v.add("c2", 1_999_999_100.0, "asst-1", DEAD)
    m, store = _monitor(tmp_path, v, [], mon.Destinations())
    s = m.run_cycle()
    assert s["scored"] == 2 and v.fetches == ["c1", "c2"]  # oldest first
    ev = _event(store, "c1")
    assert list(ev) == EVENT_KEYS
    assert ev["schema"] == "hotato.monitorevent.v1"
    assert ev["stack"] == "vapi" and ev["agent_id"] == "asst-1"
    assert ev["score"] == 0.0 and ev["duration_s"] == 12.0
    assert ev["started_at"].endswith("Z") and ev["scored_at"].endswith("Z")
    inc = ev["incidents"][0]
    assert list(inc) == ["type", "severity", "at_s", "detail"]
    assert inc["type"] == "barge-in" and inc["severity"] == "critical"
    assert os.path.isfile(ev["report_path"])
    # a second cycle, and a fresh Monitor on the same store, score nothing again
    m.run_cycle()
    m2, _ = _monitor(tmp_path, v, [], mon.Destinations())
    s3 = m2.run_cycle()
    assert v.fetches == ["c1", "c2"] and s3["scored"] == 0
    assert store.load_state()["cursor"] == 1_999_999_100.0


def test_restart_resumes_from_cursor_and_scores_only_new_calls(tmp_path):
    v = FakeVendor(tmp_path)
    v.add("c1", 1_999_999_000.0, None, BARGE)
    m, store = _monitor(tmp_path, v, [], mon.Destinations())
    m.run_cycle()
    seen_since = []
    orig = v.lister
    v.lister = lambda *a, **k: (seen_since.append(k.get("since")), orig(*a, **k))[1]
    v.add("c2", 1_999_999_500.0, None, DEAD)
    m2, _ = _monitor(tmp_path, v, [], mon.Destinations())
    m2.run_cycle()
    assert seen_since == [1_999_999_000.0]
    assert v.fetches == ["c1", "c2"]


def test_not_yet_recorded_call_is_retried_then_recorded_unscored(tmp_path):
    v = FakeVendor(tmp_path)
    v.add("c1", 1_999_999_000.0, None, ValueError("no recording yet"))
    m, store = _monitor(tmp_path, v, [], mon.Destinations())
    for _ in range(mon.MAX_FETCH_ATTEMPTS - 1):
        assert m.run_cycle()["retry"] == 1
        assert not store.has_event("c1")
    v.calls["c1"] = (1_999_999_000.0, None, BARGE)   # recording appears
    assert m.run_cycle()["scored"] == 1
    assert _event(store, "c1")["score"] == 0.0

    v.add("c2", 1_999_999_100.0, None, ValueError("never"))
    for _ in range(mon.MAX_FETCH_ATTEMPTS):
        m.run_cycle()
    ev = _event(store, "c2")
    assert ev["score"] is None and ev["incidents"] == [] and ev["report_path"] is None


def test_mono_call_is_best_effort_with_null_score(tmp_path):
    v = FakeVendor(tmp_path)
    v.add("m1", 1_999_999_000.0, None, MONO)
    rules = mon.load_rules(_rules(tmp_path, [
        {"id": "low", "when": {"metric": "score", "op": "<", "value": 90},
         "channels": ["webhook"]},
        {"id": "dead", "when": {"incident_type": "dead-air",
                                "severity_at_least": "critical"},
         "channels": ["webhook"]}]))
    rx = Receiver()
    try:
        m, store = _monitor(tmp_path, v, rules,
                            mon.Destinations(webhook_url=rx.url), allow_mono=True)
        m.run_cycle()
        ev = _event(store, "m1")
        assert ev["score"] is None
        assert ev["incidents"][0]["type"] == "dead-air"
        # no score on a mono call -> the score rule has no data; the incident rule fires
        assert ev["alerts_fired"] == ["dead"]
    finally:
        rx.close()


# --- channels ----------------------------------------------------------------

def test_slack_block_kit_message(tmp_path, rx):
    v = FakeVendor(tmp_path)
    v.add("call-xyz", 1_999_999_000.0, "asst-1", BARGE)
    rules = mon.load_rules(_rules(tmp_path, [
        {"id": "talk-over", "when": {"incident_type": "barge-in",
                                     "severity_at_least": "critical"},
         "channels": ["slack"]}]))
    m, store = _monitor(tmp_path, v, rules,
                        mon.Destinations(slack_url=rx.url + "/slack",
                                         report_url_base="https://reports.example/x"))
    m.run_cycle()
    (body,) = rx.bodies("/slack")
    text = json.dumps(body)
    assert "call-xyz" in body["text"] and "0" in body["text"]
    assert [b["type"] for b in body["blocks"]][0] == "header"
    assert "barge-in" in text and "CRITICAL" in text
    assert "https://reports.example/x/autopsy-apx-" in text
    assert _event(store, "call-xyz")["alerts_fired"] == ["talk-over"]


def test_pagerduty_trigger_dedup_key_then_resolve_when_rule_clears(tmp_path, rx):
    v = FakeVendor(tmp_path)
    v.add("bad", 1_999_999_000.0, "asst-1", DEAD)
    v.add("good", 1_999_999_100.0, "asst-1", _clean_path(tmp_path))
    rules = mon.load_rules(_rules(tmp_path, [
        {"id": "dead-air", "when": {"metric": "dead_air_s", "op": ">=", "value": 5},
         "channels": ["pagerduty"]}]))
    d = mon.Destinations(pagerduty_key="KEY123", pagerduty_url=rx.url + "/pd")
    m, store = _monitor(tmp_path, v, rules, d)
    m.run_cycle()
    trig, res = rx.bodies("/pd")
    assert trig["event_action"] == "trigger" and trig["routing_key"] == "KEY123"
    assert trig["dedup_key"] == "hotato-dead-air-asst-1"
    assert trig["payload"]["severity"] == "critical"
    assert "bad" in trig["payload"]["summary"]
    assert res["event_action"] == "resolve" and res["dedup_key"] == trig["dedup_key"]
    assert "payload" not in res
    assert _event(store, "bad")["alerts_fired"] == ["dead-air"]
    assert _event(store, "good")["alerts_fired"] == []


def _clean_path(tmp_path):
    p = str(tmp_path / "clean.wav")
    if not os.path.exists(p):
        _clean_wav(p)
    return p


def test_webhook_payload_and_secrets_never_logged(tmp_path, rx, capsys):
    v = FakeVendor(tmp_path)
    v.add("c1", 1_999_999_000.0, "asst-1", DEAD)
    rules = mon.load_rules(_rules(tmp_path, [
        {"id": "r", "when": {"metric": "score", "op": "<", "value": 90},
         "channels": ["webhook", "pagerduty"]}]))
    rx.status = 500   # every delivery fails -> logged, never raised
    d = mon.Destinations(webhook_url=rx.url + "/hook?token=SEKRET",
                         pagerduty_key="PDKEYSEKRET", pagerduty_url=rx.url + "/pd")
    m, store = _monitor(tmp_path, v, rules, d)
    m.run_cycle()
    err = capsys.readouterr().err
    assert "SEKRET" not in err and "PDKEYSEKRET" not in err
    assert _event(store, "c1")["alerts_fired"] == []   # nothing delivered
    rx.status = 200
    rx.posts.clear()
    v.add("c2", 1_999_999_100.0, "asst-1", DEAD)
    m.run_cycle()
    (hook,) = rx.bodies("/hook?token=SEKRET")
    assert hook["tool"] == "hotato" and hook["kind"] == "monitor-alert"
    assert hook["call_id"] == "c2" and hook["rule_id"] == "r"
    assert hook["agent_id"] == "asst-1" and hook["score"] == 0.0
    assert hook["incidents"][0]["type"] == "dead-air"


def test_cooldown_suppresses_repeat_alerts_then_allows(tmp_path, rx):
    v = FakeVendor(tmp_path)
    now = [2_000_000_000.0]
    rules = mon.load_rules(_rules(tmp_path, [
        {"id": "r", "when": {"metric": "score", "op": "<", "value": 90},
         "channels": ["webhook"], "cooldown_min": 10}]))
    m, store = _monitor(tmp_path, v, rules, mon.Destinations(webhook_url=rx.url + "/h"),
                        clock=lambda: now[0])
    v.add("c1", 1_999_999_000.0, "a", DEAD)
    v.add("c2", 1_999_999_001.0, "a", DEAD)
    m.run_cycle()
    assert len(rx.bodies("/h")) == 1                      # c2 inside the cooldown
    assert _event(store, "c2")["alerts_fired"] == []
    now[0] += 11 * 60
    v.add("c3", 1_999_999_002.0, "a", DEAD)
    m.run_cycle()
    assert len(rx.bodies("/h")) == 2
    # another agent has its own state
    v.add("c4", 1_999_999_003.0, "b", DEAD)
    m.run_cycle()
    assert len(rx.bodies("/h")) == 3


def test_window_rule_needs_min_hits(tmp_path, rx):
    v = FakeVendor(tmp_path)
    rules = mon.load_rules(_rules(tmp_path, [
        {"id": "rate", "when": {"incident_type": "any", "severity_at_least": "critical"},
         "window": {"calls": 3, "min_hits": 2}, "channels": ["webhook"],
         "cooldown_min": 0}]))
    m, store = _monitor(tmp_path, v, rules, mon.Destinations(webhook_url=rx.url + "/h"))
    clean = _clean_path(tmp_path)
    for i, wav in enumerate([DEAD, clean, DEAD]):
        v.add(f"c{i}", 1_999_999_000.0 + i, "a", wav)
    m.run_cycle()
    assert [_event(store, f"c{i}")["alerts_fired"] for i in range(3)] == [[], [], ["rate"]]
    assert len(rx.bodies("/h")) == 1


def test_calls_without_rules_are_scored_but_nothing_is_sent(tmp_path, rx):
    v = FakeVendor(tmp_path)
    v.add("c1", 1_999_999_000.0, None, DEAD)
    m, store = _monitor(tmp_path, v, [], mon.Destinations(webhook_url=rx.url))
    m.run_cycle()
    assert rx.posts == [] and _event(store, "c1")["alerts_fired"] == []


# --- pin ---------------------------------------------------------------------

def test_pin_makes_a_contract_from_a_monitored_call(tmp_path, monkeypatch, capsys):
    v = FakeVendor(tmp_path)
    v.add("c1", 1_999_999_000.0, None, BARGE)
    m, store = _monitor(tmp_path, v, [], mon.Destinations())
    m.run_cycle()
    out = tmp_path / "contracts"
    rc = cli.main(["monitor", "pin", "c1", "--store", store.root, "--out", str(out)])
    assert rc == 0
    assert "hotato prove" in capsys.readouterr().out
    assert any(out.iterdir())


def test_pin_unknown_call_exits_2(tmp_path, capsys):
    mon.Store(str(tmp_path / "s"))
    rc = cli.main(["monitor", "pin", "nope", "--store", str(tmp_path / "s")])
    assert rc == 2
    assert "no scored recording" in capsys.readouterr().err


# --- CLI ---------------------------------------------------------------------

def test_cli_run_once_via_injected_vendor(tmp_path, rx, monkeypatch, capsys):
    v = FakeVendor(tmp_path)
    v.add("c1", 1_999_999_000.0, "a", DEAD)
    rp = _rules(tmp_path, [{"id": "r", "when": {"metric": "score", "op": "<", "value": 90},
                            "channels": ["webhook"]}])
    rc = mon.run_monitor("vapi", once=True, rules_path=rp, store=str(tmp_path / "s"),
                         since="30d", dest=mon.Destinations(webhook_url=rx.url + "/h"),
                         lister=v.lister, fetcher=v.fetcher)
    assert rc == 0 and len(rx.bodies("/h")) == 1
    assert "1 scored" in capsys.readouterr().out


def test_cli_run_refuses_missing_key_and_missing_destination(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    assert cli.main(["monitor", "run", "--stack", "vapi", "--once",
                     "--store", str(tmp_path / "s")]) == 2
    assert "VAPI_API_KEY" in capsys.readouterr().err
    monkeypatch.delenv(mon.ENV_SLACK, raising=False)
    rp = _rules(tmp_path, [{"id": "r", "when": {"metric": "score", "op": "<", "value": 9},
                            "channels": ["slack"]}])
    assert cli.main(["monitor", "run", "--stack", "vapi", "--once", "--rules", rp,
                     "--store", str(tmp_path / "s")]) == 2
    assert "no destination" in capsys.readouterr().err


def test_cli_test_alert_delivers_to_every_channel(tmp_path, rx, monkeypatch, capsys):
    monkeypatch.setenv(mon.ENV_SLACK, rx.url + "/slack")
    monkeypatch.setenv(mon.ENV_WEBHOOK, rx.url + "/hook")
    monkeypatch.setenv(mon.ENV_PAGERDUTY, "K")
    monkeypatch.setenv(mon.ENV_PAGERDUTY_URL, rx.url + "/pd")
    rp = _rules(tmp_path, [{"id": "r1", "when": {"metric": "score", "op": "<", "value": 90},
                            "channels": ["slack", "pagerduty", "webhook"]}])
    assert cli.main(["monitor", "test-alert", "--rules", rp]) == 0
    assert "TEST" in json.dumps(rx.bodies("/slack")[0])
    trig, res = rx.bodies("/pd")
    assert trig["event_action"] == "trigger" and res["event_action"] == "resolve"
    assert trig["dedup_key"] == "hotato-test-r1-test-agent"
    assert rx.bodies("/hook")[0]["call_id"] == "test-call-000"
    assert "every test alert was delivered" in capsys.readouterr().out


def test_cli_test_alert_reports_failed_delivery(tmp_path, rx, monkeypatch, capsys):
    rx.status = 500
    monkeypatch.setenv(mon.ENV_WEBHOOK, rx.url + "/hook")
    rp = _rules(tmp_path, [{"id": "r1", "when": {"metric": "score", "op": "<", "value": 90},
                            "channels": ["webhook"]}])
    assert cli.main(["monitor", "test-alert", "--rules", rp]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_monitor_is_in_the_describe_manifest(capsys):
    assert cli.main(["describe", "--format", "json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    cmds = {c["name"]: c for c in manifest["subcommands"]}
    assert {s["name"] for s in cmds["monitor"]["subcommands"]} == {
        "monitor run", "monitor test-alert", "monitor pin"}


def test_call_score_is_per_call_voice_stability_score():
    from hotato.monitor import call_score
    assert call_score([]) == 100.0
    assert call_score([{"severity": "warning"}, {"severity": "warning"}]) == 100.0
    assert call_score([{"severity": "warning"}, {"severity": "critical"}]) == 0.0
