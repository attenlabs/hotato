"""``hotato explain`` CLI: exit codes (0 nothing to explain, 1 attribution or
refusal produced, 2 usage error), --format json, --html writing a
self-contained report, and the machine shape validating against
schema/explain.v1.json."""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato.core import run_suite

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _envelope(events, stack="generic"):
    return {"tool": "hotato", "schema_version": "1", "mode": "suite",
            "stack": stack, "offline": True, "events": events, "exit_code": 1}


def _event(event_id, *, expected_yield, did_yield, passed, reasons=(),
           seconds_to_yield=None, talk_over_sec=0.0):
    return {
        "event_id": event_id, "scenario_id": event_id, "title": event_id,
        "category": "should_yield" if expected_yield else "should_not_yield",
        "expected_yield": expected_yield,
        "verdict": {"passed": passed, "did_yield": did_yield,
                   "seconds_to_yield": seconds_to_yield,
                   "talk_over_sec": talk_over_sec, "reasons": list(reasons)},
        "measurements": {"caller_onset_sec": 2.0, "agent_talking_at_onset": True},
        "signals": {"barge_in": {"did_yield": did_yield,
                                 "time_to_yield_sec": seconds_to_yield,
                                 "talk_over_sec": talk_over_sec},
                   "latency": {"response_gap_sec": None,
                              "premature_start_sec": None}},
        "fix": None,
    }


MISSED = dict(expected_yield=True, did_yield=False, passed=False,
              reasons=["expected the agent to yield but it kept talking"],
              talk_over_sec=2.5)
PASS_HOLD = dict(expected_yield=False, did_yield=False, passed=True)


def test_no_source_is_a_usage_error():
    with pytest.raises(SystemExit):
        cli.main(["explain"])


def test_missing_file_is_exit_2():
    assert cli.main(["explain", "/no/such/file.json"]) == 2


def test_non_envelope_json_is_exit_2(tmp_path):
    path = _write(tmp_path, "junk.json", {"nope": 1})
    assert cli.main(["explain", path]) == 2


def test_no_failures_is_exit_0(tmp_path, capsys):
    path = _write(tmp_path, "r.json", _envelope([_event("b", **PASS_HOLD)]))
    rc = cli.main(["explain", path])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to explain" in out


def test_attribution_or_refusal_is_exit_1(tmp_path, capsys):
    path = _write(tmp_path, "r.json",
                  _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]))
    rc = cli.main(["explain", path])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ATTRIBUTION" in out


def test_format_json_prints_the_full_explanation(tmp_path, capsys):
    path = _write(tmp_path, "r.json",
                  _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]))
    rc = cli.main(["explain", path, "--format", "json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "hotato.explain.v1"
    assert payload["input_kind"] == "run_envelope"
    assert payload["attributions"][0]["type"] == "missed_real_interruption"


def test_html_writes_a_self_contained_report(tmp_path):
    path = _write(tmp_path, "r.json",
                  _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]))
    out_html = tmp_path / "explain.html"
    rc = cli.main(["explain", path, "--html", str(out_html)])
    assert rc == 1
    text = out_html.read_text(encoding="utf-8")
    assert "<html" in text
    assert "hotato explain" in text


def test_dispatches_to_a_contract_bundle(tmp_path, capsys):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "cli-1",
        "--onset", "2.40", "--expect", "hold", "--out", str(tmp_path),
    ])
    assert rc == 0
    capsys.readouterr()  # drain contract create's own output
    rc2 = cli.main(["explain", str(tmp_path / "cli-1.hotato"), "--format", "json"])
    assert rc2 == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["input_kind"] == "contract_bundle"
    assert payload["refusals"]


def test_dispatches_to_a_candidate_ref(tmp_path):
    doc = {
        "tool": "hotato", "kind": "analyze", "stack": "generic",
        "candidates": [{
            "source": "calls/call_x.wav", "kind": "long_response_gap",
            "t_sec": 3.0, "salience": 1.0,
            "durations": {"gap_sec": 2.0}, "agent_reaction": {},
        }],
        "total_candidates": 1,
    }
    path = _write(tmp_path, "sweep.json", doc)
    rc = cli.main(["explain", f"{path}#1", "--format", "json"])
    assert rc == 1


# --- schema validation ---------------------------------------------------------

def test_explanation_validates_against_the_shipped_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "explain.v1.json")
        .read_text(encoding="utf-8"))

    from hotato import explain as ex

    root = resources.files("hotato").joinpath("data", "demo", "failing")
    funnel_env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                           audio_dir=str(root.joinpath("audio")))
    path1 = _write(tmp_path, "funnel.json", funnel_env)
    path2 = _write(tmp_path, "clean.json",
                   _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]))

    for path in (path1, path2):
        explanation = ex.explain(path)
        jsonschema.validate(explanation, schema)


def test_schema_rejects_a_bad_fixability(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "explain.v1.json")
        .read_text(encoding="utf-8"))
    from hotato import explain as ex

    path = _write(tmp_path, "r.json",
                  _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]))
    explanation = ex.explain(path)
    explanation["attributions"][0]["fixability"] = "auto_apply"  # not allowed
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(explanation, schema)
