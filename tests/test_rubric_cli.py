"""``hotato rubric run`` / ``hotato rubric calibrate`` -- CLI surface.

Deterministic + network-free: the CLI's judge is monkeypatched to an injected
FAKE judge (canned responses), except where a path is refused BEFORE any network
touch (egress opt-in). The live model path is proven in test_rubric.py's
``test_ollama_judge_live``.
"""
import json

import pytest

from hotato import cli
from tests.test_rubric import FakeJudge, _fail, _pass


def _run(argv):
    return cli.main(argv)


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    # keep the default cache under a per-test tmp dir, never ~/.hotato
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def _rubrics_file(tmp_path):
    return _write(tmp_path, "rubrics.json", {"version": 1, "rubrics": [
        {"id": "polite", "kind": "judge_rubric", "dimension": "conversation",
         "criterion": "was the agent polite?", "evidence": ["transcript"],
         "evaluation": {"repetitions": 1, "confidence_required": 0.5}}]})


def _transcript(tmp_path):
    return _write(tmp_path, "t.json",
                  [{"role": "agent", "text": "thank you so much"}])


def _inject(monkeypatch, responses):
    judge = FakeJudge(responses)
    monkeypatch.setattr(cli, "_build_judge", lambda args: judge)
    return judge


def test_rubric_run_advisory_pass(tmp_path, capsys, monkeypatch):
    _inject(monkeypatch, [_pass("polite tone")])
    code = _run(["rubric", "run", "--rubrics", _rubrics_file(tmp_path),
                 "--transcript", _transcript(tmp_path), "--format", "json"])
    env = json.loads(capsys.readouterr().out)
    assert code == 0                       # advisory: never gates
    assert env["schema"] == "rubric.v1"
    assert env["advisory"] is True
    r = env["results"][0]
    assert r["status"] == "PASS" and r["deterministic"] is False
    assert r["judge"]["model"] == "fake-judge-1b"
    assert "overall_score" not in json.dumps(env)


def test_rubric_run_advisory_fail_does_not_gate(tmp_path, capsys, monkeypatch):
    _inject(monkeypatch, [_fail()])
    code = _run(["rubric", "run", "--rubrics", _rubrics_file(tmp_path),
                 "--transcript", _transcript(tmp_path), "--format", "json"])
    assert code == 0                       # a FAIL is advisory by default
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "FAIL"


def test_rubric_run_gate_flips_exit_on_fail(tmp_path, capsys, monkeypatch):
    _inject(monkeypatch, [_fail()])
    code = _run(["rubric", "run", "--rubrics", _rubrics_file(tmp_path),
                 "--transcript", _transcript(tmp_path), "--gate", "--format", "json"])
    env = json.loads(capsys.readouterr().out)
    assert code == 1 and env["gated"] is True


def test_rubric_run_missing_transcript_is_inconclusive(tmp_path, capsys, monkeypatch):
    judge = _inject(monkeypatch, [_pass()])
    code = _run(["rubric", "run", "--rubrics", _rubrics_file(tmp_path),
                 "--format", "json"])   # no --transcript
    env = json.loads(capsys.readouterr().out)
    assert code == 0
    assert env["results"][0]["status"] == "INCONCLUSIVE"
    assert judge.calls == 0             # no model call on missing evidence


def test_rubric_run_cache_replay_is_cached(tmp_path, capsys, monkeypatch):
    rf, tf = _rubrics_file(tmp_path), _transcript(tmp_path)
    _inject(monkeypatch, [_pass("first")])
    _run(["rubric", "run", "--rubrics", rf, "--transcript", tf, "--format", "json"])
    capsys.readouterr()
    # a fresh (different) fake would answer FAIL, but the cache hit must win
    _inject(monkeypatch, [_fail("second")])
    _run(["rubric", "run", "--rubrics", rf, "--transcript", tf, "--format", "json"])
    env = json.loads(capsys.readouterr().out)
    r = env["results"][0]
    assert r["judge"]["cached"] is True and r["status"] == "PASS"


def test_rubric_run_no_cache_surfaces_drift(tmp_path, capsys, monkeypatch):
    rf, tf = _rubrics_file(tmp_path), _transcript(tmp_path)
    _inject(monkeypatch, [_pass()])
    _run(["rubric", "run", "--rubrics", rf, "--transcript", tf, "--format", "json"])
    capsys.readouterr()
    _inject(monkeypatch, [_fail("changed")])
    _run(["rubric", "run", "--rubrics", rf, "--transcript", tf, "--no-cache",
          "--format", "json"])
    r = json.loads(capsys.readouterr().out)["results"][0]
    assert r["judge"]["drift"]["cached_status"] == "PASS"
    assert r["judge"]["drift"]["fresh_status"] == "FAIL"


def test_rubric_run_hosted_egress_refused(tmp_path):
    # NO monkeypatch: this is refused BEFORE any network touch (exit 2).
    code = _run(["rubric", "run", "--rubrics", _rubrics_file(tmp_path),
                 "--transcript", _transcript(tmp_path),
                 "--judge-provider", "hosted",
                 "--judge-endpoint", "https://api.example.com/v1"])
    assert code == 2


def test_rubric_run_malformed_rubrics_is_usage_error(tmp_path):
    bad = _write(tmp_path, "bad.json", {"version": 1, "rubrics": [
        {"id": "x", "kind": "judge_rubric"}]})  # no criterion
    code = _run(["rubric", "run", "--rubrics", bad,
                 "--transcript", _transcript(tmp_path)])
    assert code == 2


def test_rubric_calibrate_writes_reproducible_artifact(tmp_path, monkeypatch):
    labeled = tmp_path / "labeled"
    labeled.mkdir()
    (labeled / "a.json").write_text(json.dumps({
        "rubric": {"id": "polite", "kind": "judge_rubric",
                   "criterion": "was the agent polite?", "evidence": ["transcript"],
                   "evaluation": {"repetitions": 1, "confidence_required": 0.5}},
        "transcript": [{"role": "agent", "text": "thank you"}],
        "label": "pass", "split": "held_out"}), encoding="utf-8")
    monkeypatch.setattr(cli, "_build_judge", lambda args: FakeJudge([_pass()]))
    out = str(tmp_path / "agreement.json")
    code = _run(["rubric", "calibrate", "--labeled", str(labeled), "--out", out])
    assert code == 0
    art = json.loads(open(out).read())
    assert art["schema"] == "hotato.rubric-calibration.v1"
    assert art["agreement"] == 1.0
    assert "overall_score" not in json.dumps(art)


def test_rubric_command_requires_subcommand():
    # a required subparser: argparse exits 2 (usage) when none is given, exactly
    # like `hotato scenario` / `hotato contract` with no subcommand.
    with pytest.raises(SystemExit) as exc:
        _run(["rubric"])
    assert exc.value.code == 2
