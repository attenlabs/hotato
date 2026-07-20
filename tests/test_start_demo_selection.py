"""P0 demo truth: the demo contract is built from the DECLARED missed
interruption, selected by evidence fields, never by candidate rank.

The defect this pins: ``_DEMO_CONTRACT_CANDIDATE = 2`` selected the sweep's #2
candidate by position. On the current packaged demo audio, rank 2 is an
``agent_stop_no_caller`` event, so the "FAIL as expected: the agent talked over
the caller" story shipped with a measurement of ``talk_over=0.0`` and
``seconds_to_yield=None`` (nothing was talked over at that moment). Selection
must come from the packaged scenario's own declaration
(fd-01-missed-interruption.json: audio + caller_onset_sec + expected.yield) so
reordering the sweep can never change which moment the first run shows.
"""

import json
import os

import pytest

from hotato import start as S
from hotato.failure_record import SelectorError


def _demo_sweep(tmp_path):
    """Run the real bundled demo sweep once and return (sweep_dict, out_dir)."""
    out = str(tmp_path)
    S._sweep_demo(out)
    with open(os.path.join(out, S._SWEEP_JSON), encoding="utf-8") as fh:
        return json.load(fh), out


# --- the defect, end to end ---------------------------------------------------

def test_demo_contract_is_built_from_the_declared_missed_interruption(tmp_path):
    sweep, out = _demo_sweep(tmp_path)
    info = S._create_and_verify_demo_contract(out, os.path.join(out, S._SWEEP_JSON))
    contract = json.load(open(os.path.join(info["bundle_dir"], "contract.json"),
                              encoding="utf-8"))
    scenario = S._demo_scenario()
    # the contract's moment is the scenario's declared missed interruption
    assert contract["source"]["candidate_kind"] == "overlap_while_agent_talking"
    assert abs(float(contract["event"]["source_onset_sec"])
               - float(scenario["caller_onset_sec"])) <= S._DEMO_ONSET_TOLERANCE_SEC
    # trust-scorable and measurement-scorable, and the FAIL story is measured:
    # the agent kept talking over the caller, so talk_over is a positive number.
    assert info["scorable"] is True
    assert info["passed"] is False  # fails by design: the interruption was missed
    from hotato import contract as C
    verify = C.verify_contracts(info["contracts_dir"])
    res = verify["results"][0]
    assert res["scorable"] is True
    # the FAIL story is measured: the agent kept talking over the caller
    assert res["measurement"]["talk_over_sec"] > 0.0


# --- selector semantics -------------------------------------------------------

def test_selector_is_stable_under_reordering(tmp_path):
    sweep, _ = _demo_sweep(tmp_path)
    scenario = S._demo_scenario()
    rank = S._select_demo_candidate(sweep, scenario)
    chosen = sweep["candidates"][rank - 1]
    reordered = dict(sweep)
    reordered["candidates"] = list(reversed(sweep["candidates"]))
    rank2 = S._select_demo_candidate(reordered, scenario)
    chosen2 = reordered["candidates"][rank2 - 1]
    assert (chosen["source"], chosen["kind"], chosen["t_sec"]) == \
           (chosen2["source"], chosen2["kind"], chosen2["t_sec"])


def test_selector_zero_matches_is_a_distinct_internal_error(tmp_path):
    sweep, _ = _demo_sweep(tmp_path)
    scenario = dict(S._demo_scenario())
    scenario["audio"] = "does-not-exist.wav"
    with pytest.raises(S.DemoCandidateNotFound):
        S._select_demo_candidate(sweep, scenario)


def test_selector_multiple_matches_is_a_distinct_internal_error(tmp_path):
    sweep, _ = _demo_sweep(tmp_path)
    scenario = S._demo_scenario()
    rank = S._select_demo_candidate(sweep, scenario)
    dup = dict(sweep)
    dup["candidates"] = list(sweep["candidates"]) + [dict(sweep["candidates"][rank - 1])]
    with pytest.raises(S.DemoCandidateAmbiguous):
        S._select_demo_candidate(dup, scenario)
    # the two error types are distinct classes under one internal-contract root
    assert issubclass(S.DemoCandidateNotFound, S.DemoSelectionError)
    assert issubclass(S.DemoCandidateAmbiguous, S.DemoSelectionError)
    assert S.DemoCandidateNotFound is not S.DemoCandidateAmbiguous


def test_selector_ignores_malformed_candidates(tmp_path):
    sweep, _ = _demo_sweep(tmp_path)
    scenario = S._demo_scenario()
    rank = S._select_demo_candidate(sweep, scenario)
    keep = sweep["candidates"][rank - 1]
    # malformed entries (missing fields, wrong types) can never be selected
    junk = [{}, {"kind": "overlap_while_agent_talking"},
            {"kind": "overlap_while_agent_talking", "source": scenario["audio"],
             "t_sec": "not-a-number"}]
    mixed = dict(sweep)
    mixed["candidates"] = junk + [keep]
    assert S._select_demo_candidate(mixed, scenario) == len(junk) + 1


def test_repeated_demo_runs_select_the_same_event(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, out_a = _demo_sweep(tmp_path / "a")
    b, out_b = _demo_sweep(tmp_path / "b")
    scenario = S._demo_scenario()
    ca = a["candidates"][S._select_demo_candidate(a, scenario) - 1]
    cb = b["candidates"][S._select_demo_candidate(b, scenario) - 1]
    assert (ca["source"], ca["kind"], ca["t_sec"]) == (cb["source"], cb["kind"], cb["t_sec"])


# --- Slice C: the share-safe Failure Record is projected BY CONTRACT ID -------

_RECORD_FILES = ("failure-record.json", "failure-record.md",
                 "failure-record.html", "failure-record.svg")


def _real_verify(tmp_path):
    """Run the real bundled demo contract create+verify once and return the
    FULL in-memory contract-verify envelope."""
    out = str(tmp_path)
    os.makedirs(out, exist_ok=True)
    S._sweep_demo(out)
    info = S._create_and_verify_demo_contract(out, os.path.join(out, S._SWEEP_JSON))
    return info["verify"]


def _contract_info(verify):
    """Wrap a verify envelope in the ``contract_info`` shape
    ``_write_demo_failure_record`` now takes (it carries the bundle location so
    the record card can add the caught-moment timeline). The bundle_rel here
    points at no real event.wav, so the render-only timeline stays absent and
    these tests exercise the projection path exactly as before."""
    return {"verify": verify,
            "bundle_rel": f"contracts/{S._DEMO_CONTRACT_ID}.hotato"}


def _decoy_failing_result(contract_id="decoy-failing-contract"):
    """A second, FAILING contract-verify result. If the demo picked its moment
    by POSITION (first failing result), a decoy placed first would win; because
    selection is by SELECTOR _DEMO_CONTRACT_ID, the decoy is never chosen."""
    return {
        "id": contract_id,
        "dir": "/should/never/leak/DECOY-PATH",
        "expect": "yield",
        "passed": False,
        "scorable": True,
        "verdict_eligible": True,
        "verdict_ineligible_reason": None,
        "not_scorable_reason": None,
        "measurement": {"did_yield": False, "seconds_to_yield": None,
                        "talk_over_sec": 1.5},
        "authenticity": "unsigned",
        "authenticated": False,
        "authenticity_reason": None,
        "assertions": None,
    }


def test_write_demo_record_selects_by_contract_id_not_position(tmp_path):
    """The demo record is projected by SELECTOR _DEMO_CONTRACT_ID, never by
    position: a FAILING decoy placed before/after the real contract, in either
    order, never wins -- the frozen moment is always the demo's own contract."""
    verify = _real_verify(tmp_path / "src")
    real = verify["results"][0]
    assert real["id"] == S._DEMO_CONTRACT_ID

    before = dict(verify, results=[_decoy_failing_result(), real])
    after = dict(verify, results=[real, _decoy_failing_result()])

    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    S._write_demo_failure_record(str(tmp_path / "b"), _contract_info(before))
    S._write_demo_failure_record(str(tmp_path / "a"), _contract_info(after))

    records = {}
    for key, base in (("before", tmp_path / "b"), ("after", tmp_path / "a")):
        records[key] = json.load(
            open(base / S._DEMO_RECORD_DIR / "failure-record.json",
                 encoding="utf-8"))
        # the demo's own contract is the frozen moment in BOTH orderings, never
        # the decoy (id-based selection is position-independent)
        assert records[key]["subject"]["test_id"] == S._DEMO_CONTRACT_ID
        assert records[key]["status"] == "FAIL"
    # the selected moment's evidence (its headline) does not depend on where the
    # decoy sat in the result list
    assert records["before"]["headline"] == records["after"]["headline"]


def test_write_demo_record_does_not_copy_source_paths_or_media(tmp_path):
    """No source envelope, absolute path, or media locator from the verify
    envelope leaks into the share directory or any record file."""
    verify = _real_verify(tmp_path / "src")
    # Plant unmistakable markers on the envelope's non-projected fields (the
    # per-run absolute bundle dir, and a decoy result's own dir).
    verify = dict(verify, dir="/tmp/SENTINEL-9f3a77/should-not-leak")
    decoy = _decoy_failing_result()
    decoy["dir"] = "/tmp/SENTINEL-9f3a77/DECOY.wav"
    verify = dict(verify, results=[verify["results"][0], decoy])

    out = tmp_path / "out"
    out.mkdir()
    S._write_demo_failure_record(str(out), _contract_info(verify))

    rec_dir = out / S._DEMO_RECORD_DIR
    # only the four record files are written into the share directory
    assert sorted(p.name for p in rec_dir.iterdir()) == sorted(_RECORD_FILES)
    # the planted markers appear in NONE of them
    for name in _RECORD_FILES:
        blob = (rec_dir / name).read_text(encoding="utf-8")
        assert "SENTINEL-9f3a77" not in blob, name
        assert "should-not-leak" not in blob, name
        assert "DECOY" not in blob, name


def test_write_demo_record_is_an_essential_invariant_not_swallowed(tmp_path):
    """Record generation is an essential demo invariant: if the selector no
    longer resolves (the envelope has no demo-missed-interruption contract), the
    failure RAISES rather than being silently swallowed or faked."""
    verify = _real_verify(tmp_path / "src")
    # rename the only result's id so the selector matches nothing
    broken = dict(verify)
    broken["results"] = [dict(verify["results"][0], id="renamed-away")]
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SelectorError):
        S._write_demo_failure_record(str(out), _contract_info(broken))
    # nothing half-written: the selector fails before any record file lands
    assert not (out / S._DEMO_RECORD_DIR).exists()


def test_projection_envelope_strips_nondeterministic_absolute_paths(tmp_path):
    """The projection view drops the per-run absolute bundle ``dir`` fields so
    the record's content address depends only on the re-scored evidence."""
    verify = _real_verify(tmp_path / "src")
    verify = dict(verify, dir="/run/one/abs/path")
    doc = S._projection_envelope(verify)
    assert "dir" not in doc
    assert all("dir" not in r for r in doc["results"])
    # the original envelope is untouched (deep copy)
    assert verify["dir"] == "/run/one/abs/path"
