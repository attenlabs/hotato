"""Regression tests for the GPT-review evidence-kernel KILL findings.

K2: the manifest signature verifier returns a dict {ok, signed, reason}; the
recompute classifier must test .ok, never the dict's truthiness (a {ok: False}
dict is still truthy, which used to upgrade a FORGED/ALTERED signature to the
strongest 'signed' tier).
K7: a present-but-failing signature must not silently read as clean; the tier
map must place it at the refuse floor, distinct from an honestly-unsigned or a
signature-we-could-not-check manifest.
"""
from hotato import manifest as _manifest
from hotato import evidence as _evidence


KEY = b"kernel-test-key"


def _signed_manifest():
    m = {"schema": "hotato.manifest.v1", "fixtures": [], "policy_hash": "abc",
         "battery": {"min_n": 1}}
    return _manifest.sign_manifest(dict(m), KEY)


def test_k2_valid_signature_verifies():
    m = _signed_manifest()
    r = _manifest.verify_manifest_signature(m, KEY)
    assert r["ok"] is True and r["signed"] is True


def test_k2_altered_body_after_signing_fails_ok_is_false():
    m = _signed_manifest()
    m["policy_hash"] = "tampered"  # edit the body after signing
    r = _manifest.verify_manifest_signature(m, KEY)
    assert r["ok"] is False and r["signed"] is True
    # the returned dict is still TRUTHY -- the exact trap the classifier hit
    assert bool(r) is True


def test_k2_wrong_key_fails_ok_is_false_but_dict_is_truthy():
    m = _signed_manifest()
    r = _manifest.verify_manifest_signature(m, b"the-wrong-key")
    assert r["ok"] is False
    assert bool(r) is True, "the failing result is a truthy dict: .ok must be tested, not the dict"


def test_k2_unsigned_manifest_reports_not_signed():
    m = {"schema": "hotato.manifest.v1", "fixtures": []}
    r = _manifest.verify_manifest_signature(m, KEY)
    assert r["ok"] is False and r["signed"] is False


def test_k2_k7_tier_map_places_signature_states_correctly():
    pol = _evidence._DIMENSIONS["policy_integrity"]
    # a verified signature is the strongest policy tier
    assert pol["signed"] == _evidence.TIER_ATTESTED
    # a claimed-but-failed signature is a refuse-floor authenticity failure,
    # strictly BELOW an honestly-unsigned manifest (K7)
    assert pol["signature_invalid"] == _evidence.TIER_NONE
    assert pol["signature_invalid"] < pol["unsigned"] < pol["manifest_pinned"]
    # a claimed-but-unverifiable (no key) signature holds at PAIRED (still pinned)
    # but is NOT upgraded to attested
    assert pol["signature_unverified"] == _evidence.TIER_PAIRED
    assert pol["signature_unverified"] < pol["signed"]


# --- K1 regression: the scorer pin must cover the real scorer bytes ----------

def test_k1_wheel_hash_covers_more_than_init_py():
    """The pin must not be just sha256(__init__.py); a change to _engine/score.py
    (the actual scorer) has to change it. Prove it is NOT the old init-only hash."""
    import hashlib as _h, os as _os, hotato as _pkg
    from hotato import manifest as _m
    init = _pkg.__file__
    init_only = _h.sha256(open(init, "rb").read()).hexdigest()
    wh = _m.wheel_hash()
    assert wh != "unverified"
    assert len(wh) == 64
    assert wh != init_only, "wheel_hash still only covers __init__.py (K1 not fixed)"
    # it must fold in the vendored engine source
    eng = _os.path.join(_os.path.dirname(init), "_engine")
    assert _os.path.isdir(eng), "sanity: _engine package present"


def test_k1_wheel_hash_is_deterministic():
    from hotato import manifest as _m
    assert _m.wheel_hash() == _m.wheel_hash()


def test_k1_scorer_changed_state_refuses():
    from hotato import evidence as _e
    assert _e._DIMENSIONS["score_integrity"]["scorer_changed"] == _e.TIER_NONE
