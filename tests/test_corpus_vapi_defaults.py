"""Regression gate for the vapi-defaults real-call example set
(``corpus/vapi-defaults/``).

What it pins:
  - the manifest matches the files on disk (byte-exact via sha256, no strays)
    and stays inside the stated size budget;
  - every battery label validates CLEANLY against ``corpus/validate.py``
    (these are operator-owned MIT contributions: zero policy diffs, unlike
    the CC BY tree in ``corpus/real/``);
  - every committed clip scores deterministically to the measurements
    recorded in the manifest at build time (did_yield, seconds_to_yield,
    talk_over, verdict), i.e. the labelled verdicts reproduce;
  - the battery as a whole fails on BOTH axes (a missed real interruption
    AND false yields on backchannels), the envelope funnel fires, and
    ``diagnose`` returns battery decision ``do_not_tune_single_threshold``;
  - the script 9 analysis clip is honestly not scorable for yield/hold (the
    agent was not talking at the caller onset) and reproduces its measured
    latency fact (the agent entering the caller's mid-sentence pause).

The recorded verdicts here include honest FAILs by construction (the whole
point of the set is that the default configuration fails in both
directions), so the suite-level exit_code of 1 is pinned as EXPECTED.

Skips cleanly if the corpus directory or its audio is absent (partial
checkout), exactly like tests/test_corpus_real.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_REPO, "corpus", "vapi-defaults")
_MANIFEST = os.path.join(_DIR, "manifest.json")

# Committed audio stays modest (the stated budget for this set).
SIZE_BUDGET_BYTES = 20 * 1024 * 1024

pytestmark = pytest.mark.skipif(
    not os.path.exists(_MANIFEST),
    reason="corpus/vapi-defaults has not been built/checked out",
)


def _manifest() -> dict:
    with open(_MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _clips() -> list:
    return _manifest()["clips"]


def _clip_params(kind=None):
    if not os.path.exists(_MANIFEST):
        return []
    return [
        pytest.param(c, id=c["id"])
        for c in _clips()
        if kind is None or c["kind"] == kind
    ]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _audio_missing() -> bool:
    return any(
        not os.path.exists(os.path.join(_DIR, c["audio"])) for c in _clips()
    )


def _require_audio():
    if _audio_missing():
        pytest.skip("vapi-defaults clip audio not present (partial checkout)")


# --- manifest vs disk -------------------------------------------------------

def test_manifest_matches_disk():
    _require_audio()
    for clip in _clips():
        wav = os.path.join(_DIR, clip["audio"])
        assert os.path.exists(wav), f"missing {clip['audio']}"
        assert _sha256(wav) == clip["sha256"], (
            f"{clip['id']}: audio bytes changed; rebuild with build_vapi_defaults.py"
        )
        assert os.path.getsize(wav) == clip["bytes"]
        if clip["kind"] == "scenario":
            assert os.path.exists(os.path.join(_DIR, clip["label"]))


def test_no_stray_files():
    """audio/ and scenarios/ hold exactly the manifest set, nothing else."""
    clips = _clips()
    want_audio = {os.path.basename(c["audio"]) for c in clips}
    want_labels = {
        os.path.basename(c["label"]) for c in clips if c["kind"] == "scenario"
    }
    audio_dir = os.path.join(_DIR, "audio")
    scen_dir = os.path.join(_DIR, "scenarios")
    if os.path.isdir(audio_dir):
        got = {n for n in os.listdir(audio_dir) if n.endswith(".wav")}
        assert got == want_audio
    got_labels = {n for n in os.listdir(scen_dir) if n.endswith(".json")}
    assert got_labels == want_labels


def test_total_size_budget():
    total = sum(c["bytes"] for c in _clips())
    assert total < SIZE_BUDGET_BYTES


def test_manifest_provenance_block():
    m = _manifest()
    assert m["license"] == "MIT"
    prov = m["provenance"]
    assert prov["assistant_name"] == "hotato-probe"
    assert prov["model"] == "openai/gpt-4o"
    assert prov["recorded"] == "2026-07-06"
    assert "default" in prov["interruption_settings"].lower()
    assert prov["channel_map"] == {"caller": 0, "agent": 1}
    assert m["clip_count"] == len(m["clips"])
    # source recordings are pinned even though they are not distributed
    assert m["source_recordings"]["distributed"] is False
    for src in m["source_recordings"]["files"]:
        assert len(src["sha256"]) == 64
        assert src["call_id"]


# --- labels: structurally sound, honestly attributed, MIT-clean -------------

def _load_validator():
    path = os.path.join(_REPO, "corpus", "validate.py")
    spec = importlib.util.spec_from_file_location("hotato_corpus_validate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("clip", _clip_params(kind="scenario"))
def test_label_validates_clean(clip):
    """Unlike the CC BY real/ tree (three documented policy diffs), these
    labels are operator-owned MIT contributions and must validate with ZERO
    errors against the contribution policy."""
    _require_audio()
    val = _load_validator()
    report = val.validate(
        os.path.join(_DIR, clip["label"]),
        os.path.join(_DIR, clip["audio"]),
    )
    assert report.ok, f"validate.py errors: {report.errors}"


@pytest.mark.parametrize("clip", _clip_params(kind="scenario"))
def test_label_shape_and_honesty(clip):
    with open(os.path.join(_DIR, clip["label"]), encoding="utf-8") as fh:
        label = json.load(fh)
    assert label["id"] == clip["id"]
    assert label["category"] == clip["category"]
    assert label["source_type"] == "role-played"
    assert label["license"] == "MIT"
    assert label["sample_rate"] == 16000
    assert label["channels"] == {"caller_channel": 0, "agent_channel": 1}
    exp = label["expected"]
    if label["category"] == "should_yield":
        assert exp["yield"] is True
        # uniform stated bounds, never tuned per clip
        assert exp["max_time_to_yield_sec"] == 1.0
        assert exp["max_talk_over_sec"] == 1.0
    else:
        assert exp["yield"] is False
        assert exp["max_time_to_yield_sec"] is None
        assert exp["max_talk_over_sec"] is None
    prov = label["provenance"]
    assert prov["assistant"] == "hotato-probe"
    assert prov["call_id"] == clip["call_id"]
    assert prov["onset_derivation"]
    assert prov["field_note"]
    assert prov["agreement_with_field_note"]
    att = label["attestation"]
    assert att["right_to_release_mit"] is True
    assert att["consent_on_file"] is True
    assert att["no_phi"] is True


# --- scoring: the labelled verdicts reproduce deterministically -------------

@pytest.mark.parametrize("clip", _clip_params(kind="scenario"))
def test_clip_scores_to_recorded_measurement(clip):
    _require_audio()
    from hotato.core import run_single

    with open(os.path.join(_DIR, clip["label"]), encoding="utf-8") as fh:
        label = json.load(fh)
    want = clip["measured"]
    expect = "yield" if label["category"] == "should_yield" else "hold"
    env = run_single(
        stereo=os.path.join(_DIR, clip["audio"]),
        onset_sec=label["caller_onset_sec"],
        expect=expect,
        stack="vapi",
        max_time_to_yield_sec=label["expected"]["max_time_to_yield_sec"],
        max_talk_over_sec=label["expected"]["max_talk_over_sec"],
    )
    ev = env["events"][0]
    assert ev.get("scorable") is not False, ev.get("not_scorable_reason")
    assert ev["measurements"]["agent_talking_at_onset"] is True
    assert ev["verdict"]["did_yield"] == want["did_yield"]
    assert ev["verdict"]["passed"] == want["passed"]
    tty, want_tty = ev["verdict"]["seconds_to_yield"], want["seconds_to_yield"]
    if want_tty is None:
        assert tty is None
    else:
        assert tty == pytest.approx(want_tty, abs=0.011)
    assert ev["verdict"]["talk_over_sec"] == pytest.approx(
        want["talk_over_sec"], abs=0.011)


def test_analysis_clip_pause_jump_in():
    """Script 9: not scorable for yield/hold by design (the agent was not
    talking at the caller onset); the measured fact is the agent taking the
    floor inside the caller's mid-sentence pause (response_gap_sec)."""
    _require_audio()
    from hotato.core import run_single

    clips = [c for c in _clips() if c["kind"] == "analysis"]
    assert len(clips) == 1
    clip = clips[0]
    assert clip["id"] == "vapi-default-09-pause-jump-in"
    env = run_single(
        stereo=os.path.join(_DIR, clip["audio"]),
        onset_sec=clip["caller_onset_sec"],
        expect="yield",
        stack="vapi",
    )
    ev = env["events"][0]
    assert ev.get("scorable") is False
    assert ev["measurements"]["agent_talking_at_onset"] is False
    gap = ev["signals"]["latency"]["response_gap_sec"]
    want = clip["measured"]["response_gap_sec"]
    assert gap == pytest.approx(want, abs=0.011)
    # the scripted pause was 4 s; the agent measurably entered it early
    assert gap < 4.0


# --- the battery: both axes fail, the funnel fires, diagnose refuses --------

def _battery_env():
    from hotato.core import run_suite

    return run_suite(
        scenarios_dir=os.path.join(_DIR, "scenarios"),
        audio_dir=os.path.join(_DIR, "audio"),
        stack="vapi",
    )


def test_run_suite_reproduces_manifest_verdicts():
    _require_audio()
    env = _battery_env()
    by_id = {
        c["id"]: c["measured"] for c in _clips() if c["kind"] == "scenario"
    }
    assert env["summary"]["events"] == len(by_id)
    for ev in env["events"]:
        want = by_id[ev["event_id"]]
        assert ev.get("scorable") is not False, ev["event_id"]
        assert ev["verdict"]["passed"] == want["passed"], ev["event_id"]
        assert ev["verdict"]["did_yield"] == want["did_yield"], ev["event_id"]
    # honest FAILs by construction: the default config fails on both axes
    failed = [e for e in env["events"] if not e["verdict"]["passed"]]
    assert failed, "the set exists because the default config fails"
    assert env["exit_code"] == 1


def test_funnel_fires_on_real_calls():
    """The both-axes funnel on real audio: a missed real interruption AND a
    false yield on a backchannel in the same battery."""
    _require_audio()
    env = _battery_env()
    missed_real = [
        e for e in env["events"]
        if e["expected_yield"] and not e["verdict"]["passed"]
        and not e["verdict"]["did_yield"]
    ]
    false_barge = [
        e for e in env["events"]
        if not e["expected_yield"] and not e["verdict"]["passed"]
        and e["verdict"]["did_yield"]
    ]
    assert missed_real, "expected the missed quiet interruption (script 10)"
    assert false_barge, "expected false yields on backchannels (scripts 4/5)"
    funnel = env["funnel"]
    assert funnel is not None
    assert "BOTH axes" in funnel["reason"]
    assert funnel["pointer"]


def test_diagnose_refuses_single_threshold():
    _require_audio()
    from hotato.diagnose import diagnose_envelope

    diag = diagnose_envelope(_battery_env())
    battery = diag["battery"]
    assert battery["finding"] == "threshold_funnel"
    assert battery["decision"] == "do_not_tune_single_threshold"


def test_committed_battery_result_matches_fresh_run():
    """battery-result.json (the committed diagnose input) stays in sync with
    what the committed clips actually produce."""
    _require_audio()
    path = os.path.join(_DIR, "battery-result.json")
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        committed = json.load(fh)
    fresh = _battery_env()
    assert committed["summary"] == fresh["summary"]
    assert committed["funnel"] == fresh["funnel"]
    committed_verdicts = {
        e["event_id"]: e["verdict"] for e in committed["events"]
    }
    for ev in fresh["events"]:
        assert committed_verdicts[ev["event_id"]] == ev["verdict"]
