"""§6.3 trial-manifest fields: wheel hash, required yield targets / hold guards,
capture plan, permitted transformations, optional adapter identity, and optional
HMAC signing.

The fields are additive and covered by ``manifest_hash`` (so ``verify_manifest_hash``
still passes), deterministic functions of the battery (so the fix-trial
reproducible-nonce path is unaffected), and signing is a separate optional call
that leaves an unsigned build unchanged.
"""
import copy
import json
import os
import tempfile

from hotato import core, manifest as m
from tests import _trial_audio as ta


def _battery(tmp_path):
    """A real 3-yield + 1-hold battery scored through core.run_suite (the same
    envelope shape build_manifest consumes at trial time). Each call uses a fresh
    subdir so a test may build several batteries under one tmp_path."""
    root = tempfile.mkdtemp(dir=str(tmp_path))
    scen = os.path.join(root, "scen"); adir = os.path.join(root, "audio")
    os.mkdir(scen); os.mkdir(adir)
    scen = type(tmp_path)(scen); adir = type(tmp_path)(adir)
    fixtures = [("f1", True), ("f2", True), ("f3", True), ("h1", False)]
    for sid, ey in fixtures:
        (scen / f"{sid}.json").write_text(json.dumps(
            {"id": sid, "caller_onset_sec": 2.0, "expected": {"yield": ey}}))
        if ey:
            ta.yielding_call(str(adir / f"{sid}.example.wav"))
        else:
            ta.holding_call(str(adir / f"{sid}.example.wav"))
    return core.run_suite(scenarios_dir=str(scen), audio_dir=str(adir),
                          suffix=".example.wav")


def _build(tmp_path, **kw):
    return m.build_manifest(_battery(tmp_path), trial_id="t", nonce="n",
                            policy={"max_talk_over_sec": 1.0}, min_n=1, **kw)


# --- new §6.3 fields ---------------------------------------------------------

def test_manifest_carries_wheel_hash(tmp_path):
    man = _build(tmp_path)
    wh = man["scorer"]["wheel_hash"]
    assert wh == "unverified" or len(wh) == 64  # sha256 hex, or documented literal
    assert wh == m.wheel_hash()  # deterministic, network-free


def test_required_yield_targets_and_hold_guards_derive_from_battery(tmp_path):
    man = _build(tmp_path)
    targets = set(man["required_yield_targets"])
    guards = set(man["required_hold_guards"])
    keys = {f["fixture_id"]: f for f in man["fixtures"]}
    # every yield fixture is a required target; every hold fixture a required guard
    assert targets == {k for k, f in keys.items() if f["expected_yield"]}
    assert guards == {k for k, f in keys.items() if not f["expected_yield"]}
    assert targets and guards            # this battery has both
    assert targets.isdisjoint(guards)    # a fixture is never both


def test_capture_plan_binds_per_fixture_stimulus(tmp_path):
    man = _build(tmp_path)
    cp = man["capture_plan"]
    assert set(cp["per_fixture"]) == {f["fixture_id"] for f in man["fixtures"]}
    for f in man["fixtures"]:
        assert cp["per_fixture"][f["fixture_id"]] == f["stimulus_pcm_sha256"]
    # combined hash is a deterministic function of the per-fixture map
    assert len(cp["scenario_stimulus_hash"]) == 64
    assert cp["scenario_stimulus_hash"] == \
        m._sha256_str(m.canonical_json(cp["per_fixture"]))


def test_permitted_transformations_is_a_real_list_field(tmp_path):
    assert _build(tmp_path)["permitted_transformations"] == []
    man = _build(tmp_path, permitted_transformations=["codec:g711"])
    assert man["permitted_transformations"] == ["codec:g711"]


def test_adapter_included_only_when_provided(tmp_path):
    assert "adapter" not in _build(tmp_path)
    man = _build(tmp_path, adapter_name="mock", adapter_version="1")
    assert man["adapter"] == {"name": "mock", "version": "1"}


# --- additive fields stay inside manifest_hash + reproducible ----------------

def test_new_fields_are_covered_by_manifest_hash(tmp_path):
    man = _build(tmp_path)
    assert m.verify_manifest_hash(man)
    for path in (["required_yield_targets"], ["capture_plan", "scenario_stimulus_hash"],
                 ["scorer", "wheel_hash"]):
        bad = copy.deepcopy(man)
        node = bad
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = "tampered"
        assert not m.verify_manifest_hash(bad)


def test_deterministic_nonce_path_is_reproducible(tmp_path):
    # Same battery + inputs -> identical manifest_hash (the fix_trial reproducible
    # path relies on this; the new fields must not introduce nondeterminism).
    env = _battery(tmp_path)
    a = m.build_manifest(env, trial_id="t", nonce="n", policy={"max_talk_over_sec": 1.0}, min_n=1)
    b = m.build_manifest(env, trial_id="t", nonce="n", policy={"max_talk_over_sec": 1.0}, min_n=1)
    assert a["manifest_hash"] == b["manifest_hash"]


# --- optional HMAC signing ---------------------------------------------------

def test_sign_and_verify_roundtrip(tmp_path):
    man = _build(tmp_path)
    assert "signature" not in man          # build stays unsigned by default
    key = b"trial-signing-key"
    signed = m.sign_manifest(man, key)
    assert signed["signature"]["algorithm"] == "hmac-sha256"
    res = m.verify_manifest_signature(signed, key)
    assert res["ok"] is True and res["signed"] is True
    # signing does not disturb the manifest_hash (that hash excludes signature)
    assert m.verify_manifest_hash(signed)


def test_tampered_signed_body_fails_verification(tmp_path):
    key = b"trial-signing-key"
    signed = m.sign_manifest(_build(tmp_path), key)
    tampered = copy.deepcopy(signed)
    tampered["policy"]["max_talk_over_sec"] = 99.0   # edit the body post-signing
    res = m.verify_manifest_signature(tampered, key)
    assert res["ok"] is False
    # a wrong key also fails
    assert m.verify_manifest_signature(signed, b"wrong-key")["ok"] is False
    # unsigned manifest reports unsigned, not verified
    unsigned = m.verify_manifest_signature(_build(tmp_path), key)
    assert unsigned["ok"] is False and unsigned["signed"] is False
