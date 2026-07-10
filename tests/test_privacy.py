"""Fleet privacy: conservative sharing defaults, retention, audited deletion,
redaction-as-derived-artifact."""
import os

from hotato.fleet import privacy
from tests import _trial_audio as ta


def test_sharing_defaults_are_conservative():
    pol = privacy.retention_policy(consent_basis="contract", allowed_purposes=["qa"],
                                   retention_days=30)
    assert not privacy.enforce_share(pol, "export")["allowed"]
    assert not privacy.enforce_share(pol, "playback")["allowed"]
    assert not privacy.enforce_share(pol, "transcript")["allowed"]
    assert not privacy.enforce_share(pol, "hosted_egress")["allowed"]
    assert not privacy.enforce_share(pol, "public")["allowed"]


def test_retention_expiry_and_legal_hold():
    pol = privacy.retention_policy(consent_basis="c", allowed_purposes=[], retention_days=30)
    assert not privacy.is_expired(pol, captured_at=0, now=10 * 86400)
    assert privacy.is_expired(pol, captured_at=0, now=40 * 86400)
    held = privacy.retention_policy(consent_basis="c", allowed_purposes=[],
                                    retention_days=1, legal_hold=True)
    assert not privacy.is_expired(held, captured_at=0, now=999 * 86400)


def test_deletion_receipt_is_hashable_and_stable():
    r1 = privacy.deletion_receipt(subject_id="rec-1", subject_kind="recording",
                                  pcm_sha256="ab" * 32, reason="retention", actor="sys", at=1.0)
    r2 = privacy.deletion_receipt(subject_id="rec-1", subject_kind="recording",
                                  pcm_sha256="ab" * 32, reason="retention", actor="sys", at=1.0)
    assert r1["receipt_digest"] == r2["receipt_digest"] and len(r1["receipt_digest"]) == 64


def test_redaction_is_a_derived_artifact(tmp_path):
    src = str(tmp_path / "s.wav"); out = str(tmp_path / "r.wav")
    ta.yielding_call(src)
    der = privacy.redact_audio(src, [(2.0, 3.0)], out)
    assert der["derived"] and der["kind"] == "redacted-audio"
    assert der["pcm_sha256"] != der["parent_pcm_sha256"]     # new identity
    assert "must not be scored as evidence of the original" in der["evidence_statement"]
    assert os.path.isfile(src)   # original untouched
