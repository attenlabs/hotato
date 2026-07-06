"""Regression gate for the real-audio example set (``corpus/real/``).

What it pins:
  - the manifest matches the files on disk (byte-exact via sha256, no strays);
  - every committed label is structurally sound and honestly attributed
    (CC BY 4.0 AMI provenance, source_type "real", never dressed up as MIT);
  - ``corpus/validate.py`` reports EXACTLY the three known policy
    differences for this CC BY tree and nothing else;
  - every clip scores without error, and the scorer's measurements
    (did_yield, seconds_to_yield, talk_over, detected onset) reproduce the
    values recorded in the manifest at build time;
  - on clips not flagged for headset bleed, the AUTO-detected caller onset
    lands within a stated tolerance of the annotation-derived onset. The
    per-clip deltas are the interesting number and live in the manifest.

The clips are real AMI Meeting Corpus audio (CC BY 4.0); both parties are
human. Several backchannel clips honestly measure did_yield=true because
the human floor holder pauses around the acknowledgement; those recorded
verdicts are pinned here as measurements, not treated as suite failures.

Skips cleanly if the audio has not been built/checked out (the clips are
committed today, but the gate must not break a partial checkout).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL = os.path.join(_REPO, "corpus", "real")
_MANIFEST = os.path.join(_REAL, "manifest.json")

# Auto-detected onset must land this close to the annotation-derived onset on
# clips without a bleed flag. The actual deltas (the interesting numbers) are
# recorded per clip in manifest.json as measured.onset_delta_sec.
ONSET_TOLERANCE_SEC = 0.5

# The three, and only three, ways these CC BY labels differ from the MIT
# contribution-corpus policy that corpus/validate.py enforces (see
# corpus/real/README.md and LICENSES.md).
_EXPECTED_POLICY_DIFFS = (
    "attestation.right_to_release_mit must be true",
    'license must be "MIT"',
    "source_type must be one of",
)

pytestmark = pytest.mark.skipif(
    not os.path.exists(_MANIFEST),
    reason="corpus/real has not been built (run corpus/real/build_real.py)",
)


def _manifest() -> dict:
    with open(_MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _clips() -> list:
    return _manifest()["clips"]


def _clip_params():
    if not os.path.exists(_MANIFEST):
        return []
    return [pytest.param(c, id=c["id"]) for c in _clips()]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _label(clip: dict) -> dict:
    with open(os.path.join(_REAL, clip["label"]), encoding="utf-8") as fh:
        return json.load(fh)


def _audio_missing() -> bool:
    return any(
        not os.path.exists(os.path.join(_REAL, c["audio"])) for c in _clips()
    )


def _require_audio():
    if _audio_missing():
        pytest.skip("real clip audio not present (gitignored or partial checkout)")


# --- manifest vs disk -------------------------------------------------------

def test_manifest_matches_disk():
    _require_audio()
    for clip in _clips():
        wav = os.path.join(_REAL, clip["audio"])
        assert os.path.exists(wav), f"missing {clip['audio']}"
        assert _sha256(wav) == clip["sha256"], (
            f"{clip['id']}: audio bytes changed; rebuild with build_real.py"
        )
        assert os.path.getsize(wav) == clip["bytes"]
        assert os.path.exists(os.path.join(_REAL, clip["label"]))


def test_no_stray_files():
    """audio/ and scenarios/ hold exactly the manifest set, nothing else."""
    clips = _clips()
    want_audio = {os.path.basename(c["audio"]) for c in clips}
    want_labels = {os.path.basename(c["label"]) for c in clips}
    audio_dir = os.path.join(_REAL, "audio")
    scen_dir = os.path.join(_REAL, "scenarios")
    if os.path.isdir(audio_dir):
        got = {n for n in os.listdir(audio_dir) if n.endswith(".wav")}
        assert got <= want_audio, f"stray audio files: {got - want_audio}"
    got_labels = {n for n in os.listdir(scen_dir) if n.endswith(".json")}
    assert got_labels == want_labels


def test_total_size_budget():
    """The committed clip set stays modest (well under 25 MB)."""
    total = sum(c["bytes"] for c in _clips())
    assert total < 25 * 1024 * 1024


def test_manifest_provenance_block():
    m = _manifest()
    assert m["license"] == "CC-BY-4.0"
    assert m["dataset"] == "AMI Meeting Corpus"
    assert m["clip_count"] == len(m["clips"])
    files = {s["file"] for s in m["sources"]}
    assert "ami_public_manual_1.6.2.zip" in files
    for s in m["sources"]:
        assert s["url"].startswith("https://groups.inf.ed.ac.uk/ami/")
        assert len(s["sha256"]) == 64


# --- labels -----------------------------------------------------------------

@pytest.mark.parametrize("clip", _clip_params())
def test_label_shape_and_honesty(clip):
    label = _label(clip)
    assert label["id"] == clip["id"]
    assert label["category"] in ("should_yield", "should_not_yield")
    assert label["source_type"] == "real"
    assert label["license"] == "CC-BY-4.0"
    assert label["sample_rate"] == 16000
    assert 5.0 <= label["duration_sec"] <= 20.0
    assert 0 <= label["caller_onset_sec"] <= label["duration_sec"]
    exp = label["expected"]
    if label["category"] == "should_yield":
        assert exp["yield"] is True
        assert exp["max_time_to_yield_sec"] is not None
    else:
        assert exp["yield"] is False
        assert exp["max_time_to_yield_sec"] is None
        assert exp["max_talk_over_sec"] is None
    # attribution: CC BY requires it, every label carries it
    att = label["attribution"]
    assert att["dataset"] == "AMI Meeting Corpus"
    assert att["license"] == "CC-BY-4.0"
    assert "CC BY 4.0" in att["notice"]
    # provenance back to the exact source material
    prov = label["provenance"]
    assert prov["meeting"] in {"ES2002a", "EN2002b"}
    assert prov["caller"]["headset_wav"].endswith(".wav")
    assert prov["agent"]["headset_wav"].endswith(".wav")
    assert len(prov["window_global_sec"]) == 2
    # the honesty line: never dressed up as relicensable
    assert label["attestation"]["right_to_release_mit"] is False
    # word-derived segments exist and are ordered
    for key in ("caller_segments_sec", "agent_segments_sec"):
        segs = label["reference_render"][key]
        assert segs, f"{key} empty"
        for s, e in segs:
            assert 0 <= s < e <= label["duration_sec"]


def _load_validator():
    path = os.path.join(_REPO, "corpus", "validate.py")
    spec = importlib.util.spec_from_file_location("hotato_corpus_validate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("clip", _clip_params())
def test_validate_py_reports_exactly_the_policy_diffs(clip):
    """corpus/validate.py must pass everything EXCEPT the three known,
    documented MIT-corpus policy checks. Any other error is a real defect."""
    _require_audio()
    val = _load_validator()
    report = val.validate(
        os.path.join(_REAL, clip["label"]),
        os.path.join(_REAL, clip["audio"]),
    )
    unexpected = [
        e for e in report.errors
        if not any(e.startswith(prefix) for prefix in _EXPECTED_POLICY_DIFFS)
    ]
    assert not unexpected, f"unexpected validate.py errors: {unexpected}"
    assert len(report.errors) == len(_EXPECTED_POLICY_DIFFS)


# --- scoring: measurements reproduce the manifest ---------------------------

@pytest.mark.parametrize("clip", _clip_params())
def test_clip_scores_and_reproduces_manifest(clip):
    _require_audio()
    from hotato.core import run_single

    label = _label(clip)
    want = clip["measured"]
    expect = "yield" if label["category"] == "should_yield" else "hold"
    env = run_single(
        stereo=os.path.join(_REAL, clip["audio"]),
        onset_sec=label["caller_onset_sec"],
        expect=expect,
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


@pytest.mark.parametrize("clip", _clip_params())
def test_detected_onset_near_annotated_onset(clip):
    """The engine's AUTO-detected onset vs the annotation-derived onset.
    The per-clip deltas are recorded in the manifest; clips flagged for
    headset bleed document why auto-detection fires early there."""
    _require_audio()
    from hotato.core import run_single

    label = _label(clip)
    expect = "yield" if label["category"] == "should_yield" else "hold"
    env = run_single(
        stereo=os.path.join(_REAL, clip["audio"]), onset_sec=None, expect=expect
    )
    detected = env["events"][0]["measurements"]["caller_onset_sec"]
    assert detected is not None
    delta = round(detected - label["caller_onset_sec"], 3)
    want = clip["measured"]
    assert delta == pytest.approx(want["onset_delta_sec"], abs=0.011)
    bleed_flagged = "bleed" in label.get("provenance", {}).get("notes", "").lower()
    if not bleed_flagged:
        assert abs(delta) <= ONSET_TOLERANCE_SEC, (
            f"{clip['id']}: auto-detected onset {detected}s is {delta}s from "
            f"the annotated onset {label['caller_onset_sec']}s"
        )


def test_run_suite_over_real_scenarios():
    """The whole real set runs through run_suite; per-clip verdicts match the
    manifest. Backchannel clips that measure a real micro-pause yield are
    honest FAILs by construction, so the suite exit_code of 1 is expected."""
    _require_audio()
    from hotato.core import run_suite

    env = run_suite(
        scenarios_dir=os.path.join(_REAL, "scenarios"),
        audio_dir=os.path.join(_REAL, "audio"),
    )
    by_id = {c["id"]: c["measured"] for c in _clips()}
    assert env["summary"]["events"] == len(by_id)
    for ev in env["events"]:
        want = by_id[ev["event_id"]]
        assert ev["verdict"]["passed"] == want["passed"], ev["event_id"]
        assert ev["verdict"]["did_yield"] == want["did_yield"], ev["event_id"]
    failed = [e["event_id"] for e in env["events"] if not e["verdict"]["passed"]]
    assert env["exit_code"] == (1 if failed else 0)
