"""P5: the seven corpus scenario CLASSES under corpus/classes/.

mid-utterance-pause, backchannel-multilingual, noise-hold, telephony-degraded,
leading-edge-onset, structured-utterance, browser-telephony-parity: 22
scenarios, additive to corpus/suites/, built by the SAME deterministic
generator pattern (seed = sha256(id), scenario builders reused from
corpus/suites/build_suites.py, audio rendered by examples/render_examples.py).
Every scenario is synthetic and says so. Audio is gitignored and regenerates
byte-identically (python3 corpus/classes/build_classes.py); every test here
skips cleanly if that audio has not been rendered yet.

Kept deliberately separate from tests/test_corpus_suites.py's generic,
dynamically-discovered suite tests, for one honest reason: the
mid-utterance-pause class needs a wider ``turn_end_silence_sec`` than the
library default so a multi-second thinking pause is not mistaken for the end
of the caller's turn (Hotato's default fires on 0.20s of silence). That
config is scenario-specific, applied here, and documented in each label's
``latency_bounds``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASSES_DIR = os.path.join(REPO, "corpus", "classes")
BUILDER_PATH = os.path.join(CLASSES_DIR, "build_classes.py")
MANIFEST_PATH = os.path.join(CLASSES_DIR, "manifest.json")

pytestmark = pytest.mark.skipif(
    not os.path.exists(MANIFEST_PATH),
    reason="corpus/classes has not been built/checked out",
)


def _load_builder():
    spec = importlib.util.spec_from_file_location("hotato_build_classes", BUILDER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)


CLASS_NAMES = [c["name"] for c in _manifest()["classes"]] if os.path.exists(MANIFEST_PATH) else []


def _scen_dir(name):
    return os.path.join(CLASSES_DIR, name, "scenarios")


def _audio_dir(name):
    return os.path.join(CLASSES_DIR, name, "audio")


def _scenario_files(name):
    return sorted(
        fn for fn in os.listdir(_scen_dir(name))
        if fn.endswith(".json") and fn != "manifest.json"
    )


def _load(name, fn):
    with open(os.path.join(_scen_dir(name), fn), encoding="utf-8") as fh:
        return json.load(fh)


ALL_SCENARIOS = [(name, fn) for name in CLASS_NAMES for fn in _scenario_files(name)]


def _audio_missing(name):
    files = _scenario_files(name)
    audio = os.path.join(CLASSES_DIR, name, "audio")
    if not os.path.isdir(audio):
        return True
    on_disk = set(os.listdir(audio))
    return any(
        fn[:-5] + suffix not in on_disk
        for fn in files for suffix in (".example.wav", ".caller.wav")
    )


def _require_audio(name):
    if _audio_missing(name):
        pytest.skip(f"corpus/classes/{name} audio not present (partial checkout)")


# --- manifest vs disk -------------------------------------------------------

def test_manifest_lists_all_classes_on_disk():
    on_disk = sorted(
        d for d in os.listdir(CLASSES_DIR)
        if os.path.isdir(os.path.join(CLASSES_DIR, d, "scenarios"))
    )
    assert sorted(CLASS_NAMES) == on_disk


@pytest.mark.parametrize("name", CLASS_NAMES)
def test_class_manifest_matches_disk(name):
    info = {c["name"]: c for c in _manifest()["classes"]}[name]
    files = _scenario_files(name)
    assert info["scenarios"] == len(files), name

    with open(os.path.join(_scen_dir(name), "manifest.json"), encoding="utf-8") as fh:
        class_manifest = json.load(fh)
    listed = {e["id"]: e for e in class_manifest["scenarios"]}
    assert sorted(listed) == sorted(fn[:-5] for fn in files)

    fail = sum(1 for fn in files if _load(name, fn)["reference_verdict"] == "fail")
    assert info["pass"] + info["fail"] == info["scenarios"]
    assert info["fail"] == fail


def test_manifest_total_matches_sum():
    m = _manifest()
    assert m["total_scenarios"] == sum(c["scenarios"] for c in m["classes"])
    assert m["synthetic"] is True


def test_ids_do_not_collide_with_existing_corpus():
    """Guards against a silent id clash with corpus/suites/ or the bundled
    batteries (which would let one fixture's audio shadow another's)."""
    dirs = [
        os.path.join(REPO, "src", "hotato", "data", "scenarios"),
        os.path.join(REPO, "examples", "scenarios"),
        os.path.join(REPO, "examples", "funnel-demo", "scenarios"),
        os.path.join(REPO, "corpus", "vapi-defaults", "scenarios"),
    ]
    existing = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json") and fn != "manifest.json":
                existing.append(fn[:-5])
    suites_dir = os.path.join(REPO, "corpus", "suites")
    if os.path.isdir(suites_dir):
        for d in os.listdir(suites_dir):
            scen = os.path.join(suites_dir, d, "scenarios")
            if os.path.isdir(scen):
                for fn in sorted(os.listdir(scen)):
                    if fn.endswith(".json") and fn != "manifest.json":
                        existing.append(fn[:-5])

    new_ids = [fn[:-5] for _, fn in ALL_SCENARIOS]
    assert len(new_ids) == len(set(new_ids)), "duplicate ids within corpus/classes"
    clash = set(new_ids) & set(existing)
    assert not clash, f"corpus/classes ids collide with the existing corpus: {clash}"


# --- scenario schema shape and honesty rules (mirrors test_corpus_suites.py) -

def _all_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_strings(v)


@pytest.mark.parametrize("name,fn", ALL_SCENARIOS, ids=[f"{n}/{f[:-5]}" for n, f in ALL_SCENARIOS])
def test_scenario_schema_shape(name, fn):
    sc = _load(name, fn)

    assert sc["id"] == fn[:-5]
    assert isinstance(sc["title"], str) and sc["title"]
    assert sc["category"] in ("should_yield", "should_not_yield", "latency")
    assert isinstance(sc["tags"], list) and sc["tags"]
    assert isinstance(sc["family"], str) and sc["family"]
    assert sc["source_type"] == "synthetic"
    assert sc["sample_rate"] in (8000, 16000)
    dur = sc["duration_sec"]
    assert isinstance(dur, (int, float)) and dur > 0
    onset = sc["caller_onset_sec"]
    assert isinstance(onset, (int, float)) and 0 <= onset < dur

    exp = sc["expected"]
    assert set(exp) == {"yield", "max_time_to_yield_sec", "max_talk_over_sec"}
    if sc["category"] == "should_not_yield":
        assert exp["yield"] is False
        assert exp["max_time_to_yield_sec"] is None
        assert exp["max_talk_over_sec"] is None
    else:
        assert exp["yield"] is True
        assert exp["max_time_to_yield_sec"] > 0
        assert exp["max_talk_over_sec"] > 0

    rr = sc["reference_render"]
    assert isinstance(rr["agent_segments_sec"], list) and rr["agent_segments_sec"]
    segs = list(rr["agent_segments_sec"]) + list(rr.get("caller_segments_sec", []))
    for s, e in segs:
        assert 0 <= s < e <= dur + 1e-9, (sc["id"], s, e)

    assert sc["reference_verdict"] in ("pass", "fail")
    if sc["reference_verdict"] == "fail":
        assert sc["failure_axis"] in ("barge_in", "latency")
    else:
        assert "failure_axis" not in sc

    if sc["category"] == "latency":
        b = sc["latency_bounds"]
        assert b["max_response_gap_sec"] > 0
        assert isinstance(b["premature_is_failure"], bool)
        assert b["boundary_tolerance_hops"] >= 1
        assert b["turn_end_silence_sec"] > 0
        assert rr.get("continuous") is True
        assert ("rendered_response_gap_sec" in rr) != ("rendered_premature_lead_sec" in rr)

    assert isinstance(sc["why_it_matters"], str) and sc["why_it_matters"].strip()
    assert "\n" not in sc["why_it_matters"]
    assert isinstance(sc["related_signals"], list) and sc["related_signals"]

    blob = " ".join(_all_strings(sc))
    assert "%" not in blob, sc["id"]
    assert "accuracy" not in blob.lower(), sc["id"]
    assert "—" not in blob and "–" not in blob, sc["id"]
    if sc["reference_verdict"] == "fail":
        assert "DEFECT RENDER" in sc["why_it_matters"], sc["id"]
        assert "FAIL" in sc["title"], sc["id"]


# --- mid-utterance-pause: scored with a widened turn_end_silence_sec --------

MUP_SCENARIOS = [fn for fn in _scenario_files("mid-utterance-pause")] if "mid-utterance-pause" in CLASS_NAMES else []


@pytest.mark.parametrize("fn", MUP_SCENARIOS, ids=[f[:-5] for f in MUP_SCENARIOS])
def test_mid_utterance_pause_scores_to_label(fn):
    """Each fixture states its own required turn_end_silence_sec in
    latency_bounds; that value (always > the rendered pause) is the only
    non-default config used anywhere in this file, and it is read straight
    from the label rather than hand-picked in the test."""
    _require_audio("mid-utterance-pause")
    from hotato._engine.score import ScoreConfig
    from hotato.core import run_single

    sc = _load("mid-utterance-pause", fn)
    b = sc["latency_bounds"]
    cfg = ScoreConfig(turn_end_silence_sec=b["turn_end_silence_sec"])
    env = run_single(
        stereo=os.path.join(_audio_dir("mid-utterance-pause"), sc["id"] + ".example.wav"),
        onset_sec=sc["caller_onset_sec"],
        expect="yield",
        max_time_to_yield_sec=sc["expected"]["max_time_to_yield_sec"],
        max_talk_over_sec=sc["expected"]["max_talk_over_sec"],
        cfg=cfg,
    )
    e = env["events"][0]
    lat = e["signals"]["latency"]
    hop = e["measurements"]["hop_sec"]
    tol = b["boundary_tolerance_hops"] * hop + 1e-6
    rr = sc["reference_render"]

    if "rendered_response_gap_sec" in rr:
        assert lat["premature_start_sec"] == 0.0, (sc["id"], lat)
        assert lat["response_gap_sec"] is not None, (sc["id"], lat)
        assert lat["response_gap_sec"] <= b["max_response_gap_sec"] + tol, (sc["id"], lat)
    else:
        assert lat["premature_start_sec"] is not None and lat["premature_start_sec"] > 0.0, (sc["id"], lat)
        assert abs(lat["premature_start_sec"] - rr["rendered_premature_lead_sec"]) <= 1.0, (sc["id"], lat)

    premature_fail = b["premature_is_failure"] and lat["premature_start_sec"] not in (None, 0.0)
    gap_fail = lat["response_gap_sec"] is not None and lat["response_gap_sec"] > b["max_response_gap_sec"]
    computed_pass = not (premature_fail or gap_fail)
    want_pass = sc["reference_verdict"] == "pass"
    assert computed_pass is want_pass, (sc["id"], lat)


def test_mid_utterance_pause_default_config_would_misjudge_the_pause():
    """Documents WHY this class needs its own config: with the library
    DEFAULT ScoreConfig (turn_end_silence_sec=0.20s), the multi-second pause
    itself is short-circuited as the caller's turn end, so even the
    well-behaved reference fixture registers a large, spurious
    premature_start_sec under the default. This is the honest reason the
    generic suite tests never run against this class."""
    _require_audio("mid-utterance-pause")
    from hotato.core import run_single

    sc = _load("mid-utterance-pause", "mup-pause-2s.json")
    env = run_single(
        stereo=os.path.join(_audio_dir("mid-utterance-pause"), "mup-pause-2s.example.wav"),
        onset_sec=sc["caller_onset_sec"],
        expect="yield",
    )
    lat = env["events"][0]["signals"]["latency"]
    # under the default 0.20s turn-end silence, the 2.0s pause itself is
    # read as the caller's turn end, so the patient (correct) response looks
    # SLUGGISH (a large response_gap_sec, not a premature start) rather than
    # a false-premature detection; either way it does not match the reference
    # verdict, which is exactly why this class carries its own config.
    assert lat["response_gap_sec"] is not None
    assert lat["response_gap_sec"] > 1.0


# --- backchannel-multilingual and noise-hold: default config, did_yield axis

HOLD_CLASSES = [n for n in ("backchannel-multilingual", "noise-hold") if n in CLASS_NAMES]


@pytest.mark.parametrize("name", HOLD_CLASSES)
def test_hold_class_scores_to_label(name):
    _require_audio(name)
    from hotato.core import run_suite

    env = run_suite(suite="barge-in", scenarios_dir=_scen_dir(name), audio_dir=_audio_dir(name))
    by = {e["scenario_id"]: e for e in env["events"]}
    for fn in _scenario_files(name):
        sc = _load(name, fn)
        e = by[sc["id"]]
        want_pass = sc["reference_verdict"] == "pass"
        assert e["verdict"]["passed"] is want_pass, (sc["id"], e["verdict"]["reasons"])
        assert e["verdict"]["did_yield"] is (not want_pass), sc["id"]


# --- telephony-degraded: default config, did_yield axis, plus a degradation -
# sanity check that the render is actually different from an undegraded pass.

def test_telephony_degraded_scores_to_label():
    _require_audio("telephony-degraded")
    from hotato.core import run_suite

    env = run_suite(
        suite="barge-in",
        scenarios_dir=_scen_dir("telephony-degraded"),
        audio_dir=_audio_dir("telephony-degraded"),
    )
    by = {e["scenario_id"]: e for e in env["events"]}
    for fn in _scenario_files("telephony-degraded"):
        sc = _load("telephony-degraded", fn)
        e = by[sc["id"]]
        want_pass = sc["reference_verdict"] == "pass"
        assert e["verdict"]["passed"] is want_pass, (sc["id"], e["verdict"]["reasons"])


def test_telephony_degradation_actually_changes_the_audio():
    """Prove the mu-law + packet-loss step is a real transform, not a no-op:
    re-render the scenario's exact reference_render WITHOUT the degradation
    step and confirm the committed (degraded) audio differs sample-for-sample."""
    _require_audio("telephony-degraded")
    builder = _load_builder()
    bs_path = os.path.join(REPO, "corpus", "suites", "build_suites.py")
    spec = importlib.util.spec_from_file_location("hotato_build_suites_for_classes_test", bs_path)
    bs = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bs
    spec.loader.exec_module(bs)
    renderer = bs.load_renderer()

    from hotato._engine.audio import read_wav

    for fn in _scenario_files("telephony-degraded"):
        sc = _load("telephony-degraded", fn)
        sr, caller_plain, agent_plain = renderer.build_scenario(sc)
        committed = read_wav(os.path.join(_audio_dir("telephony-degraded"), sc["id"] + ".example.wav"))
        caller_committed = committed.get(0)
        assert len(caller_committed) == len(caller_plain)
        # mu-law's 8-bit codewords collapse the plain render's continuous
        # amplitude range to a small, fixed set of distinct levels.
        distinct_plain = len({round(x, 6) for x in caller_plain})
        distinct_degraded = len({round(x, 6) for x in caller_committed})
        assert distinct_degraded < distinct_plain / 10, sc["id"]
        assert distinct_degraded <= 256, sc["id"]
        # the fixed packet-loss schedule leaves exact-zero windows behind.
        zeroed = sum(1 for x in caller_committed if x == 0.0)
        assert zeroed > 0, sc["id"]


# --- structured-utterance: latency axis, widened turn_end_silence_sec -------
# Same scoring contract as mid-utterance-pause (each fixture states its own
# turn_end_silence_sec in latency_bounds, always wider than the widest
# intra-item gap), applied to structured-data cadence: phone-number digit groups
# and a spelled email, where an intra-item pause must not read as the turn end.

SU_SCENARIOS = (
    [fn for fn in _scenario_files("structured-utterance")]
    if "structured-utterance" in CLASS_NAMES else []
)


@pytest.mark.parametrize("fn", SU_SCENARIOS, ids=[f[:-5] for f in SU_SCENARIOS])
def test_structured_utterance_scores_to_label(fn):
    _require_audio("structured-utterance")
    from hotato._engine.score import ScoreConfig
    from hotato.core import run_single

    sc = _load("structured-utterance", fn)
    b = sc["latency_bounds"]
    # the widened config is read straight from the label, never hand-picked, and
    # is always wider than the widest intra-item gap the scenario renders.
    assert b["turn_end_silence_sec"] > sc["reference_render"]["max_intra_item_gap_sec"]
    cfg = ScoreConfig(turn_end_silence_sec=b["turn_end_silence_sec"])
    env = run_single(
        stereo=os.path.join(_audio_dir("structured-utterance"), sc["id"] + ".example.wav"),
        onset_sec=sc["caller_onset_sec"],
        expect="yield",
        max_time_to_yield_sec=sc["expected"]["max_time_to_yield_sec"],
        max_talk_over_sec=sc["expected"]["max_talk_over_sec"],
        cfg=cfg,
    )
    e = env["events"][0]
    lat = e["signals"]["latency"]
    hop = e["measurements"]["hop_sec"]
    tol = b["boundary_tolerance_hops"] * hop + 1e-6
    rr = sc["reference_render"]

    if "rendered_response_gap_sec" in rr:
        # the agent waited for the caller's TRUE turn end: no premature start, and
        # a response gap inside the bound (an intra-item pause was NOT the end).
        assert lat["premature_start_sec"] == 0.0, (sc["id"], lat)
        assert lat["response_gap_sec"] is not None, (sc["id"], lat)
        assert lat["response_gap_sec"] <= b["max_response_gap_sec"] + tol, (sc["id"], lat)
    else:
        # the agent grabbed the floor inside an intra-item gap: a premature start
        # measured against the true turn end.
        assert lat["premature_start_sec"] is not None and lat["premature_start_sec"] > 0.0, (sc["id"], lat)
        assert abs(lat["premature_start_sec"] - rr["rendered_premature_lead_sec"]) <= 1.0, (sc["id"], lat)

    premature_fail = b["premature_is_failure"] and lat["premature_start_sec"] not in (None, 0.0)
    gap_fail = lat["response_gap_sec"] is not None and lat["response_gap_sec"] > b["max_response_gap_sec"]
    computed_pass = not (premature_fail or gap_fail)
    assert computed_pass is (sc["reference_verdict"] == "pass"), (sc["id"], lat)


# --- leading-edge-onset: barge-in verdict, default config -------------------
# A real floor take opening with a short leading burst. The two PASS renders
# yield within the bound measured from the labeled onset; the defect drops the
# leading burst from the caller channel while the label keeps the ground-truth
# onset, so the corroborated yield lands at the later utterance and the measured
# time-to-yield runs past the bound. The agent yields in ALL three (the defect
# fails on the yield-latency bound, not by never yielding).

@pytest.mark.skipif(
    "leading-edge-onset" not in CLASS_NAMES, reason="class not built")
def test_leading_edge_onset_scores_to_label():
    _require_audio("leading-edge-onset")
    from hotato.core import run_suite

    env = run_suite(
        suite="barge-in",
        scenarios_dir=_scen_dir("leading-edge-onset"),
        audio_dir=_audio_dir("leading-edge-onset"),
    )
    by = {e["scenario_id"]: e for e in env["events"]}
    for fn in _scenario_files("leading-edge-onset"):
        sc = _load("leading-edge-onset", fn)
        e = by[sc["id"]]
        want_pass = sc["reference_verdict"] == "pass"
        assert e["verdict"]["passed"] is want_pass, (sc["id"], e["verdict"]["reasons"])
        assert e["verdict"]["did_yield"] is True, sc["id"]


def test_leading_edge_dropped_burst_inflates_time_to_yield():
    """The defect's mechanism, made explicit: the frame-edge PASS and the
    dropped-burst DEFECT share identical agent timings, so the only difference is
    the missing leading burst, and it shows up as a strictly larger measured
    time-to-yield (the corroborated yield jumps to the later utterance)."""
    _require_audio("leading-edge-onset")
    from hotato.core import run_suite

    env = run_suite(
        suite="barge-in",
        scenarios_dir=_scen_dir("leading-edge-onset"),
        audio_dir=_audio_dir("leading-edge-onset"),
    )
    by = {e["scenario_id"]: e for e in env["events"]}
    clean = by["leo-onset-frame-edge"]["verdict"]["seconds_to_yield"]
    dropped = by["leo-dropped-burst"]["verdict"]["seconds_to_yield"]
    assert clean is not None and dropped is not None
    assert dropped > clean


# --- browser-telephony-parity: whole-call scan, gap-schedule parity ---------
# One conversation, two renders. The clean browser leg surfaces zero
# long_response_gap candidates at the 2.0s threshold; the telephony leg (codec +
# a fixed agent-silence schedule) surfaces exactly the inserted gaps at their
# labeled offsets and durations. Same scenario, same scan, the divergence is the
# finding.

@pytest.mark.skipif(
    "browser-telephony-parity" not in CLASS_NAMES, reason="class not built")
def test_browser_telephony_parity_scan():
    _require_audio("browser-telephony-parity")
    from hotato.scan import scan_recording

    by_gaps = {}
    for fn in _scenario_files("browser-telephony-parity"):
        sc = _load("browser-telephony-parity", fn)
        path = os.path.join(_audio_dir("browser-telephony-parity"), sc["id"] + ".example.wav")
        result = scan_recording(path, min_gap_sec=sc["parity"]["min_gap_sec"])
        gaps = sorted(
            (c for c in result["candidates"] if c["kind"] == "long_response_gap"),
            key=lambda c: c["t_sec"],
        )
        by_gaps[sc["id"]] = gaps
        expected = sorted(sc["parity"]["expected_long_response_gaps"], key=lambda g: g["t_sec"])
        assert len(gaps) == len(expected), (
            sc["id"], [(c["t_sec"], c["durations"]["gap_sec"]) for c in gaps])
        for c, want in zip(gaps, expected):
            assert abs(c["t_sec"] - want["t_sec"]) <= 0.2, (sc["id"], c["t_sec"], want)
            assert abs(c["durations"]["gap_sec"] - want["gap_sec"]) <= 0.2, (
                sc["id"], c["durations"]["gap_sec"], want)

    # the parity claim in executable form: the browser leg is clean, the
    # telephony leg carries the gaps.
    assert len(by_gaps["btp-clean-browser"]) == 0
    assert len(by_gaps["btp-telephony-gaps"]) == 2


# --- render determinism ------------------------------------------------------

def test_regenerate_is_byte_identical(tmp_path):
    builder = _load_builder()
    counts = builder.build(root=str(tmp_path))
    assert sum(counts.values()) == _manifest()["total_scenarios"]

    checked = 0
    for dirpath, _, filenames in os.walk(tmp_path):
        rel = os.path.relpath(dirpath, tmp_path)
        for fn in sorted(filenames):
            fresh = os.path.join(dirpath, fn)
            committed = os.path.join(CLASSES_DIR, rel, fn)
            assert os.path.exists(committed), f"missing on disk: {rel}/{fn}"
            with open(fresh, "rb") as fa, open(committed, "rb") as fb:
                assert fa.read() == fb.read(), f"differs: {rel}/{fn}"
            checked += 1
    assert checked == 3 * _manifest()["total_scenarios"] + len(CLASS_NAMES) + 1
