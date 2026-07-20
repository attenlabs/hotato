"""Deployment-identity binding for a RELEASE proof.

Closes the gap where a fresh scored before/after is not, on its own, proof that
the INTENDED agent revision was tested: a paired proof binds the after side to a
pinned manifest, but not to a candidate deployment. A release proof is strictly
stronger -- it additionally requires the candidate deployment identity to be
config-hash-bound.

These tests are hermetic (no network, no provider, no live telephony) and
deterministic. They assert:

* :func:`hotato.evidence.meets_release_proof` is True only for a full paired
  vector PLUS ``deployment_identity == config_hash_bound``;
* the SAME paired vector with the identity absent / operator_asserted / unknown
  still meets a PAIRED proof but NOT a release proof;
* the release-proof gate is a strict superset of the paired-proof gate and does
  NOT modify ``REQUIRED_FOR_PAIRED_PROOF`` or what a paired proof passes/fails;
* :func:`hotato.fix_trial.run_trial` threads caller-supplied identity fields into
  the pinned manifest body (and they are covered by ``manifest_hash``), while the
  default (all ``None``) reproduces the manifest byte-for-byte.
"""

from __future__ import annotations

import json

from hotato import apply as _apply
from hotato import evidence as _evidence
from hotato import fix_trial as _fix_trial
from hotato import manifest as _manifest
from hotato import verify as _verify
from tests.test_fix_trial import _config_plan, _write_patch, build_trial

# A fully-attested paired vector: every REQUIRED_FOR_PAIRED_PROOF dimension holds
# the tier at or above PAIRED, so meets_paired_proof is True on its own. Chosen to
# mix ATTESTED-capped and PAIRED-capped states so the paired floor is genuine.
_PAIRED_VECTOR = {
    "score_integrity": "recomputed",            # ATTESTED
    "audio_identity": "recomputed",             # ATTESTED
    "policy_integrity": "manifest_pinned",      # PAIRED
    "fixture_set_integrity": "manifest_complete",  # ATTESTED
    "input_health": "clean",                    # ATTESTED
    "channel_mapping": "confirmed",             # ATTESTED
    "label_authority": "asserted",              # PAIRED
    "pairing_integrity": "contract_bound",      # ATTESTED
    "capture_origin": "operator_asserted",      # PAIRED
    "opposite_risk_guard": "present_passing",   # ATTESTED
}

_IDENTITY_FIELDS = (
    "agent_id", "deployment_id", "source_config_hash", "candidate_config_hash",
)


def _vec(**overrides):
    v = dict(_PAIRED_VECTOR)
    v.update(overrides)
    return v


# --- (a) full paired + config_hash_bound -> release proof --------------------

def test_full_paired_plus_config_hash_bound_meets_release_proof():
    vec = _vec(deployment_identity="config_hash_bound")
    assert _evidence.meets_paired_proof(vec) is True
    assert _evidence.meets_release_proof(vec) is True
    # a release proof is at least a paired proof (the tier still reaches PAIRED)
    assert _evidence.evidence_tier(vec, _evidence.REQUIRED_FOR_PAIRED_PROOF) \
        >= _evidence.TIER_PAIRED


# --- (b) same paired vector, weaker/absent identity -> paired but NOT release --

def test_absent_identity_is_paired_but_not_release():
    # deployment_identity key entirely absent (a pre-identity caller)
    vec = dict(_PAIRED_VECTOR)
    assert "deployment_identity" not in vec
    assert _evidence.meets_paired_proof(vec) is True
    assert _evidence.meets_release_proof(vec) is False


def test_operator_asserted_identity_is_paired_but_not_release():
    vec = _vec(deployment_identity="operator_asserted")
    assert _evidence.meets_paired_proof(vec) is True
    assert _evidence.meets_release_proof(vec) is False


def test_unknown_or_none_identity_is_paired_but_not_release():
    for state in ("unknown", None, "some-unrecognized-token"):
        vec = _vec(deployment_identity=state)
        assert _evidence.meets_paired_proof(vec) is True, state
        assert _evidence.meets_release_proof(vec) is False, state


# --- release proof is STRICTLY STRONGER: paired must hold first --------------

def test_release_proof_still_requires_a_paired_proof():
    # config-hash-bound identity cannot rescue a vector that fails the paired
    # gate (a tampered score is a refuse-floor authenticity failure).
    broken = _vec(score_integrity="mismatch",
                  deployment_identity="config_hash_bound")
    assert _evidence.meets_paired_proof(broken) is False
    assert _evidence.meets_release_proof(broken) is False


# --- the paired-proof contract is untouched (additive) -----------------------

def test_required_for_paired_proof_is_unchanged_and_release_is_a_superset():
    assert _evidence.REQUIRED_FOR_PAIRED_PROOF == (
        "score_integrity",
        "audio_identity",
        "policy_integrity",
        "fixture_set_integrity",
        "input_health",
        "channel_mapping",
        "label_authority",
        "pairing_integrity",
        "capture_origin",
        "opposite_risk_guard",
    )
    assert "deployment_identity" not in _evidence.REQUIRED_FOR_PAIRED_PROOF
    # the release set is exactly the paired set plus the one new dimension
    assert _evidence.REQUIRED_FOR_RELEASE_PROOF == \
        _evidence.REQUIRED_FOR_PAIRED_PROOF + ("deployment_identity",)
    # a strict superset, in order, adding nothing else
    assert _evidence.REQUIRED_FOR_RELEASE_PROOF[:-1] == \
        _evidence.REQUIRED_FOR_PAIRED_PROOF
    assert set(_evidence.REQUIRED_FOR_RELEASE_PROOF) - \
        set(_evidence.REQUIRED_FOR_PAIRED_PROOF) == {"deployment_identity"}


# --- (c) run_trial threads identity; defaults reproduce the manifest ---------

def _spy_build_manifest(monkeypatch):
    """Record every (env, kwargs, manifest) triple build_manifest is called with
    inside run_trial, without changing what it returns."""
    calls = []
    orig = _manifest.build_manifest

    def spy(env, **kw):
        man = orig(env, **kw)
        calls.append({"env": env, "kw": kw, "man": man})
        return man

    monkeypatch.setattr(_fix_trial._manifest, "build_manifest", spy)
    return calls, orig


def test_run_trial_threads_identity_into_manifest_and_defaults_are_byte_identical(
        tmp_path, monkeypatch):
    patch_path = _write_patch(tmp_path, _config_plan())
    with open(patch_path, encoding="utf-8") as fh:
        patch = json.load(fh)
    plan = _apply.load_referenced_plan(patch, str(patch_path))
    before, after, _battery = build_trial(tmp_path)

    calls, orig = _spy_build_manifest(monkeypatch)

    # 1) DEFAULTS: no identity supplied -> the manifest carries None for each
    # identity field (behaviour is exactly as before this feature).
    t_default = _fix_trial.run_trial(
        patch, name="rel-x", before=before, after=after,
        patch_source=str(patch_path), plan=plan,
    )
    default_call = calls[-1]
    man_default = default_call["man"]
    for field in _IDENTITY_FIELDS:
        assert man_default[field] is None
    assert t_default["recompute"]["manifest_hash"] == man_default["manifest_hash"]

    # 1b) BYTE-IDENTICAL: rebuild the same manifest from the SAME battery env and
    # the SAME kwargs but with the identity keys stripped entirely (the exact
    # pre-change call shape). The bodies must be identical -- proving the added
    # keyword params with None defaults preserve current behaviour exactly.
    pre_change_kw = {k: v for k, v in default_call["kw"].items()
                     if k not in _IDENTITY_FIELDS}
    pre_change_man = orig(default_call["env"], **pre_change_kw)
    assert pre_change_man == man_default
    assert _manifest.canonical_json(pre_change_man) == \
        _manifest.canonical_json(man_default)

    # 2) PROVIDED: identity fields are threaded straight into the manifest body
    # and are covered by manifest_hash (the pin changes when identity changes).
    t_id = _fix_trial.run_trial(
        patch, name="rel-x", before=before, after=after,
        patch_source=str(patch_path), plan=plan,
        agent_id="asst_9", deployment_id="dep_42",
        source_config_hash="src_hash_aaaa", candidate_config_hash="cand_hash_bbbb",
    )
    man_id = calls[-1]["man"]
    assert man_id["agent_id"] == "asst_9"
    assert man_id["deployment_id"] == "dep_42"
    assert man_id["source_config_hash"] == "src_hash_aaaa"
    assert man_id["candidate_config_hash"] == "cand_hash_bbbb"
    # the identity is inside the hashed body, and the hash still verifies
    assert _manifest.verify_manifest_hash(man_id)
    assert t_id["recompute"]["manifest_hash"] == man_id["manifest_hash"]
    # supplying identity changes the pin vs the default (None) build
    assert man_id["manifest_hash"] != man_default["manifest_hash"]


def test_build_manifest_identity_defaults_preserve_the_body(tmp_path):
    """Manifest-level guard: build_manifest called with the identity kwargs
    explicitly None is byte-identical to a call that omits them, and supplying
    real identity changes the hashed body. Deterministic (fixed nonce)."""
    from tests.test_manifest_fields import _battery

    env = _battery(tmp_path)
    common = dict(trial_id="t", nonce="fixed-nonce",
                  policy={"max_talk_over_sec": 1.0}, min_n=1)

    omitted = _manifest.build_manifest(env, **common)
    explicit_none = _manifest.build_manifest(
        env, agent_id=None, deployment_id=None,
        source_config_hash=None, candidate_config_hash=None, **common)
    assert omitted == explicit_none
    assert omitted["manifest_hash"] == explicit_none["manifest_hash"]

    with_identity = _manifest.build_manifest(
        env, agent_id="asst_9", deployment_id="dep_42",
        source_config_hash="src", candidate_config_hash="cand", **common)
    assert with_identity["agent_id"] == "asst_9"
    assert with_identity["candidate_config_hash"] == "cand"
    assert _manifest.verify_manifest_hash(with_identity)
    assert with_identity["manifest_hash"] != omitted["manifest_hash"]
