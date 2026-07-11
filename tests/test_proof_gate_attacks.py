"""Permanent adversarial gates for the recompute proof kernel.

Each test is a forgery from the plan's red-team (Mission 1) turned into a
regression gate. They exercise the recompute/manifest/evidence kernel directly
with REAL audio that genuinely scores, so a proof can never again rest on a
hand-written verdict, a re-encoded old call, a dropped fixture, or unrelated
audio reused under the original ids.
"""
import copy
import json
import os

import pytest

from hotato import core, manifest as m, recompute as rc, evidence as ev
from tests import _trial_audio as ta


def _build_trial(tmp_path):
    """Real before(fail)/after(pass) suite over two fixtures sharing the same
    scripted caller stimulus (only the agent side changes across the fix)."""
    scen = tmp_path / "scen"; bdir = tmp_path / "before"; adir = tmp_path / "after"
    for d in (scen, bdir, adir):
        d.mkdir()
    json.dump({"id": "f1-yield", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0, "max_talk_over_sec": 1.0}},
              open(scen / "f1-yield.json", "w"))
    json.dump({"id": "f2-hold", "caller_onset_sec": 2.0,
               "expected": {"yield": False, "max_time_to_yield_sec": None, "max_talk_over_sec": None}},
              open(scen / "f2-hold.json", "w"))
    ta.talkover_call(str(bdir / "f1-yield.example.wav"))
    ta.yielded_to_backchannel_call(str(bdir / "f2-hold.example.wav"))
    ta.yielding_call(str(adir / "f1-yield.example.wav"))
    ta.holding_call(str(adir / "f2-hold.example.wav"))
    before = core.run_suite(scenarios_dir=str(scen), audio_dir=str(bdir), suffix=".example.wav")
    after = core.run_suite(scenarios_dir=str(scen), audio_dir=str(adir), suffix=".example.wav")
    json.dump(before, open(bdir / "run.json", "w"))
    json.dump(after, open(adir / "run.json", "w"))
    man = m.build_manifest(before, trial_id="t", nonce="n",
                           policy={"max_talk_over_sec": 1.0, "max_time_to_yield_sec": 1.0}, min_n=1)
    return before, str(bdir), after, str(adir), man


def test_legit_before_after_is_not_refused_and_reaches_paired(tmp_path):
    before, bdir, after, adir, man = _build_trial(tmp_path)
    assert [e["verdict"]["passed"] for e in before["events"]] == [False, False]
    assert [e["verdict"]["passed"] for e in after["events"]] == [True, True]
    r = rc.recompute_trial(before, bdir, after, adir, man)
    assert r["refusal"] is None
    # recompute-only floors at MEASURED; trust enrichment lifts to PAIRED
    assert r["evidence"]["tier"] >= ev.TIER_MEASURED
    vec = dict(r["evidence"]["vector"]); vec["input_health"] = "clean"; vec["channel_mapping"] = "confirmed"
    assert ev.classify(vec)["tier"] >= ev.TIER_PAIRED


def test_verdict_tampering_is_refused(tmp_path):
    """Rank 1: the after-side stored verdict is hand-edited to passed over
    audio that genuinely fails -> recompute detects the mismatch -> refused."""
    before, bdir, after, adir, man = _build_trial(tmp_path)
    tdir = tmp_path / "tampered"; tdir.mkdir()
    tampered = copy.deepcopy(before)  # the real FAILING audio
    for e in tampered["events"]:
        e["verdict"]["passed"] = True   # the forgery
    tampered["summary"] = {"events": 2, "passed": 2, "failed": 0, "regression": False}
    for e in before["events"]:
        name = e["audio_provenance"]["sides"][0]["path"]
        (tdir / name).write_bytes((os.path.join(bdir, name) and open(os.path.join(bdir, name), "rb").read()))
    json.dump(tampered, open(tdir / "run.json", "w"))
    r = rc.recompute_trial(before, bdir, tampered, str(tdir), man)
    assert r["refusal"] is not None
    assert r["refusal"]["kind"] == "score_mismatch"
    assert r["evidence"]["tier"] == ev.TIER_NONE


def test_same_audio_recapture_is_refused(tmp_path):
    """Header/gain/resample re-encodes reduce to: before and after decode to the
    same PCM -> not a fresh result -> refused."""
    before, bdir, after, adir, man = _build_trial(tmp_path)
    r = rc.recompute_trial(before, bdir, before, bdir, man)
    assert r["refusal"]["kind"] == "same_audio"
    assert r["evidence"]["tier"] == ev.TIER_NONE


def test_dropped_fixture_is_refused(tmp_path):
    """Rank 2: a fixture removed from a side (cherry-pick) -> incomplete
    universe -> refused, even though the remaining fixtures improved."""
    before, bdir, after, adir, man = _build_trial(tmp_path)
    subdir = tmp_path / "sub"; subdir.mkdir()
    sub = copy.deepcopy(after)
    sub["events"] = [e for e in after["events"] if e["event_id"] != "f2-hold"]
    (subdir / "f1-yield.example.wav").write_bytes(open(os.path.join(adir, "f1-yield.example.wav"), "rb").read())
    json.dump(sub, open(subdir / "run.json", "w"))
    r = rc.recompute_trial(before, bdir, sub, str(subdir), man)
    assert r["refusal"]["kind"] == "incomplete_fixture_set"


def test_unrelated_audio_reused_ids_is_not_improved(tmp_path):
    """Rank 3: unrelated audio under the original ids -> the caller stimulus no
    longer matches (or the fixture becomes not-scorable) -> never improved."""
    before, bdir, after, adir, man = _build_trial(tmp_path)
    udir = tmp_path / "unrel"; udir.mkdir()
    ta.yielding_call(str(udir / "f1-yield.example.wav"), onset=1.0)
    ta.holding_call(str(udir / "f2-hold.example.wav"), onset=1.0)
    unrel = core.run_suite(scenarios_dir=str(tmp_path / "scen"), audio_dir=str(udir), suffix=".example.wav")
    r = rc.recompute_trial(before, bdir, unrel, str(udir), man)
    assert r["refusal"] is not None
    assert r["refusal"]["kind"] in ("stimulus_mismatch", "incomplete_fixture_set", "same_audio")


def test_manifest_pins_fixture_universe_and_hash(tmp_path):
    before, bdir, after, adir, man = _build_trial(tmp_path)
    assert m.verify_manifest_hash(man)
    assert len(man["fixtures"]) == 2
    # tampering with the manifest body breaks its hash
    bad = copy.deepcopy(man); bad["policy"]["max_talk_over_sec"] = 99.0
    assert not m.verify_manifest_hash(bad)


def test_evidence_lattice_is_a_minimum_not_an_average():
    """One weak dimension caps the tier; strengths never average it up."""
    strong = {"score_integrity": "recomputed", "audio_identity": "recomputed",
              "policy_integrity": "signed", "fixture_set_integrity": "manifest_complete",
              "input_health": "clean", "channel_mapping": "confirmed", "label_authority": "human",
              "pairing_integrity": "contract_bound", "capture_origin": "runner_attested",
              "opposite_risk_guard": "present_passing"}
    assert ev.evidence_tier(strong) == ev.TIER_ATTESTED
    weak = dict(strong); weak["score_integrity"] = "envelope_only"
    assert ev.evidence_tier(weak) == ev.TIER_ASSERTED   # one weak dim pulls it all the way down
    refuse = dict(strong); refuse["audio_identity"] = "same_pcm"
    assert ev.evidence_tier(refuse) == ev.TIER_NONE


def test_fabricated_stored_pcm_is_refused(tmp_path):
    """M2: the stored provenance pcm_sha256 that disagrees with the freshly
    decoded audio is refused (identity is decoded off disk, not trusted)."""
    import copy
    before, bdir, after, adir, man = _build_trial(tmp_path)
    tampered = copy.deepcopy(after)
    tampered["events"][0]["audio_provenance"]["sides"][0]["pcm_sha256"] = "0" * 64
    r = rc.recompute_trial(before, bdir, tampered, adir, man)
    assert r["refusal"]["kind"] == "provenance_mismatch"
    assert r["evidence"]["vector"]["audio_identity"] == "mismatch"


def test_unlabeled_battery_cannot_reach_paired(tmp_path):
    """M1: a battery whose expectations are not explicit human labels caps
    label_authority below human, so the proof cannot reach PAIRED."""
    import copy
    before, bdir, after, adir, _man = _build_trial(tmp_path)
    stripped = copy.deepcopy(before)
    for e in stripped["events"]:
        e.pop("expected_yield", None)          # no explicit human label
    man = m.build_manifest(stripped, trial_id="t", nonce="n",
                           policy={"max_talk_over_sec": 1.0, "max_time_to_yield_sec": 1.0}, min_n=1)
    assert man["fixtures"][0]["label_authority"] == "none"
    r = rc.recompute_trial(before, bdir, after, adir, man)
    vec = dict(r["evidence"]["vector"]); vec["input_health"] = "clean"; vec["channel_mapping"] = "confirmed"
    assert ev.classify(vec)["tier"] < ev.TIER_PAIRED
    # while a legitimately-labelled battery still reaches PAIRED
    r2 = rc.recompute_trial(before, bdir, after, adir,
                            m.build_manifest(before, trial_id="t2", nonce="n",
                                             policy={"max_talk_over_sec": 1.0, "max_time_to_yield_sec": 1.0}, min_n=1))
    vec2 = dict(r2["evidence"]["vector"]); vec2["input_health"] = "clean"; vec2["channel_mapping"] = "confirmed"
    assert vec2["label_authority"] == "human" and ev.classify(vec2)["tier"] >= ev.TIER_PAIRED


def test_fixture_key_is_collision_free():
    """Minor: an event_id containing the old '::' separator cannot forge
    another fixture's key."""
    a = m.fixture_key({"event_id": "a", "scenario_id": "b::c"})
    b = m.fixture_key({"event_id": "a::b", "scenario_id": "c"})
    assert a != b
