"""Release-blocking adversarial regression tests for the evidence-integrity
holes an external 0.10.0 audit found and 1.0.1 closed.

Each test replicates one audit attack verbatim and asserts the fix holds, so a
future refactor can never silently reopen a forgery path. Named by the finding
(P0-1 .. P1-9) for traceability.

Zero-network, deterministic; drives the same public API/CLI the audit used.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import wave

import pytest

from tests import _trial_audio as ta
import hotato.core as core
import hotato.evidence as ev
import hotato.manifest as manifest
import hotato.recompute as recompute
import hotato.receipt as receipt
import hotato.contract as contract
from hotato.fleet.api import FleetAPI


# --- helpers ---------------------------------------------------------------

def _yield_trial(root, *, with_hold_guard=False):
    """A clean before/after pair: 1+ yield target(s) fail->pass, same caller
    stimulus, distinct agent audio. Returns (before_env, bdir, after_env, adir,
    manifest)."""
    scen, bdir, adir = (os.path.join(root, d) for d in ("scen", "before", "after"))
    for d in (scen, bdir, adir):
        os.makedirs(d, exist_ok=True)
    json.dump({"id": "f1", "caller_onset_sec": 2.0,
               "expected": {"yield": True, "max_time_to_yield_sec": 1.0,
                            "max_talk_over_sec": 1.0}},
              open(os.path.join(scen, "f1.json"), "w"))
    ta.talkover_call(os.path.join(bdir, "f1.example.wav"))   # fails yield
    ta.yielding_call(os.path.join(adir, "f1.example.wav"))   # passes yield
    before = core.run_suite(scenarios_dir=scen, audio_dir=bdir, suffix=".example.wav")
    after = core.run_suite(scenarios_dir=scen, audio_dir=adir, suffix=".example.wav")
    man = manifest.build_manifest(
        before, trial_id="t", nonce="n",
        policy={"max_talk_over_sec": 1.0, "max_time_to_yield_sec": 1.0}, min_n=1)
    return before, bdir, after, adir, man


# --- P0-1: contract media binding ------------------------------------------

def test_p0_1_contract_audio_swap_is_tampered(tmp_path, monkeypatch):
    """Replacing a signed contract's bundled audio (fail -> pass) must flip it to
    tampered + not authenticated + not passing -- never an authenticated pass."""
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "audit-secret-key")
    fail = str(tmp_path / "fail.wav"); ok = str(tmp_path / "pass.wav")
    ta.talkover_call(fail, onset=5.0, total=12)
    ta.yielding_call(ok, onset=5.0, total=12)
    res = contract.create_contract(
        stereo=fail, onset_sec=5.0, expect="yield", contract_id="auth-swap",
        out_dir=str(tmp_path / "b"), no_clip=True,
        max_talk_over_sec=0.5, max_time_to_yield_sec=0.5)
    bundle = res["dir"]
    r1 = contract.verify_contracts(bundle)["results"][0]
    assert r1["authenticated"] is True and r1["passed"] is False
    # swap ONLY the audio; contract.json + attestation.json untouched
    shutil.copyfile(ok, os.path.join(bundle, "audio", "event.wav"))
    v2 = contract.verify_contracts(bundle)
    r2 = v2["results"][0]
    assert r2["authenticity"] == "tampered"
    assert r2["authenticated"] is False
    assert r2["passed"] is False
    assert v2["summary"]["passed"] == 0


# --- P0-2: capture receipts are verified -----------------------------------

def test_p0_2_dummy_receipt_does_not_grant_runner_attested(tmp_path):
    before, bdir, after, adir, man = _yield_trial(tmp_path)
    r = recompute.recompute_trial(before, bdir, after, adir, man,
                                  capture_receipts={"anything": {"not": "a receipt"}})
    assert r["evidence"]["vector"]["capture_origin"] != "runner_attested"
    assert r["refusal"] is not None and r["refusal"]["kind"] == "invalid_receipt"


def test_p0_2_wrong_pcm_and_wrong_trial_receipts_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "k1")
    before, bdir, after, adir, man = _yield_trial(tmp_path)
    fk = list(manifest.fixture_index(man).keys())[0]
    base = recompute.recompute_trial(before, bdir, after, adir, man)
    after_pcm = base["per_fixture"]["after"][fk]["pcm_sha256"]
    wrong_pcm = {fk: receipt.build_receipt(
        trial_id="t", nonce="n", recording_locator=fk, raw_sha256="x",
        pcm_sha256="deadbeef", runner="vapi", key=b"k1")}
    wrong_trial = {fk: receipt.build_receipt(
        trial_id="OTHER", nonce="n", recording_locator=fk, raw_sha256="x",
        pcm_sha256=after_pcm, runner="vapi", key=b"k1")}
    for bad in (wrong_pcm, wrong_trial):
        r = recompute.recompute_trial(before, bdir, after, adir, man, capture_receipts=bad)
        assert r["refusal"]["kind"] == "invalid_receipt"


def test_p0_2_valid_signed_receipt_grants_runner_attested(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "k1")
    before, bdir, after, adir, man = _yield_trial(tmp_path)
    fk = list(manifest.fixture_index(man).keys())[0]
    base = recompute.recompute_trial(before, bdir, after, adir, man)
    after_pcm = base["per_fixture"]["after"][fk]["pcm_sha256"]
    good = {fk: receipt.build_receipt(
        trial_id="t", nonce="n", recording_locator=fk, raw_sha256="x",
        pcm_sha256=after_pcm, runner="vapi", key=b"k1")}
    r = recompute.recompute_trial(before, bdir, after, adir, man, capture_receipts=good)
    assert r["refusal"] is None
    assert r["evidence"]["vector"]["capture_origin"] == "runner_attested"


# --- P0-3: operator-asserted != fresh-recapture green ----------------------

def test_p0_3_operator_asserted_is_not_fresh_recapture_green():
    # a paired result whose recapture origin is only operator-asserted must NOT
    # borrow the attested "fresh-recapture" headline/green.
    vec = {"score_integrity": "recomputed", "audio_identity": "recomputed",
           "policy_integrity": "manifest_pinned", "fixture_set_integrity": "manifest_complete",
           "input_health": "clean", "channel_mapping": "confirmed", "label_authority": "human",
           "pairing_integrity": "contract_bound", "capture_origin": "operator_asserted",
           "opposite_risk_guard": "present_passing"}
    c = ev.classify(vec)
    assert "FRESH-RECAPTURE" not in c["headline"]
    assert "OPERATOR-ASSERTED" in c["headline"]
    # only a runner-attested vector earns the fresh-recapture green
    vec2 = dict(vec, capture_origin="runner_attested", policy_integrity="signed")
    assert ev.classify(vec2)["headline"] == "PAIRED FRESH-RECAPTURE IMPROVED"


# --- P0-4: Fleet requires a real improvement + honors --min-n --------------

def test_p0_4_fleet_all_pass_is_not_improved(tmp_path):
    # all-pass before AND after: zero fail->pass transitions -> never "improved".
    scen, ad = os.path.join(tmp_path, "s"), os.path.join(tmp_path, "ap")
    os.makedirs(scen); os.makedirs(ad)
    for i in range(4):
        sid = f"p{i}"
        json.dump({"id": sid, "caller_onset_sec": 1.0,
                   "expected": {"yield": True, "max_time_to_yield_sec": 1.0,
                                "max_talk_over_sec": 1.0}},
                  open(os.path.join(scen, f"{sid}.json"), "w"))
        ta.yielding_call(os.path.join(ad, f"{sid}.example.wav"), onset=1.0)
    allpass = core.run_suite(scenarios_dir=scen, audio_dir=ad, suffix=".example.wav")
    home = str(tmp_path / "home")
    with FleetAPI(home) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        r = api.experiment_run("a", "ag", trial_id="allpass", battery_env=allpass,
                               before_env=allpass, before_dir=ad,
                               after_env=allpass, after_dir=ad, min_n=10)
    assert r["verdict"] != "improved"


def test_p0_4_fleet_min_n_enforced_and_happy_path(tmp_path):
    scen, bd, adr = (os.path.join(tmp_path, d) for d in ("s", "bd", "ad"))
    for d in (scen, bd, adr):
        os.makedirs(d)
    for i in range(3):
        sid = f"f{i}"
        json.dump({"id": sid, "caller_onset_sec": 2.0,
                   "expected": {"yield": True, "max_time_to_yield_sec": 1.0,
                                "max_talk_over_sec": 1.0}},
                  open(os.path.join(scen, f"{sid}.json"), "w"))
        ta.talkover_call(os.path.join(bd, f"{sid}.example.wav"))
        ta.yielding_call(os.path.join(adr, f"{sid}.example.wav"))
    before = core.run_suite(scenarios_dir=scen, audio_dir=bd, suffix=".example.wav")
    after = core.run_suite(scenarios_dir=scen, audio_dir=adr, suffix=".example.wav")
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        good = api.experiment_run("a", "ag", trial_id="ok", battery_env=before,
                                  before_env=before, before_dir=bd,
                                  after_env=after, after_dir=adr, min_n=1)
        assert good["verdict"] == "improved"
        # only 3 failed, need 5 -> not improved
        strict = api.experiment_run("a", "ag", trial_id="strict", battery_env=before,
                                    before_env=before, before_dir=bd,
                                    after_env=after, after_dir=adr, min_n=5)
        assert strict["verdict"] != "improved"


# --- P0-5: precommitted manifest blocks cherry-picking ---------------------

def test_p0_5_precommitted_manifest_blocks_cherry_pick(tmp_path):
    scen, bdir, adir = (os.path.join(tmp_path, d) for d in ("scen", "before", "after"))
    for d in (scen, bdir, adir):
        os.makedirs(d)
    for i in (1, 2, 3):
        sid = f"f{i}"
        json.dump({"id": sid, "caller_onset_sec": 2.0,
                   "expected": {"yield": True, "max_time_to_yield_sec": 1.0,
                                "max_talk_over_sec": 1.0}},
                  open(os.path.join(scen, f"{sid}.json"), "w"))
        ta.talkover_call(os.path.join(bdir, f"{sid}.example.wav"))
        ta.yielding_call(os.path.join(adir, f"{sid}.example.wav"))
    battery = core.run_suite(scenarios_dir=scen, audio_dir=bdir, suffix=".example.wav")
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        created = api.experiment_create("a", "ag", trial_id="t1", battery_env=battery, min_n=1)
        assert len(created["fixtures"]) == 3
        # drop f1 from before + after; run against the COMMITTED 3-fixture manifest
        bd2, ad2, sc2 = (os.path.join(tmp_path, d) for d in ("b2", "a2", "s2"))
        for d in (bd2, ad2, sc2):
            os.makedirs(d)
        for i in (2, 3):
            shutil.copy(os.path.join(bdir, f"f{i}.example.wav"), os.path.join(bd2, f"f{i}.example.wav"))
            shutil.copy(os.path.join(adir, f"f{i}.example.wav"), os.path.join(ad2, f"f{i}.example.wav"))
            json.dump({"id": f"f{i}", "caller_onset_sec": 2.0,
                       "expected": {"yield": True, "max_time_to_yield_sec": 1.0,
                                    "max_talk_over_sec": 1.0}},
                      open(os.path.join(sc2, f"f{i}.json"), "w"))
        b2 = core.run_suite(scenarios_dir=sc2, audio_dir=bd2, suffix=".example.wav")
        a2 = core.run_suite(scenarios_dir=sc2, audio_dir=ad2, suffix=".example.wav")
        r = api.experiment_run("a", "ag", trial_id="t1",
                               manifest_ref=created["manifest_digest"],
                               before_env=b2, before_dir=bd2, after_env=a2, after_dir=ad2)
    assert r["verdict"] == "refused"
    assert r["refusal"]["kind"] == "incomplete_fixture_set"


# --- P0-6: opposite-risk guard ---------------------------------------------

def test_p0_6_no_hold_guard_is_disclosed(tmp_path):
    before, bdir, after, adir, man = _yield_trial(tmp_path)
    vec = dict(recompute.recompute_trial(before, bdir, after, adir, man)["evidence"]["vector"])
    assert vec["opposite_risk_guard"] == "none"
    # lift the trust dims the way fix_trial does, then the headline discloses it
    vec["input_health"] = "clean"; vec["channel_mapping"] = "confirmed"
    assert "NO HOLD GUARD" in ev.classify(vec)["headline"]


def test_p0_6_regressed_hold_guard_caps_tier_none():
    vec = {"score_integrity": "recomputed", "audio_identity": "recomputed",
           "policy_integrity": "signed", "fixture_set_integrity": "manifest_complete",
           "input_health": "clean", "channel_mapping": "confirmed", "label_authority": "human",
           "pairing_integrity": "contract_bound", "capture_origin": "runner_attested",
           "opposite_risk_guard": "regressed"}
    assert ev.classify(vec)["tier"] == ev.TIER_NONE


# --- P1-7 / P1-8: label integrity ------------------------------------------

def _multi_candidate_wav(path):
    rate, dur = 16000, 12
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        import math
        frames = []
        for i in range(rate * dur):
            t = i / rate
            c = 12000 * math.sin(2 * math.pi * 300 * t) if (3 < t < 4 or 7 < t < 8) else 0
            a = 12000 * math.sin(2 * math.pi * 200 * t) if t < 8 else 0
            frames.append(struct.pack("<hh", int(c), int(a)))
        w.writeframes(b"".join(frames))


def test_p1_7_label_ids_do_not_collide(tmp_path):
    wav = str(tmp_path / "multi.wav"); _multi_candidate_wav(wav)
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a"); api.agent_add("a", "ag", stack="vapi")
        ing = api.ingest_recording("a", "ag", wav)
        disc = api.discover("a", "ag", wav, recording_id=ing["recording_id"])
        cids = [c["candidate_id"] for c in disc["candidates"]]
        if len(cids) < 2:
            pytest.skip("scan produced <2 candidates on this synthetic input")
        l1 = api.label("a", cids[0], decision="yield", reviewer="alice")
        l2 = api.label("a", cids[1], decision="hold", reviewer="bob")
        assert l1["label_id"] != l2["label_id"]
        rows = api.registry._all("SELECT label_id FROM labels")
        assert len(rows) == 2


def test_p1_8_orphan_label_is_rejected(tmp_path):
    with FleetAPI(str(tmp_path / "home")) as api:
        api.init_workspace("a")
        with pytest.raises(ValueError):
            api.label("a", "does-not-exist", decision="yield", reviewer="alice")


# --- P1-9: start --stereo emits no verdict before a human label ------------

def test_p1_9_start_stereo_no_verdict_before_label(tmp_path):
    import io
    import contextlib
    from hotato.start import _run_stereo_flow
    wav = str(tmp_path / "h1.wav"); ta.talkover_call(wav, onset=5.0, total=12)
    out = str(tmp_path / "un"); os.makedirs(out)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _run_stereo_flow(wav, out_dir=out, fmt="json", label=None, onset_sec=None,
                         caller_channel=0, agent_channel=1)
    payload = json.loads(buf.getvalue())
    tc = payload.get("top_candidate") or {}
    assert "verdict" not in tc
    assert "needs_label" in tc
