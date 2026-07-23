"""Fail-closed edge cases: "could not tell AT ALL" is never green.

Regressions pinned from the 2026-07-23 error-vs-fail audit, one per finding.
The stance under test is the stated one ("could not tell" is never green,
mirroring prove's inconclusive-never-green) applied to the edges where an
inconclusive outcome used to map to exit 0:

  1. a user-labelled battery (`hotato run --scenarios/--audio`) whose every
     event is not scorable -- or that holds zero scenarios -- exits 2, never 0;
  2. `hotato pull --score` over a pulled set with no scorable event exits 2;
  3. a judge that answers with an empty/garbage body twice is a judge that
     could not run: ERROR, never an "inconclusive" abstention vote;
  4. under gating, a judge ERROR with no rubric FAIL exits 2 (refuse), distinct
     from a scored FAIL's exit 1;
  5. `hotato drive` maps a fresh call with no scorable moment to exit 2
     (unusable evidence), distinct from a scored invariant FAIL's exit 1;
  6. `hotato pull` / `hotato sweep` exit non-zero when every listed recording
     failed to fetch (a vendor outage, not a completed pull).

The constraint in the OTHER direction is pinned too: a battery/set that holds
at least one scored result keeps its scored verdict semantics -- only
could-not-tell-at-all turns red (see also tests/test_not_scorable.py's
mixed-suite pin and tests/test_pull_sweep.py's one-bad-file skip pin).
"""

import json
import math
import os
import struct
import wave
from importlib import resources

import pytest

from hotato import assert_ as A
from hotato import capture as cap
from hotato import cli
from hotato import rubric as R
from hotato import test_run as TR
from hotato.core import process_exit_code, run_suite

# --- deterministic synthetic fixtures (same shape as test_not_scorable.py) ---

def _write_stereo(path, caller_segments, agent_segments, duration_sec=3.0, sr=16000):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(caller_segments, t) else 0
        a = int(0.35 * 32767 * math.sin(2 * math.pi * 330.0 * i / sr)) if _on(agent_segments, t) else 0
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _scenario(scen_dir, sid, category="should_yield", expect=True):
    (scen_dir / f"{sid}.json").write_text(
        json.dumps({"id": sid, "category": category,
                    "expected": {"yield": expect}}),
        encoding="utf-8",
    )


# =========================================================================
# Finding 1 (CRITICAL): all-not-scorable or EMPTY labelled battery -> exit 2
# =========================================================================

def test_battery_with_only_missing_audio_exits_2(tmp_path, capsys):
    # The audit repro: `hotato run --scenarios scen --audio audio` where the
    # audio dir exists but holds no wavs -- every event is not scorable
    # (missing audio), so the battery could not tell anything. Green here
    # turned a wrong --audio path / un-fetched CI artifacts into a pass.
    scen_dir = tmp_path / "scen"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()
    _scenario(scen_dir, "case1")

    code = cli.main(["run", "--scenarios", str(scen_dir),
                     "--audio", str(audio_dir)])
    assert code == 2
    out = capsys.readouterr().out
    assert "NOT SCORABLE" in out
    assert "process_exit_code=2" in out


def test_battery_with_zero_scenarios_is_refused(tmp_path):
    # The audit repro: `hotato run --scenarios empty --audio audio` scored
    # "0/0 events pass" and exited 0. A battery of nothing is refused, exactly
    # like prove's "a proof of nothing is refused".
    scen_dir = tmp_path / "empty"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()

    with pytest.raises(ValueError, match="no scenario"):
        run_suite(scenarios_dir=str(scen_dir), audio_dir=str(audio_dir))
    # through the CLI boundary that ValueError is the standard exit-2 path
    assert cli.main(["run", "--scenarios", str(scen_dir),
                     "--audio", str(audio_dir)]) == 2


def test_all_not_scorable_suite_envelope_maps_to_exit_2(tmp_path):
    scen_dir = tmp_path / "scen"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()
    _scenario(scen_dir, "zz-silent")
    # silent caller channel: present but not scorable
    _write_stereo(audio_dir / "zz-silent.example.wav", [], [(0.2, 2.8)])

    env = run_suite(scenarios_dir=str(scen_dir), audio_dir=str(audio_dir))
    assert env["summary"]["not_scorable"] == env["summary"]["events"] == 1
    assert env["exit_code"] == 0  # envelope stays schema-frozen 0|1
    assert process_exit_code(env) == 2


def test_mixed_battery_keeps_its_scored_verdict(tmp_path):
    # The constraint the other way: one scored pass + one not-scorable event
    # is a scored battery, and its scored verdict (0 here) stands.
    scen_dir = tmp_path / "scen"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()
    _scenario(scen_dir, "aa-valid")
    _write_stereo(audio_dir / "aa-valid.example.wav", [(1.0, 2.0)], [(0.0, 1.5)])
    _scenario(scen_dir, "zz-missing")  # no audio file: not scorable

    env = run_suite(scenarios_dir=str(scen_dir), audio_dir=str(audio_dir))
    assert env["summary"]["passed"] == 1
    assert env["summary"]["not_scorable"] == 1
    assert process_exit_code(env) == 0


# =========================================================================
# Finding 2 (MODERATE): pull --score over an all-unscorable set -> exit 2
# =========================================================================

def _fake_pull_with(files):
    def fake_pull(stack, creds, *, out_dir, ids=None, since=None, limit=50,
                  allow_mono=False, log=None):
        os.makedirs(out_dir, exist_ok=True)
        pulled = []
        for name, data in files:
            p = os.path.join(out_dir, name)
            with open(p, "wb") as fh:
                fh.write(data)
            pulled.append({"id": os.path.splitext(name)[0], "path": p})
        return {"stack": stack, "out_dir": out_dir, "listed": len(pulled),
                "pulled": pulled, "skipped": []}
    return fake_pull


def _not_scorable_wav_bytes(tmp_path):
    # silent caller channel, talking agent: scores as wholly not scorable
    p = _write_stereo(tmp_path / "notscorable.wav", [], [(0.2, 2.8)])
    with open(p, "rb") as fh:
        return fh.read()


def test_pull_score_all_not_scorable_set_exits_2(tmp_path, monkeypatch, capsys):
    # The audit repro: a pulled set in which EVERY recording is not scorable
    # (silent channel) exited 0 -- the same file scored via
    # `hotato run --stereo` exits 2.
    monkeypatch.setattr(cap, "pull", _fake_pull_with(
        [("vapi__x1.wav", _not_scorable_wav_bytes(tmp_path))]))
    rc = cli.main(["pull", "--stack", "vapi", "--api-key", "k",
                   "--out", str(tmp_path / "d"), "--score", "--format", "json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["score"]["exit_code"] == 2
    assert payload["score"]["recordings"][0]["not_scorable"] == 1


def test_pull_score_mixed_set_keeps_its_scored_verdict(tmp_path, monkeypatch):
    # One scored PASS beside a not-scorable recording: the scored verdict
    # semantics stand (exit 0), the not-scorable is reported per file.
    good = (resources.files("hotato")
            .joinpath("data", "audio", "01-hard-interruption.example.wav")
            .read_bytes())
    monkeypatch.setattr(cap, "pull", _fake_pull_with(
        [("vapi__a.wav", good),
         ("vapi__x1.wav", _not_scorable_wav_bytes(tmp_path))]))
    rc = cli.main(["pull", "--stack", "vapi", "--api-key", "k",
                   "--out", str(tmp_path / "d"), "--score"])
    assert rc == 0


def test_pull_score_corrupt_plus_not_scorable_set_exits_2(tmp_path, monkeypatch):
    # 1 corrupt (score skip) + 1 wholly-not-scorable: not one scorable event
    # anywhere in the set -> could not tell at all -> exit 2, never 0.
    monkeypatch.setattr(cap, "pull", _fake_pull_with(
        [("vapi__bad.wav", b"not a wav"),
         ("vapi__x1.wav", _not_scorable_wav_bytes(tmp_path))]))
    rc = cli.main(["pull", "--stack", "vapi", "--api-key", "k",
                   "--out", str(tmp_path / "d"), "--score"])
    assert rc == 2


# =========================================================================
# Finding 3 (MODERATE): an empty/garbage judge response is ERROR, not an
# "inconclusive" vote -- so it gates under --gate
# =========================================================================

class _CannedJudge(R.Judge):
    provider = "fake"

    def __init__(self, responses, *, model="fake-judge-1b"):
        self.model = model
        self._responses = list(responses)
        self.calls = 0

    def complete(self, system, user):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r

    def model_digest(self):
        return "cafef00d"


_RUBRIC = {"id": "polite", "kind": "judge_rubric",
           "criterion": "was the agent polite?", "evidence": ["transcript"],
           "evaluation": {"repetitions": 1, "confidence_required": 0.5}}
_TX = [{"role": "caller", "text": "this is frustrating"},
       {"role": "agent", "text": "thank you for your patience"}]


def test_empty_judge_response_is_error_and_gates(tmp_path):
    # The audit repro: EmptyJudge returning "" under gate=True used to become
    # an INCONCLUSIVE that stayed advisory (exit 0). A judge that never
    # produced a parseable verdict could not judge: ERROR, which gates.
    res = R.evaluate_rubric(_RUBRIC, transcript=_TX,
                            judge=_CannedJudge(["", ""]))
    assert res["status"] == "ERROR"
    assert "parseable" in res["rationale"]

    env = R.rubric_envelope([res], gate=True)
    assert env["exit_code"] != 0


def test_garbage_judge_response_is_error_after_the_repair_retry():
    judge = _CannedJudge(["garbage", "still garbage"])
    res = R.evaluate_rubric(_RUBRIC, transcript=_TX, judge=judge)
    assert judge.calls == 2  # the repair retry still runs first
    assert res["status"] == "ERROR"


def test_well_formed_inconclusive_verdict_stays_inconclusive_and_advisory():
    # The boundary: a model that RESPONDS with a well-formed abstention is a
    # judge that ran and could not decide -- INCONCLUSIVE, advisory even under
    # gate (a suite that wants to gate on that uses inconclusive_policy).
    res = R.evaluate_rubric(_RUBRIC, transcript=_TX,
                            judge=_CannedJudge(['{"verdict":"inconclusive"}']))
    assert res["status"] == "INCONCLUSIVE"
    assert R.rubric_envelope([res], gate=True)["exit_code"] == 0


# =========================================================================
# Finding 4 (MINOR): gated judge ERROR with no rubric FAIL -> exit 2 (refuse),
# distinct from a scored FAIL's exit 1
# =========================================================================

def _result(status):
    return {"id": f"r-{status.lower()}", "status": status}


def test_gated_error_only_exits_2_and_fail_exits_1():
    assert R.rubric_envelope([_result("FAIL")], gate=True)["exit_code"] == 1
    assert R.rubric_envelope([_result("ERROR")], gate=True)["exit_code"] == 2
    # a scored FAIL beside an ERROR keeps the scored verdict's exit 1
    both = R.rubric_envelope([_result("FAIL"), _result("ERROR")], gate=True)
    assert both["exit_code"] == 1
    # advisory stays advisory
    assert R.rubric_envelope([_result("ERROR")], gate=False)["exit_code"] == 0


def test_test_run_gate_judge_error_only_exits_2():
    doc = {
        "id": "t-gate", "inconclusive_policy": "report",
        "assertions": {"rubric": [dict(_RUBRIC)]},
        "success": {"required": ["no_rubric_failure"]},
    }
    ctx = A.build_context(transcript=_TX)
    boom = OSError("judge down")

    class _DownJudge(_CannedJudge):
        def complete(self, system, user):
            raise R.JudgeError("judge backend unreachable")

        def model_digest(self):
            raise R.JudgeError("judge backend unreachable")

    res = TR.evaluate_conversation_test(
        doc, ctx, agent_id="a", judge=_DownJudge([]), gate_judge=True)
    assert res["rubric"]["results"][0]["status"] == "ERROR"
    assert res["success"]["passed"] is False
    assert res["exit_code"] == 2
    del boom


def test_test_run_gate_judge_fail_still_exits_1():
    doc = {
        "id": "t-gate", "inconclusive_policy": "report",
        "assertions": {"rubric": [dict(_RUBRIC)]},
        "success": {"required": ["no_rubric_failure"]},
    }
    ctx = A.build_context(transcript=_TX)
    res = TR.evaluate_conversation_test(
        doc, ctx, agent_id="a",
        judge=_CannedJudge(['{"verdict":"fail","rationale":"x"}']),
        gate_judge=True)
    assert res["rubric"]["results"][0]["status"] == "FAIL"
    assert res["exit_code"] == 1


# =========================================================================
# Finding 5 (MINOR): drive's not_scorable outcome -> exit 2 (unusable fresh
# evidence), distinct from a scored invariant FAIL's exit 1
# =========================================================================

def test_drive_not_scorable_fresh_call_exits_2(tmp_path, monkeypatch, capsys):
    from tests.test_drive_cmd import _make_bundle, _Spy

    for var in ("HOTATO_DRIVE_OPT_IN", "VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID",
                "HOTATO_DRIVE_CUSTOMER_NUMBER", "VAPI_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    # the mocked live call returns a recording with a silent caller channel:
    # no scorable moment on the fresh call
    silent = _write_stereo(tmp_path / "silent-caller.wav", [], [(0.2, 2.8)])
    spy = _Spy(silent, provider="vapi", caller="assistant-originated")
    monkeypatch.setattr("hotato.drive.place_call_vapi", spy)
    bundle = _make_bundle(tmp_path, stack="vapi")

    code = cli.main([
        "drive", str(bundle), "--stack", "vapi", "--assistant", "asst_clone",
        "--api-key", "sk-live", "--phone-number-id", "pn_1",
        "--customer", "+15551230000", "--yes",
    ])
    out = capsys.readouterr().out
    assert "NOT SCORABLE" in out
    assert "the gate stays red" in out
    assert code == 2


# =========================================================================
# Finding 6 (MINOR): a total-fetch-failure pull/sweep is an outage -> non-zero
# =========================================================================

def _outage_pull(stack, creds, *, out_dir, ids=None, since=None, limit=50,
                 allow_mono=False, log=None):
    os.makedirs(out_dir, exist_ok=True)
    return {"stack": stack, "out_dir": out_dir, "listed": 3, "pulled": [],
            "skipped": [{"id": f"c{i}", "reason": "HTTP 503"} for i in range(3)]}


def test_pull_total_fetch_failure_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cap, "pull", _outage_pull)
    rc = cli.main(["pull", "--stack", "vapi", "--api-key", "k",
                   "--out", str(tmp_path / "d")])
    assert rc == 2
    out = capsys.readouterr().out
    assert "skipped 3" in out


def test_pull_with_nothing_listed_still_exits_0(tmp_path, monkeypatch):
    # No recent calls is a completed (empty) pull, not an outage.
    monkeypatch.setattr(cap, "pull", _fake_pull_with([]))
    rc = cli.main(["pull", "--stack", "vapi", "--api-key", "k",
                   "--out", str(tmp_path / "d")])
    assert rc == 0


def test_sweep_total_fetch_failure_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cap, "pull", _outage_pull)
    rc = cli.main(["sweep", "--stack", "vapi", "--api-key", "k",
                   "--dir", str(tmp_path / "d"), "--out",
                   str(tmp_path / "s.html"), "--no-open"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "failed to fetch" in err
