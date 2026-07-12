"""Evidence Kernel v2, K5: the signed label-record that REPLACES the
expected_yield-presence inference for human label authority.

Covers hotato.labelrecord directly (mint/verify, both signing tiers, tamper /
wrong-key / unbound-audio refusal) and its integration into
manifest.build_manifest's label_authority derivation: human (verified
Ed25519, via a real local trust registry) / human-shared (verified HMAC) /
asserted (an explicit expectation, no record) / none (neither) / invalid (a
supplied record that does not verify -- refused, never silently downgraded).
"""
import copy
import json
import os

import pytest

from hotato import core, evidence as ev, labelrecord as lr, manifest as m, sign
from tests import _labels
from tests import _trial_audio as ta


def _battery(tmp_path, *, expected=True):
    """A single scorable should-yield fixture, scored through core.run_suite
    (the real event shape build_manifest and labelrecord consume)."""
    root = tmp_path / "battery"
    scen = root / "scen"; adir = root / "audio"
    scen.mkdir(parents=True); adir.mkdir(parents=True)
    sc = {"id": "f1", "caller_onset_sec": 2.0}
    if expected:
        sc["expected"] = {"yield": True, "max_time_to_yield_sec": 1.0,
                          "max_talk_over_sec": 1.0}
    (scen / "f1.json").write_text(json.dumps(sc))
    ta.yielding_call(str(adir / "f1.example.wav"))
    return core.run_suite(scenarios_dir=str(scen), audio_dir=str(adir),
                          suffix=".example.wav")


# --- labelrecord.py: mint / verify, both tiers ------------------------------

def test_mint_and_verify_ed25519_roundtrip_is_human():
    pytest.importorskip("cryptography")  # Ed25519 path needs the [sign] extra
    priv, pub, key_id = sign.keygen()
    record = lr.mint_label_record(
        reviewer_principal="alice", event_audio_pcm_sha256="a" * 64,
        decision="yield", private_key=priv, key_id=key_id)
    assert record["schema"] == lr.SCHEMA
    assert record["signer"] == {"key_id": key_id, "algo": "ed25519"}
    res = lr.verify_label_record(record, pubkey_or_key=pub, event_pcm_sha256="a" * 64)
    assert res == {"ok": True, "authority": "human",
                   "reason": "Ed25519-signed label-record verified"}


def test_mint_and_verify_hmac_roundtrip_is_human_shared():
    key = b"a-shared-secret"
    record = lr.mint_label_record(
        reviewer_principal="bob", event_audio_pcm_sha256="b" * 64,
        decision="hold", hmac_key=key)
    assert record["signer"]["algo"] == "hmac"
    res = lr.verify_label_record(record, pubkey_or_key=key, event_pcm_sha256="b" * 64)
    assert res["ok"] is True and res["authority"] == "human-shared"


def test_tampered_body_fails_both_tiers():
    pytest.importorskip("cryptography")  # Ed25519 path needs the [sign] extra
    priv, pub, key_id = sign.keygen()
    record = lr.mint_label_record(
        reviewer_principal="alice", event_audio_pcm_sha256="a" * 64,
        decision="yield", private_key=priv, key_id=key_id)
    tampered = copy.deepcopy(record)
    tampered["decision"] = "hold"          # edit the body post-signing
    res = lr.verify_label_record(tampered, pubkey_or_key=pub, event_pcm_sha256="a" * 64)
    assert res["ok"] is False and res["authority"] is None

    key = b"shared"
    hrecord = lr.mint_label_record(
        reviewer_principal="bob", event_audio_pcm_sha256="b" * 64,
        decision="hold", hmac_key=key)
    htampered = copy.deepcopy(hrecord)
    htampered["reviewer_principal"] = "mallory"
    hres = lr.verify_label_record(htampered, pubkey_or_key=key, event_pcm_sha256="b" * 64)
    assert hres["ok"] is False and hres["authority"] is None


def test_wrong_key_is_refused():
    pytest.importorskip("cryptography")  # Ed25519 path needs the [sign] extra
    priv, pub, key_id = sign.keygen()
    _priv2, pub2, _kid2 = sign.keygen()
    record = lr.mint_label_record(
        reviewer_principal="alice", event_audio_pcm_sha256="a" * 64,
        decision="yield", private_key=priv, key_id=key_id)
    res = lr.verify_label_record(record, pubkey_or_key=pub2, event_pcm_sha256="a" * 64)
    assert res["ok"] is False and res["authority"] is None

    key = b"shared-key-1"
    hrecord = lr.mint_label_record(
        reviewer_principal="bob", event_audio_pcm_sha256="b" * 64,
        decision="hold", hmac_key=key)
    hres = lr.verify_label_record(hrecord, pubkey_or_key=b"shared-key-2",
                                  event_pcm_sha256="b" * 64)
    assert hres["ok"] is False and hres["authority"] is None


def test_unbound_audio_is_refused():
    pytest.importorskip("cryptography")  # Ed25519 path needs the [sign] extra
    """A label-record genuinely signed for one recording must not silently
    authenticate a DIFFERENT recording's fixture."""
    priv, pub, key_id = sign.keygen()
    record = lr.mint_label_record(
        reviewer_principal="alice", event_audio_pcm_sha256="a" * 64,
        decision="yield", private_key=priv, key_id=key_id)
    res = lr.verify_label_record(record, pubkey_or_key=pub, event_pcm_sha256="c" * 64)
    assert res["ok"] is False and res["authority"] is None
    assert "pcm" in res["reason"].lower()


def test_bad_decision_is_rejected():
    with pytest.raises(ValueError):
        lr.mint_label_record(reviewer_principal="alice",
                             event_audio_pcm_sha256="a" * 64,
                             decision="maybe", hmac_key=b"k")


def test_minting_without_any_key_refuses_not_silently_unsigned(monkeypatch, tmp_path):
    monkeypatch.delenv("HOTATO_ATTEST_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))     # no ~/.hotato/attest.key, no saved Ed25519 key
    with pytest.raises(lr.NoSigningKeyConfigured):
        lr.mint_label_record(reviewer_principal="alice",
                             event_audio_pcm_sha256="a" * 64, decision="yield")


# --- manifest.build_manifest integration ------------------------------------

def test_no_expectation_and_no_record_is_none(tmp_path):
    env = _battery(tmp_path, expected=False)
    man = m.build_manifest(env, trial_id="t", nonce="n", min_n=1)
    assert man["fixtures"][0]["label_authority"] == "none"
    assert man["fixtures"][0]["label_record"] is None


def test_explicit_expectation_no_record_is_asserted(tmp_path):
    env = _battery(tmp_path, expected=True)
    man = m.build_manifest(env, trial_id="t", nonce="n", min_n=1)
    assert man["fixtures"][0]["label_authority"] == "asserted"


def test_verified_hmac_label_record_is_human_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "the-shared-attest-key")
    env = _battery(tmp_path, expected=True)
    _labels.sign_event_human_shared(env["events"][0], key=b"the-shared-attest-key")
    man = m.build_manifest(env, trial_id="t", nonce="n", min_n=1)
    fx = man["fixtures"][0]
    assert fx["label_authority"] == "human-shared"
    assert fx["label_record"]["signer"]["algo"] == "hmac"


def test_verified_ed25519_label_record_via_local_trust_is_human(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")  # Ed25519 path needs the [sign] extra
    """The real end-to-end path: a key saved to the local trust registry
    (mirrors what `hotato fixture create` / a keygen step would leave behind)
    lets build_manifest's own trust resolution -- not a caller-supplied key --
    verify the signature and grant "human"/TIER_ATTESTED."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    priv, pub, key_id = sign.keygen()
    sign.save_signing_key(key_id, priv)
    sign.save_trust(key_id, pub)

    env = _battery(tmp_path, expected=True)
    event = env["events"][0]
    pcm = m._stimulus_pcm(event)
    record = lr.mint_label_record(
        reviewer_principal="alice", event_audio_pcm_sha256=pcm, decision="yield")
    assert record["signer"]["algo"] == "ed25519"
    event["label_record"] = record

    man = m.build_manifest(env, trial_id="t", nonce="n", min_n=1)
    fx = man["fixtures"][0]
    assert fx["label_authority"] == "human"
    vector = {"label_authority": fx["label_authority"]}
    assert ev._cap_for("label_authority", vector["label_authority"]) == ev.TIER_ATTESTED


def test_tampered_label_record_refuses_not_downgrades(tmp_path):
    pytest.importorskip("cryptography")  # Ed25519 path needs the [sign] extra
    """A label-record that WAS supplied but fails verification caps at
    TIER_NONE (refused) -- strictly worse than an honestly-absent record
    ("none"/TIER_ASSERTED), never a silent slide to "asserted"."""
    env = _battery(tmp_path, expected=True)
    event = env["events"][0]
    _labels.sign_event_human(event)
    event["label_record"]["event_audio_pcm_sha256"] = "0" * 64  # unbind it

    man = m.build_manifest(env, trial_id="t", nonce="n", min_n=1)
    fx = man["fixtures"][0]
    assert fx["label_authority"] == "invalid"
    assert ev._cap_for("label_authority", fx["label_authority"]) == ev.TIER_NONE
    assert ev._cap_for("label_authority", fx["label_authority"]) < \
        ev._cap_for("label_authority", "none")


# --- `hotato fixture create` mints and embeds a label-record ----------------

def test_fixture_create_mints_label_record_when_a_key_is_configured(tmp_path, monkeypatch):
    from hotato import fixture as _fixture
    from importlib import resources

    monkeypatch.setenv("HOTATO_ATTEST_KEY", "fixture-create-key")
    src = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    out = str(tmp_path / "out")
    result = _fixture.create_fixture(
        stereo=src, fixture_id="fx-labeled-001", onset_sec=2.40,
        expect="yield", out_dir=out, reviewer_principal="qa-alice")

    assert result["scenario"]["label_record"]["signer"]["algo"] == "hmac"
    assert result["scenario"]["label_record"]["reviewer_principal"] == "qa-alice"
    assert result["scenario"]["label_record"]["decision"] == "yield"
    label_path = os.path.join(out, "labels", "fx-labeled-001.label.json")
    assert os.path.exists(label_path)
    with open(label_path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk == result["scenario"]["label_record"]

    # and it reaches "human-shared" once run through the real suite + manifest
    env = core.run_suite(scenarios_dir=os.path.join(out, "scenarios"),
                         audio_dir=os.path.join(out, "audio"))
    man = m.build_manifest(env, trial_id="t", nonce="n", min_n=1)
    assert man["fixtures"][0]["label_authority"] == "human-shared"


def test_fixture_create_without_any_key_degrades_to_asserted_not_crash(tmp_path, monkeypatch):
    from hotato import fixture as _fixture
    from importlib import resources

    monkeypatch.delenv("HOTATO_ATTEST_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    src = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    out = str(tmp_path / "out")
    result = _fixture.create_fixture(
        stereo=src, fixture_id="fx-unlabeled-001", onset_sec=2.40,
        expect="yield", out_dir=out)

    assert "label_record" not in result["scenario"]
    assert not os.path.exists(os.path.join(out, "labels", "fx-unlabeled-001.label.json"))
    env = core.run_suite(scenarios_dir=os.path.join(out, "scenarios"),
                         audio_dir=os.path.join(out, "audio"))
    man = m.build_manifest(env, trial_id="t", nonce="n", min_n=1)
    assert man["fixtures"][0]["label_authority"] == "asserted"
