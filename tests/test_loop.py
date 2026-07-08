"""hotato loop: one-command orchestration of the closed loop, with memory.

Covers the load-bearing behaviours from the spec:

* the loop ADVANCES and PERSISTS state across two runs: run 1 over a folder
  discovers candidate moments (awaiting_label); once the human has labeled
  fixtures, run 2 plans a fix (awaiting_verify); a third run re-reports from
  memory without redoing work;
* it NEVER auto-labels (it creates no fixtures of its own) and NEVER auto-applies
  (the plan it writes never mutates a platform);
* a first run with no folder, and a corrupt state file, are clean usage errors.

Discovery uses the bundled packaged fixtures (present in every wheel/sdist), so
the test never depends on the heavy repo corpus.
"""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources

import pytest

from hotato import cli
from hotato import loop as _loop


def _bundled_wavs(dst, names):
    d = resources.files("hotato").joinpath("data", "audio")
    picked = []
    for p in sorted(d.iterdir(), key=lambda x: x.name):
        if any(n in p.name for n in names) and p.name.endswith(".wav"):
            shutil.copy(str(p), os.path.join(dst, p.name))
            picked.append(p.name)
    return picked


def _copy_demo_fixtures(dst):
    """Copy the packaged demo failing battery in as if a human had labeled it
    (DIR/scenarios/*.json + DIR/audio/*.wav)."""
    demo = resources.files("hotato").joinpath("data", "demo", "failing")
    os.makedirs(os.path.join(dst, "scenarios"), exist_ok=True)
    os.makedirs(os.path.join(dst, "audio"), exist_ok=True)
    for sub in ("scenarios", "audio"):
        src = demo.joinpath(sub)
        for p in src.iterdir():
            shutil.copy(str(p), os.path.join(dst, sub, p.name))


@pytest.fixture()
def recordings(tmp_path):
    d = tmp_path / "recordings"
    d.mkdir()
    got = _bundled_wavs(str(d), ["02-backchannel", "07-echo"])
    assert got, "expected bundled example wavs to copy"
    return str(d)


# --- the two-run advance + persistence --------------------------------------

def test_loop_advances_and_persists_across_two_runs(recordings, tmp_path):
    state = str(tmp_path / ".hotato" / "loop-state.json")

    # Run 1: discovery -> awaiting_label
    r1, code1 = _loop.run_loop(recordings, state_path=state)
    assert code1 == 0
    assert r1["stage"] == "awaiting_label"
    assert r1["advanced"] is True
    assert r1["run"] == 1
    assert r1["discovery"]["total_candidates"] >= 1
    assert "awaiting your label" in r1["message"]
    assert os.path.exists(state)  # persisted

    # The human labels fixtures (loop never does this itself).
    fixtures = str(tmp_path / "tests" / "hotato")
    os.makedirs(fixtures)
    _copy_demo_fixtures(fixtures)

    # Run 2: labeled fixtures present -> plan -> awaiting_verify
    r2, code2 = _loop.run_loop(recordings, fixtures_dir=fixtures, state_path=state)
    assert code2 == 0
    assert r2["stage"] == "awaiting_verify"
    assert r2["advanced"] is True
    assert r2["run"] == 2
    assert r2["planning"]["ran_fixtures"] == 2
    assert os.path.exists(r2["planning"]["plan_path"])

    # State remembers the whole history and the run counter advanced.
    st = json.loads(open(state, encoding="utf-8").read())
    assert st["schema"] == "hotato.loop-state.v1"
    assert st["stage"] == "awaiting_verify"
    assert [(h["run"], h["stage"]) for h in st["history"]] == [
        (1, "awaiting_label"), (2, "awaiting_verify")]

    # Run 3: nothing new -> re-report from memory, no re-work.
    r3, code3 = _loop.run_loop(recordings, fixtures_dir=fixtures, state_path=state)
    assert code3 == 0
    assert r3["stage"] == "awaiting_verify"
    assert r3["advanced"] is False
    assert r3["run"] == 3


# --- honesty: no auto-label, no auto-apply ----------------------------------

def test_loop_never_auto_labels(recordings, tmp_path):
    state = str(tmp_path / "state.json")
    fixtures = str(tmp_path / "fixtures")
    os.makedirs(os.path.join(fixtures, "scenarios"))
    r, _ = _loop.run_loop(recordings, fixtures_dir=fixtures, state_path=state)
    # discovery ran, but the loop wrote no scenarios of its own
    assert r["stage"] == "awaiting_label"
    assert os.listdir(os.path.join(fixtures, "scenarios")) == []


def test_loop_plan_never_mutates_a_platform(recordings, tmp_path):
    state = str(tmp_path / ".hotato" / "state.json")
    fixtures = str(tmp_path / "fixtures")
    os.makedirs(fixtures)
    _copy_demo_fixtures(fixtures)
    # go straight to planning (human labeled before the first loop run)
    r, code = _loop.run_loop(recordings, fixtures_dir=fixtures, state_path=state)
    assert code == 0
    assert r["stage"] == "awaiting_verify"
    plan = json.loads(open(r["planning"]["plan_path"], encoding="utf-8").read())
    assert plan["platform_mutation"]["performed"] is False
    assert plan["approval"]["production_apply"] is False
    # the loop points at hotato patch (a human step), it does not apply
    assert any("hotato patch" in cmd for cmd in r["next"])


def test_loop_message_for_a_config_fix_awaiting_verify():
    # Unit-check the planning-stage message for a propose_one_step decision.
    state = {
        "stage": "awaiting_verify",
        "planning": {"decision": "propose_one_step", "fixes_awaiting_verify": 1,
                     "plan_path": "/x/fixplan.json"},
    }
    msg, nxt = _loop._message("awaiting_verify", state)
    assert "awaiting verify" in msg
    assert any("hotato patch" in c for c in nxt)
    assert any("hotato verify" in c for c in nxt)


# --- usage errors -----------------------------------------------------------

def test_first_run_without_a_folder_is_a_usage_error(tmp_path):
    state = str(tmp_path / "state.json")
    with pytest.raises(ValueError):
        _loop.run_loop(None, state_path=state)


def test_corrupt_state_file_is_rejected(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        _loop.run_loop(str(tmp_path), state_path=str(state))


def test_non_loop_state_file_is_rejected(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"schema": "something.else"}), encoding="utf-8")
    with pytest.raises(ValueError):
        _loop.run_loop(str(tmp_path), state_path=str(state))


# --- CLI --------------------------------------------------------------------

def test_cli_loop_json_and_text(recordings, tmp_path, capsys):
    state = str(tmp_path / "state.json")
    code = cli.main(["loop", recordings, "--state", state, "--format", "json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "loop"
    assert doc["stage"] == "awaiting_label"

    code = cli.main(["loop", recordings, "--state", state])
    assert code == 0
    text = capsys.readouterr().out
    assert "hotato loop" in text
    assert "no auto-apply" in text


def test_cli_loop_first_run_no_folder_exits_2(tmp_path):
    assert cli.main(["loop", "--state", str(tmp_path / "s.json")]) == 2
