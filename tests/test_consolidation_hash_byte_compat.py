"""Byte-compatibility proof for the canonical-JSON / sha256 consolidation.

Finding #2 replaced four hand-rolled copies of the
``sha256(json.dumps(x, sort_keys=True, separators=(",", ":")))`` idiom
(rubric.py, fleet/canary.py x2, fleet/privacy.py) and ``fleet/store.put_json``'s
inline compact-dump with the shared ``manifest.canonical_json`` /
``manifest._sha256_str`` primitives. Those digests are content addresses and
signature subjects: if a single output byte moved, stored blobs would no longer
resolve and receipts would stop verifying. This test freezes the PRE-CHANGE hex
for a deliberately nasty payload (non-ascii unicode, deep nesting, floats incl.
negative-zero and integer-valued floats, control chars, empty containers,
unsorted keys) and proves:

  1. the frozen hex is exactly what the OLD literal expression produced,
  2. the shared manifest helper reproduces it byte-for-byte, and
  3. every consolidated call site (store.put_json, the two canary receipts, the
     privacy deletion receipt, rubric's local helpers) still emits that digest.
"""
from __future__ import annotations

import hashlib
import json

from hotato import manifest, rubric
from hotato.fleet import canary, privacy, store

# The nasty payload and the two frozen digests, computed offline from the EXACT
# pre-consolidation expressions. Do not "fix" these by regenerating them from the
# new code -- their whole point is to be an external, pre-change ground truth.
NASTY = {
    "z_last": 1,
    "a_first": True,
    "unicode": "café 日本語 \U0001f954 naïve",
    "nested": {
        "b": [3.14159, -0.0, 2.0, {"deep": None, "x": "\t\n quote\" backslash\\"}],
        "a": {"k": 1e-10, "big": 1234567890.123},
    },
    "list": [1, "two", False, None, {"m": "n"}],
    "empty": {},
    "float_int": 5.0,
}
# sha256 over json.dumps(NASTY, sort_keys=True, separators=(",", ":")) [ensure_ascii default True]
PRE_CHANGE_FLEET_RUBRIC_HEX = (
    "d2f554dbd7aec8cfce16c895946115e36f956c0aea4f6457a1089c3f48588d5e"
)
# sha256 over the same, WITH the trailing "\n" that store.put_json appends
PRE_CHANGE_STORE_HEX = (
    "19e1770c2e5f60e7205fe671f8ef214e212324e4de5780b5ab8523cdd7124a44"
)


def _old_fleet_rubric_digest(body) -> str:
    """The exact expression fleet/canary.py, fleet/privacy.py, and
    rubric._sha256_json computed BEFORE the consolidation."""
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _old_store_digest(obj) -> str:
    """The exact expression fleet/store.put_json computed BEFORE the
    consolidation (note the trailing newline appended before hashing)."""
    return hashlib.sha256(
        (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    ).hexdigest()


def test_frozen_hex_equals_old_expression():
    # the frozen constants ARE what the pre-change code produced
    assert _old_fleet_rubric_digest(NASTY) == PRE_CHANGE_FLEET_RUBRIC_HEX
    assert _old_store_digest(NASTY) == PRE_CHANGE_STORE_HEX


def test_shared_manifest_helper_is_byte_identical():
    new = manifest._sha256_str(manifest.canonical_json(NASTY))
    assert new == PRE_CHANGE_FLEET_RUBRIC_HEX
    assert new == _old_fleet_rubric_digest(NASTY)


def test_rubric_local_helpers_unchanged():
    assert rubric._sha256_json(NASTY) == PRE_CHANGE_FLEET_RUBRIC_HEX
    assert rubric._canonical(NASTY) == json.dumps(
        NASTY, sort_keys=True, separators=(",", ":")
    )
    txt = "café \U0001f954"
    assert rubric._sha256_text(txt) == hashlib.sha256(txt.encode("utf-8")).hexdigest()


def test_store_put_json_digest_unchanged(tmp_path):
    st = store.ArtifactStore(str(tmp_path / "cas"))
    digest = st.put_json(NASTY)
    assert digest == PRE_CHANGE_STORE_HEX
    assert digest == _old_store_digest(NASTY)
    # content addressing still holds: the stored bytes re-hash to this digest
    assert st.verify(digest)


def test_canary_receipt_digests_unchanged():
    r = canary.deployment_receipt(
        "clone", agent_id="a", variant_id="v", config_hash="c",
        prior_revision=3, detail={"unicode": "café", "n": 1e-10},
    )
    body = {k: v for k, v in r.items() if k != "receipt_digest"}
    assert r["receipt_digest"] == _old_fleet_rubric_digest(body)

    class _Adapter:
        def rollback(self, ref, revision):
            return {"ok": True, "ref": ref, "rev": revision}

    rb = canary.rollback(
        _Adapter(), ref="agent:1", revision=2, reason="regress",
        actor="op", at=123.0,
    )
    body2 = {k: v for k, v in rb.items() if k != "receipt_digest"}
    assert rb["receipt_digest"] == _old_fleet_rubric_digest(body2)


def test_privacy_deletion_receipt_digest_unchanged():
    r = privacy.deletion_receipt(
        subject_id="s", subject_kind="recording", pcm_sha256="deadbeef",
        reason="expired", actor="op", at=99.0, legal_hold=False,
    )
    body = {k: v for k, v in r.items() if k != "receipt_digest"}
    assert r["receipt_digest"] == _old_fleet_rubric_digest(body)
