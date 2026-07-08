"""Honest "not scorable" semantics, from an external correctness review.

Two malformed-input shapes used to escape as normal verdicts:

  (a) a recording with no detectable caller speech and no onset provided was
      clamped to frame 0 and scored anyway;
  (b) a should-yield expectation with the agent silent at the caller onset
      carried a "did_yield is not meaningful" note but still produced a
      normal PASS or FAIL.

Both are input problems, not agent verdicts. The contract pinned here:

  * such an event carries scorable: false plus a plain not_scorable_reason;
  * it is counted in summary.events but in neither passed nor failed, never
    trips regression, the fix map, or the funnel, and can never be a normal
    pass or fail;
  * summary.not_scorable appears ONLY when at least one event is not
    scorable; the envelope exit_code stays 0 or 1 and reflects scorable
    failures only; process_exit_code maps an all-not-scorable single run to
    the CLI's existing exit-2 (unusable input) convention;
  * every valid recording is byte-identical to before: no scorable key
    anywhere, and the checked-in golden still matches.
"""

import json
import math
import os
import struct
import wave
from importlib import resources

import pytest

from hotato.core import process_exit_code, run_single, run_suite

_HERE = os.path.dirname(os.path.abspath(__file__))


# --- deterministic synthetic fixtures --------------------------------------

def _write_stereo(path, caller_segments, agent_segments, duration_sec=3.0, sr=16000):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1. Each
    channel is a pure sine inside its active segments and exact digital
    silence outside them, so every render is byte-identical everywhere."""
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


def _silent_caller_wav(tmp_path):
    # The agent talks; the caller channel never does.
    return _write_stereo(tmp_path / "silent-caller.wav", [], [(0.2, 2.8)])


def _agent_silent_at_onset_wav(tmp_path):
    # The caller speaks at 1.0s; the agent is silent then and only responds
    # later. Under the old behavior this scored a normal (meaningless) PASS.
    return _write_stereo(
        tmp_path / "agent-silent-at-onset.wav", [(1.0, 2.0)], [(2.4, 2.9)]
    )


def _assert_not_scorable(env, reason_must_mention):
    e = env["events"][0]
    assert e["scorable"] is False
    reason = e["not_scorable_reason"]
    assert reason_must_mention in reason
    # plain honest prose: no em or en dashes, no accuracy claims
    assert "—" not in reason and "–" not in reason
    assert "%" not in reason
    # never a normal pass or fail: fail-closed verdict, excluded everywhere
    assert e["verdict"]["passed"] is False
    assert e["verdict"]["reasons"] == [reason]
    assert e["fix"] is None
    assert env["fix_map"] == []
    assert env["funnel"] is None
    s = env["summary"]
    assert s["events"] == 1
    assert s["passed"] == 0
    assert s["failed"] == 0
    assert s["not_scorable"] == 1
    assert s["regression"] is False
    # the envelope exit_code stays schema-frozen 0|1; the process-level exit-2
    # decision is surfaced through the helper the CLI can wire later
    assert env["exit_code"] == 0
    assert process_exit_code(env) == 2
    return e


# --- (a) silent caller, no onset --------------------------------------------

def test_silent_caller_without_onset_is_not_scorable(tmp_path):
    env = run_single(stereo=_silent_caller_wav(tmp_path), expect="yield")
    _assert_not_scorable(env, "caller speech")


def test_silent_caller_not_scorable_even_when_expecting_hold(tmp_path):
    # No caller event exists, so there is nothing to judge under ANY
    # expectation: this must never come back as a normal hold pass.
    env = run_single(stereo=_silent_caller_wav(tmp_path), expect="hold")
    _assert_not_scorable(env, "caller speech")


def test_silent_caller_with_explicit_onset_still_scores():
    # An explicit onset is the user asserting where the event is; that path
    # is unchanged. Pin it on a real bundled recording.
    wav = str(
        resources.files("hotato").joinpath(
            "data", "audio", "01-hard-interruption.example.wav"
        )
    )
    env = run_single(stereo=wav, onset_sec=1.8, expect="yield")
    assert "scorable" not in env["events"][0]
    assert "not_scorable" not in env["summary"]


# --- (b) agent silent at the caller onset, expecting a yield ----------------

@pytest.mark.parametrize("onset_sec", [None, 1.0])
def test_agent_silent_at_onset_with_yield_expectation_is_not_scorable(tmp_path, onset_sec):
    env = run_single(
        stereo=_agent_silent_at_onset_wav(tmp_path),
        onset_sec=onset_sec,
        expect="yield",
    )
    e = _assert_not_scorable(env, "agent was not talking")
    # the raw measurement that triggered the call is still visible
    assert e["measurements"]["agent_talking_at_onset"] is False


def test_agent_silent_at_onset_with_hold_expectation_stays_scorable(tmp_path):
    # The reviewed defect is scoped to should-yield: a yield expectation with
    # no agent speech at onset is malformed. A hold expectation still gets a
    # normal verdict, whatever it is.
    env = run_single(
        stereo=_agent_silent_at_onset_wav(tmp_path), onset_sec=1.0, expect="hold"
    )
    assert "scorable" not in env["events"][0]
    assert "not_scorable" not in env["summary"]
    assert env["summary"]["passed"] + env["summary"]["failed"] == 1


# --- suites: listed with a reason, never failing the suite by themselves ----

def test_suite_lists_not_scorable_without_failing(tmp_path):
    scen_dir = tmp_path / "scenarios"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()

    # one clean should-yield scenario: agent holds the floor, caller barges in
    # at 1.0s, agent yields at 1.5s
    _write_stereo(audio_dir / "aa-valid.example.wav", [(1.0, 2.0)], [(0.0, 1.5)])
    (scen_dir / "aa-valid.json").write_text(
        json.dumps(
            {
                "id": "aa-valid",
                "title": "clean interruption",
                "category": "should_yield",
                "expected": {"yield": True},
            }
        ),
        encoding="utf-8",
    )
    # one silent-caller scenario with no labeled onset
    _write_stereo(audio_dir / "zz-silent.example.wav", [], [(0.2, 2.8)])
    (scen_dir / "zz-silent.json").write_text(
        json.dumps(
            {
                "id": "zz-silent",
                "title": "silent caller channel",
                "category": "should_yield",
                "expected": {"yield": True},
            }
        ),
        encoding="utf-8",
    )

    env = run_suite(
        suite="barge-in", scenarios_dir=str(scen_dir), audio_dir=str(audio_dir)
    )
    by = {e["event_id"]: e for e in env["events"]}
    assert by["aa-valid"]["verdict"]["passed"] is True
    assert "scorable" not in by["aa-valid"]
    assert by["zz-silent"]["scorable"] is False
    assert "caller speech" in by["zz-silent"]["not_scorable_reason"]

    s = env["summary"]
    assert s == {
        "events": 2,
        "passed": 1,
        "failed": 0,
        "regression": False,
        "not_scorable": 1,
    }
    # a not-scorable event does not fail the suite by itself
    assert env["exit_code"] == 0
    assert process_exit_code(env) == 0  # exit-2 mapping is single mode only


def test_missing_scenario_audio_is_not_scorable_not_a_missed_interruption(tmp_path):
    """Regression: a scenario whose audio file does not exist is an INPUT problem,
    not a 'missed real interruption'. run_suite must mark it scorable:false (like
    every other not-scorable input problem) so it is excluded from passed/failed,
    the funnel, and fix_map -- and can never spuriously fire the engagement-control
    pointer on a typo or an untrusted third-party scenario submission. Before the
    fix run_suite fabricated a did_yield:False verdict + a fix block, so a
    should-yield scenario with a missing file counted as a genuine failed
    interruption and could arm the funnel; diagnose already refused it, so the two
    commands disagreed on the same battery."""
    scen_dir = tmp_path / "scenarios"
    audio_dir = tmp_path / "audio"
    scen_dir.mkdir()
    audio_dir.mkdir()

    # a real should-HOLD backchannel (the other funnel axis), so that if the
    # missing should-yield file were mis-scored as a real miss, the funnel would
    # fire on both axes.
    _write_stereo(audio_dir / "aa-backchannel.example.wav",
                  [(1.0, 1.2)], [(0.0, 3.0)])
    (scen_dir / "aa-backchannel.json").write_text(
        json.dumps({"id": "aa-backchannel", "category": "should_hold",
                    "expected": {"yield": False}}),
        encoding="utf-8",
    )
    # a should-yield scenario with NO audio file created
    (scen_dir / "missing-01-should-yield.json").write_text(
        json.dumps({"id": "missing-01-should-yield", "category": "should_yield",
                    "expected": {"yield": True}}),
        encoding="utf-8",
    )

    env = run_suite(
        suite="barge-in", scenarios_dir=str(scen_dir), audio_dir=str(audio_dir)
    )
    by = {e["event_id"]: e for e in env["events"]}
    miss = by["missing-01-should-yield"]
    assert miss["scorable"] is False
    assert "missing audio" in miss["not_scorable_reason"]
    # no fabricated did_yield:False verdict, no fix block
    assert miss["verdict"]["did_yield"] is None
    assert "fix" not in miss
    # excluded from failed and the funnel; cannot fire the engagement pointer
    assert env["summary"]["not_scorable"] == 1
    assert env["funnel"] is None
    assert all(f["scenario_id"] != "missing-01-should-yield"
               for f in env.get("fix_map", []))


# --- (c) valid recordings are byte-identical to before ----------------------

def test_bundled_suite_has_no_scorable_key_and_golden_matches():
    env = run_suite(suite="barge-in")
    for e in env["events"]:
        assert "scorable" not in e
        assert "not_scorable_reason" not in e
    assert "not_scorable" not in env["summary"]

    with open(os.path.join(_HERE, "golden", "suite_barge-in.json"), encoding="utf-8") as fh:
        golden = json.load(fh)
    got = json.loads(json.dumps(env))
    got["engine"]["version"] = "*"
    assert json.dumps(got, sort_keys=True) == json.dumps(golden, sort_keys=True)


# --- (d) both shapes validate against the shipped schema --------------------

def _schema():
    return json.loads(
        resources.files("hotato")
        .joinpath("schema", "envelope.v1.json")
        .read_text(encoding="utf-8")
    )


def test_not_scorable_envelopes_validate_against_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema()
    envs = [
        run_single(stereo=_silent_caller_wav(tmp_path), expect="yield"),
        run_single(
            stereo=_agent_silent_at_onset_wav(tmp_path), onset_sec=1.0, expect="yield"
        ),
        run_suite(suite="barge-in"),
    ]
    for env in envs:
        jsonschema.validate(instance=env, schema=schema)


def test_schema_enforces_not_scorable_honesty(tmp_path):
    """The schema itself forbids dishonest shapes: scorable may only ever be
    false, it must travel with its reason, and summary.not_scorable may not
    appear as a zero."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema()
    base = run_single(stereo=_silent_caller_wav(tmp_path), expect="yield")

    lying = json.loads(json.dumps(base))
    lying["events"][0]["scorable"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=lying, schema=schema)

    reasonless = json.loads(json.dumps(base))
    del reasonless["events"][0]["not_scorable_reason"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=reasonless, schema=schema)

    zero = json.loads(json.dumps(base))
    zero["summary"]["not_scorable"] = 0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=zero, schema=schema)
