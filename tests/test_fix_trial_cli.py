"""End-to-end fix-trial gates through the real CLI (hotato fix trial).

Locks in both the refusals AND the happy path: a legit before(fail)/after(pass)
trial with the same scripted stimulus must reach 'improved' at PAIRED tier, or a
working barge-in fix would be stuck at 'inconclusive' (regression guard for the
trust swap-heuristic false positive on successful fixes)."""
import copy
import json
import shutil

from hotato import cli, core
from hotato import patch as _patch
from tests import _trial_audio as ta
from tests import test_fix_trial as _T


def _setup(tmp_path):
    scen = tmp_path / "scen"; bdir = tmp_path / "before"; adir = tmp_path / "after"
    tdir = tmp_path / "tampered"
    for d in (scen, bdir, adir, tdir):
        d.mkdir()
    json.dump({"id": "f1-yield", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0, "max_talk_over_sec": 1.0}},
              open(scen / "f1-yield.json", "w"))
    json.dump({"id": "f2-hold", "caller_onset_sec": 2.0, "expected": {"yield": False}},
              open(scen / "f2-hold.json", "w"))
    ta.talkover_call(str(bdir / "f1-yield.example.wav"))
    ta.yielded_to_backchannel_call(str(bdir / "f2-hold.example.wav"))
    ta.yielding_call(str(adir / "f1-yield.example.wav"))
    ta.holding_call(str(adir / "f2-hold.example.wav"))
    before = core.run_suite(scenarios_dir=str(scen), audio_dir=str(bdir), suffix=".example.wav")
    after = core.run_suite(scenarios_dir=str(scen), audio_dir=str(adir), suffix=".example.wav")
    json.dump(before, open(bdir / "run.json", "w"))
    json.dump(after, open(adir / "run.json", "w"))
    tamp = copy.deepcopy(before)
    for e in tamp["events"]:
        e["verdict"]["passed"] = True
    tamp["summary"] = {"events": 2, "passed": 2, "failed": 0, "regression": False}
    for e in before["events"]:
        n = e["audio_provenance"]["sides"][0]["path"]
        shutil.copy(str(bdir / n), str(tdir / n))
    json.dump(tamp, open(tdir / "run.json", "w"))
    patch_path = tmp_path / "patch.json"
    json.dump(_patch.build_patch(_T._config_plan(), source="fixplan.json"),
              open(patch_path, "w"))
    return str(patch_path), str(bdir), str(adir), str(tdir)


def _trial(capsys, patch_path, before, after):
    code = cli.main(["fix", "trial", patch_path, "--name", "staging",
                     "--before", before, "--after", after, "--battery", before,
                     "--min-n", "1", "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def test_legit_trial_reaches_improved_paired(tmp_path, capsys):
    patch_path, before, after, _ = _setup(tmp_path)
    code, d = _trial(capsys, patch_path, before, after)
    assert d["verdict"] == "improved"
    assert d["evidence"]["tier"] >= 3          # PAIRED
    assert code == 0


def test_tampered_verdict_is_refused(tmp_path, capsys):
    patch_path, before, _after, tdir = _setup(tmp_path)
    code, d = _trial(capsys, patch_path, before, tdir)
    assert d["verdict"] == "refused"
    r = d.get("refusal")
    assert (r.get("kind") if isinstance(r, dict) else r) == "score_mismatch" \
        or d.get("refusal_kind") == "score_mismatch"


def test_same_audio_is_refused(tmp_path, capsys):
    patch_path, before, _after, _ = _setup(tmp_path)
    code, d = _trial(capsys, patch_path, before, before)
    assert d["verdict"] == "refused"
    r = d.get("refusal")
    assert (r.get("kind") if isinstance(r, dict) else r) == "same_audio" \
        or d.get("refusal_kind") == "same_audio"
