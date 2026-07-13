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
