"""P4: the tiered synthetic scenario suites under corpus/suites/.

Four suites (silver, silver-defects, gold, gold-defects), ~112 scenarios, all
SYNTHETIC and labeled synthetic: deterministic shaped noise rendered from each
scenario's own reference_render timings (seed = sha256(id)), so the timings are
the ground truth and two renders are byte-identical on any machine.

What is asserted here:
  - the suites manifest matches what is on disk, file for file;
  - every scenario JSON conforms to the corpus scenario shape and its honesty
    rules (source_type synthetic, no accuracy claims, no em or en dashes);
  - run_suite over each suite reproduces every labeled reference verdict:
    reference renders PASS, defect renders FAIL on their labeled axis;
  - the latency axis measures within the labeled hop tolerance of the rendered
    ground truth and passes/fails exactly as labeled;
  - a fresh regenerate of every suite (JSON and WAV) is byte-identical to the
    committed corpus.
"""

import importlib.util
import json
import os
import sys

import pytest

from hotato.core import run_single, run_suite
from hotato._engine.score import ScoreConfig
from hotato._engine.vad import VADParams

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITES_DIR = os.path.join(REPO, "corpus", "suites")
BUILDER_PATH = os.path.join(SUITES_DIR, "build_suites.py")

with open(os.path.join(SUITES_DIR, "manifest.json"), encoding="utf-8") as fh:
    MANIFEST = json.load(fh)

SUITE_NAMES = [s["name"] for s in MANIFEST["suites"]]
SUITE_INFO = {s["name"]: s for s in MANIFEST["suites"]}


def _scen_dir(name):
    return os.path.join(SUITES_DIR, name, "scenarios")


def _audio_dir(name):
    return os.path.join(SUITES_DIR, name, "audio")


def _scenario_files(name):
    return sorted(
        fn for fn in os.listdir(_scen_dir(name))
        if fn.endswith(".json") and fn != "manifest.json"
    )


def _load(name, fn):
    with open(os.path.join(_scen_dir(name), fn), encoding="utf-8") as fh:
        return json.load(fh)


ALL_SCENARIOS = [(name, fn) for name in SUITE_NAMES for fn in _scenario_files(name)]


@pytest.fixture(scope="module")
def suite_envelopes():
    """Run each suite once through the real entry point and cache the result."""
    return {
        name: run_suite(
            suite="barge-in", scenarios_dir=_scen_dir(name), audio_dir=_audio_dir(name)
        )
        for name in SUITE_NAMES
    }


# --- manifest vs disk -------------------------------------------------------

def test_manifest_lists_all_suites_on_disk():
    # a suite dir is one that carries a scenarios/ subdir (ignores __pycache__)
    on_disk = sorted(
        d for d in os.listdir(SUITES_DIR)
        if os.path.isdir(os.path.join(SUITES_DIR, d, "scenarios"))
    )
    assert sorted(SUITE_NAMES) == on_disk


@pytest.mark.parametrize("name", SUITE_NAMES)
def test_manifest_matches_disk(name):
    info = SUITE_INFO[name]
    files = _scenario_files(name)
    assert info["scenarios"] == len(files), name

    with open(os.path.join(_scen_dir(name), "manifest.json"), encoding="utf-8") as fh:
        suite_manifest = json.load(fh)
    listed = {e["id"]: e for e in suite_manifest["scenarios"]}
    assert sorted(listed) == sorted(fn[:-5] for fn in files)

    audio = set(os.listdir(_audio_dir(name)))
    for fn in files:
        sc = _load(name, fn)
        sid = sc["id"]
        entry = listed[sid]
        # the per-suite manifest mirrors the scenario labels exactly
        assert entry["category"] == sc["category"]
        assert entry["family"] == sc["family"]
        assert entry["sample_rate"] == sc["sample_rate"]
        assert entry["expected_yield"] == sc["expected"]["yield"]
        assert entry["reference_verdict"] == sc["reference_verdict"]
        assert entry["failure_axis"] == sc.get("failure_axis")
        # both fixtures exist for every scenario
        assert sid + ".example.wav" in audio, sid
        assert sid + ".caller.wav" in audio, sid
    # no orphan audio
    assert audio == {
        fn[:-5] + suffix for fn in files for suffix in (".example.wav", ".caller.wav")
    }
    # counts in the suites manifest are internally consistent
    assert info["barge_in_pass"] + info["barge_in_fail"] == info["scenarios"]
    assert info["expected_exit_code"] == (1 if info["barge_in_fail"] else 0)


def test_manifest_total_matches_sum():
    assert MANIFEST["total_scenarios"] == sum(
        SUITE_INFO[n]["scenarios"] for n in SUITE_NAMES
    )
    assert MANIFEST["synthetic"] is True


def test_manifest_dimensions_recompute():
    scenarios = [_load(name, fn) for name, fn in ALL_SCENARIOS]
    dims = MANIFEST["dimensions"]
    assert dims["sample_rates"] == sorted({sc["sample_rate"] for sc in scenarios})
    assert dims["families"] == sorted({sc["family"] for sc in scenarios})
    assert dims["max_duration_sec"] == max(sc["duration_sec"] for sc in scenarios)
    assert dims["noise_floor_amps"] == sorted(
        {sc["reference_render"].get("noise_floor_amp", 0.0006) for sc in scenarios}
    )


# --- corpus size and identity -----------------------------------------------

def _existing_set_ids():
    """Scenario ids from the pre-existing sets: the frozen bundled battery,
    examples/, and examples/funnel-demo/."""
    dirs = [
        os.path.join(REPO, "src", "hotato", "data", "scenarios"),
        os.path.join(REPO, "examples", "scenarios"),
        os.path.join(REPO, "examples", "funnel-demo", "scenarios"),
    ]
    ids = []
    for d in dirs:
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json") and fn != "manifest.json":
                ids.append(fn[:-5])
    return ids


def test_total_corpus_is_120_plus():
    existing = _existing_set_ids()
    total = len(existing) + len(ALL_SCENARIOS)
    assert len(ALL_SCENARIOS) >= 104
    assert total >= 120, f"corpus has {total} scenarios"


def test_ids_unique_across_suites_and_existing_sets():
    suite_ids = [fn[:-5] for _, fn in ALL_SCENARIOS]
    assert len(suite_ids) == len(set(suite_ids))
    clash = set(suite_ids) & set(_existing_set_ids())
    assert not clash, f"suite ids collide with existing sets: {clash}"


# --- scenario schema shape and honesty rules --------------------------------

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

    # identity and typing
    assert sc["id"] == fn[:-5]
    assert isinstance(sc["title"], str) and sc["title"]
    assert sc["category"] in ("should_yield", "should_not_yield", "latency")
    assert isinstance(sc["tags"], list) and sc["tags"]
    assert all(isinstance(t, str) for t in sc["tags"])
    assert isinstance(sc["family"], str) and sc["family"]
    assert sc["source_type"] == "synthetic"
    assert sc["sample_rate"] in (8000, 16000)
    dur = sc["duration_sec"]
    assert isinstance(dur, (int, float)) and dur > 0
    onset = sc["caller_onset_sec"]
    assert isinstance(onset, (int, float)) and 0 <= onset < dur

    # expected block, consistent with the category
    exp = sc["expected"]
    assert set(exp) == {"yield", "max_time_to_yield_sec", "max_talk_over_sec"}
    assert isinstance(exp["yield"], bool)
    if sc["category"] == "should_not_yield":
        assert exp["yield"] is False
        assert exp["max_time_to_yield_sec"] is None
        assert exp["max_talk_over_sec"] is None
    else:
        assert exp["yield"] is True
        assert exp["max_time_to_yield_sec"] > 0
        assert exp["max_talk_over_sec"] > 0

    # reference render: exact timings, all inside the file
    rr = sc["reference_render"]
    assert isinstance(rr["agent_segments_sec"], list) and rr["agent_segments_sec"]
    segs = list(rr["agent_segments_sec"]) + list(rr.get("caller_segments_sec", []))
    for s, e in segs:
        assert 0 <= s < e <= dur + 1e-9, (sc["id"], s, e)
    if rr.get("caller_is_echo_of_agent"):
        assert rr["caller_segments_sec"] == []
        assert rr["echo_delay_sec"] > 0
        assert rr["echo_gain"] > 0
    else:
        assert isinstance(rr.get("caller_segments_sec"), list)
        if sc["category"] != "should_not_yield":
            assert rr["caller_segments_sec"]

    # verdict labels
    assert sc["reference_verdict"] in ("pass", "fail")
    if sc["reference_verdict"] == "fail":
        assert sc["failure_axis"] in ("barge_in", "latency")
    else:
        assert "failure_axis" not in sc

    # latency scenarios carry their exposed bounds and rendered ground truth
    if sc["category"] == "latency":
        b = sc["latency_bounds"]
        assert b["max_response_gap_sec"] > 0
        assert isinstance(b["premature_is_failure"], bool)
        assert b["boundary_tolerance_hops"] >= 1
        assert rr.get("continuous") is True
        assert ("rendered_response_gap_sec" in rr) != ("rendered_premature_lead_sec" in rr)

    # docs fields
    assert isinstance(sc["why_it_matters"], str) and sc["why_it_matters"].strip()
    assert "\n" not in sc["why_it_matters"]
    assert isinstance(sc["related_signals"], list) and sc["related_signals"]

    # honesty: synthetic stays synthetic, no accuracy claims, no typographic dashes
    blob = " ".join(_all_strings(sc))
    assert "%" not in blob, sc["id"]
    assert "accuracy" not in blob.lower(), sc["id"]
    assert "—" not in blob and "–" not in blob, sc["id"]
    # defect renders declare themselves in the visible copy
    if sc["reference_verdict"] == "fail":
        assert "DEFECT RENDER" in sc["why_it_matters"], sc["id"]
        assert "FAIL" in sc["title"], sc["id"]


# --- reference verdicts through the real entry point ------------------------

@pytest.mark.parametrize("name", SUITE_NAMES)
def test_suite_reference_verdicts(name, suite_envelopes):
    env = suite_envelopes[name]
    info = SUITE_INFO[name]
    by = {e["scenario_id"]: e for e in env["events"]}
    assert env["summary"]["events"] == info["scenarios"]

    for fn in _scenario_files(name):
        sc = _load(name, fn)
        e = by[sc["id"]]
        # a latency-axis defect still passes the barge-in verdict; every other
        # label maps 1:1 onto run_suite's pass/fail
        should_pass = (
            sc["reference_verdict"] == "pass" or sc.get("failure_axis") == "latency"
        )
        assert e["verdict"]["passed"] is should_pass, (
            sc["id"], e["verdict"]["reasons"])
        if not should_pass:
            assert e["fix"] is not None, sc["id"]

    assert env["summary"]["passed"] == info["barge_in_pass"]
    assert env["summary"]["failed"] == info["barge_in_fail"]
    assert env["exit_code"] == info["expected_exit_code"]


# --- the latency axis, against rendered ground truth ------------------------

def _no_hangover_cfg():
    return ScoreConfig(
        caller_vad=VADParams(hangover_sec=0.0),
        agent_vad=VADParams(hangover_sec=0.0),
    )


def _latency_passes(lat, bounds):
    if bounds.get("premature_is_failure", True) and lat["premature_start_sec"] not in (None, 0.0):
        return False
    gap = lat["response_gap_sec"]
    if gap is not None and gap > bounds["max_response_gap_sec"]:
        return False
    return True


LATENCY_SCENARIOS = [
    (name, fn) for name, fn in ALL_SCENARIOS
    if _load(name, fn)["category"] == "latency"
]


@pytest.mark.parametrize("name,fn", LATENCY_SCENARIOS,
                         ids=[f"{n}/{f[:-5]}" for n, f in LATENCY_SCENARIOS])
def test_latency_axis_as_labeled(name, fn):
    sc = _load(name, fn)
    env = run_single(
        stereo=os.path.join(_audio_dir(name), sc["id"] + ".example.wav"),
        onset_sec=sc["caller_onset_sec"],
        expect="yield",
        max_time_to_yield_sec=sc["expected"]["max_time_to_yield_sec"],
        max_talk_over_sec=sc["expected"]["max_talk_over_sec"],
        cfg=_no_hangover_cfg(),
    )
    e = env["events"][0]
    lat = e["signals"]["latency"]
    hop = e["measurements"]["hop_sec"]
    b = sc["latency_bounds"]
    tol = b["boundary_tolerance_hops"] * hop + 1e-6
    rr = sc["reference_render"]

    # the barge-in axis is clean in every latency fixture
    assert e["verdict"]["passed"] is True, e["verdict"]

    # measured timing matches the rendered ground truth within the tolerance
    if "rendered_response_gap_sec" in rr:
        assert lat["premature_start_sec"] == 0.0
        assert lat["response_gap_sec"] is not None
        assert abs(lat["response_gap_sec"] - rr["rendered_response_gap_sec"]) <= tol, lat
    else:
        assert lat["premature_start_sec"] is not None
        assert lat["premature_start_sec"] > 0.0
        assert abs(lat["premature_start_sec"] - rr["rendered_premature_lead_sec"]) <= tol, lat

    # and the labeled latency verdict holds
    want_pass = sc["reference_verdict"] == "pass"
    assert _latency_passes(lat, b) is want_pass, (sc["id"], lat)


# --- render determinism ------------------------------------------------------

def test_regenerate_is_byte_identical(tmp_path):
    """Rebuild every suite (labels, manifests, audio) into a temp dir and
    byte-compare against the committed corpus: double-render determinism and
    label/audio provenance in one check."""
    spec = importlib.util.spec_from_file_location("hotato_build_suites", BUILDER_PATH)
    builder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = builder
    spec.loader.exec_module(builder)

    counts = builder.build(root=str(tmp_path))
    assert sum(counts.values()) == MANIFEST["total_scenarios"]

    checked = 0
    for dirpath, _, filenames in os.walk(tmp_path):
        rel = os.path.relpath(dirpath, tmp_path)
        for fn in sorted(filenames):
            fresh = os.path.join(dirpath, fn)
            committed = os.path.join(SUITES_DIR, rel, fn)
            assert os.path.exists(committed), f"missing on disk: {rel}/{fn}"
            with open(fresh, "rb") as fa, open(committed, "rb") as fb:
                assert fa.read() == fb.read(), f"differs: {rel}/{fn}"
            checked += 1
    # 2 WAVs + 1 JSON per scenario, a manifest per suite, the suites manifest
    assert checked == 3 * MANIFEST["total_scenarios"] + len(SUITE_NAMES) + 1
