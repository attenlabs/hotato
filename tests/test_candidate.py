"""``hotato candidate``: the candidate-identity binding -- the hermetic half of
the candidate-bound proof path.

Pinned here (all hermetic, NO network -- the one network function is
monkeypatched out):

  * canonicalize strips the volatile identity noise (id/orgId/createdAt/
    updatedAt/isServerUrlSecretSet), including NESTED, and keeps every semantic
    field (model/voice/startSpeakingPlan/...);
  * config_hash is deterministic: two fetches of the same logical config (with
    the volatile fields differing) hash identically, and a real semantic change
    (a moved stopSpeakingPlan threshold) moves the hash -- drift detection fires
    on real changes and not on noise;
  * `candidate hash --config FILE` prints the expected hash with no network;
  * `candidate verify` is the refuse-on-drift gate: unchanged -> exit 0,
    semantically changed -> exit 1, unknown provider / missing key -> exit 2;
  * the hash `candidate hash` emits, fed to `prove --candidate-config-hash`,
    yields claim_scope candidate_revision;
  * share-safety: the saved/canonical config and the printed output carry no
    API key and no absolute path.
"""

from __future__ import annotations

import json

import pytest

from hotato import candidate as _candidate
from hotato import cli

# --- a fixture Vapi assistant config (the shape `candidate hash` fetches) ---
# Two logically-identical fetches differ ONLY in volatile fields; a third is a
# genuine semantic change (a moved stopSpeakingPlan interruption threshold).

def _assistant_config(*, updated_at="2026-07-20T10:00:00.000Z",
                      num_words=0, server_secret_set=False):
    return {
        "id": "asst_" + "0" * 24,
        "orgId": "org_" + "1" * 24,
        "createdAt": "2026-07-01T09:00:00.000Z",
        "updatedAt": updated_at,
        "isServerUrlSecretSet": server_secret_set,
        "name": "support-v3",
        "firstMessage": "Hi, how can I help?",
        "model": {
            "provider": "openai",
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a support agent."},
            ],
            # A nested volatile key: must be stripped at depth too.
            "updatedAt": "2026-07-19T00:00:00.000Z",
        },
        "voice": {"provider": "11labs", "voiceId": "burt"},
        "transcriber": {"provider": "deepgram", "model": "nova-2"},
        "startSpeakingPlan": {"waitSeconds": 0.4},
        "stopSpeakingPlan": {"numWords": num_words, "voiceSeconds": 0.2},
    }


# =========================================================================
# 1. canonicalize: strips volatile (incl. nested), keeps semantic
# =========================================================================

def test_canonicalize_strips_volatile_including_nested_and_keeps_semantic():
    canon = _candidate.canonicalize_config(_assistant_config())
    # volatile identity noise gone at the top level ...
    for key in ("id", "orgId", "createdAt", "updatedAt", "isServerUrlSecretSet"):
        assert key not in canon
    # ... and nested (the updatedAt inside model) gone too
    assert "updatedAt" not in canon["model"]
    # every semantic field kept
    assert canon["model"]["model"] == "gpt-4o"
    assert canon["model"]["messages"][0]["content"] == "You are a support agent."
    assert canon["voice"] == {"provider": "11labs", "voiceId": "burt"}
    assert canon["transcriber"]["model"] == "nova-2"
    assert canon["startSpeakingPlan"] == {"waitSeconds": 0.4}
    assert canon["stopSpeakingPlan"] == {"numWords": 0, "voiceSeconds": 0.2}
    assert canon["firstMessage"] == "Hi, how can I help?"


def test_canonicalize_does_not_mutate_the_input():
    original = _assistant_config()
    _candidate.canonicalize_config(original)
    assert "id" in original and "updatedAt" in original["model"]


def test_canonicalize_rejects_a_non_object():
    with pytest.raises(ValueError):
        _candidate.canonicalize_config(["not", "an", "object"])


# =========================================================================
# 2. config_hash: deterministic on noise, moves on a real semantic change
# =========================================================================

def test_config_hash_is_stable_across_volatile_only_differences():
    a = _candidate.config_hash(
        _assistant_config(updated_at="2026-07-20T10:00:00.000Z",
                          server_secret_set=False))
    b = _candidate.config_hash(
        _assistant_config(updated_at="2026-07-22T23:59:59.000Z",
                          server_secret_set=True))
    assert a == b
    assert a.startswith("sha256:")


def test_config_hash_moves_on_a_semantic_change():
    base = _candidate.config_hash(_assistant_config(num_words=0))
    changed = _candidate.config_hash(_assistant_config(num_words=3))
    assert base != changed


def test_config_hash_is_reproducible_byte_for_byte():
    cfg = _assistant_config()
    assert _candidate.config_hash(cfg) == _candidate.config_hash(cfg)


# =========================================================================
# 3. `candidate hash --config FILE`: the pure path, no network
# =========================================================================

def _write_config(tmp_path, cfg, name="candidate-config.json"):
    path = tmp_path / name
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_candidate_hash_config_file_prints_the_expected_hash(tmp_path, capsys):
    cfg = _assistant_config()
    path = _write_config(tmp_path, cfg)
    expected = _candidate.config_hash(cfg)

    rc = cli.main(["candidate", "hash", "--config", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert expected in out


def test_candidate_hash_config_file_json_format(tmp_path, capsys):
    cfg = _assistant_config()
    path = _write_config(tmp_path, cfg)
    expected = _candidate.config_hash(cfg)

    rc = cli.main(["candidate", "hash", "--config", str(path), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_hash"] == expected
    assert payload["command"] == "candidate hash"


def test_candidate_hash_config_with_provider_is_a_usage_error(tmp_path, capsys):
    path = _write_config(tmp_path, _assistant_config())
    rc = cli.main(["candidate", "hash", "--config", str(path),
                   "--provider", "vapi"])
    assert rc == 2


# =========================================================================
# 4. `candidate verify`: refuse-on-drift (fetch monkeypatched, no network)
# =========================================================================

def _patch_fetch(monkeypatch, config):
    """Inject the live-config fetch so no network is touched. Fails loudly if a
    real API key is ever required (there must be none in a hermetic test)."""
    def _fake(provider, assistant_id, api_key, *, base_url=None, timeout=30):
        assert api_key, "the resolved API key must reach the fetch"
        return config
    monkeypatch.setattr(_candidate, "fetch_assistant", _fake)


def test_candidate_verify_holds_when_unchanged(monkeypatch, capsys):
    cfg = _assistant_config()
    _patch_fetch(monkeypatch, cfg)
    expected = _candidate.config_hash(cfg)

    rc = cli.main(["candidate", "verify", "--provider", "vapi",
                   "--assistant", "asst_123", "--expect", expected,
                   "--api-key", "vapi-key"])
    assert rc == 0
    assert "HELD" in capsys.readouterr().out


def test_candidate_verify_refuses_on_drift(monkeypatch, capsys):
    before = _assistant_config(num_words=0)
    after = _assistant_config(num_words=3)          # a real semantic change
    expected = _candidate.config_hash(before)
    _patch_fetch(monkeypatch, after)                 # the live config drifted

    rc = cli.main(["candidate", "verify", "--provider", "vapi",
                   "--assistant", "asst_123", "--expect", expected,
                   "--api-key", "vapi-key"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "DRIFTED" in out
    assert expected in out                           # before hash shown
    assert _candidate.config_hash(after) in out      # after hash shown


def test_candidate_verify_holds_across_volatile_only_drift(monkeypatch, capsys):
    # A server-touched updatedAt / secret flag is NOT drift: the gate must not
    # fire on noise, only on a real config change.
    before = _assistant_config(updated_at="2026-07-20T10:00:00.000Z")
    after = _assistant_config(updated_at="2026-07-23T11:11:11.000Z",
                              server_secret_set=True)
    expected = _candidate.config_hash(before)
    _patch_fetch(monkeypatch, after)

    rc = cli.main(["candidate", "verify", "--provider", "vapi",
                   "--assistant", "asst_123", "--expect", expected,
                   "--api-key", "vapi-key"])
    assert rc == 0


def test_candidate_verify_unknown_provider_is_exit_2(monkeypatch, capsys):
    _patch_fetch(monkeypatch, _assistant_config())
    rc = cli.main(["candidate", "verify", "--provider", "nope",
                   "--assistant", "asst_123", "--expect", "sha256:x",
                   "--api-key", "k"])
    assert rc == 2


def test_candidate_verify_missing_key_is_exit_2(monkeypatch, capsys):
    _patch_fetch(monkeypatch, _assistant_config())
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    rc = cli.main(["candidate", "verify", "--provider", "vapi",
                   "--assistant", "asst_123", "--expect", "sha256:x"])
    assert rc == 2


def test_candidate_hash_unknown_provider_is_exit_2(monkeypatch, capsys):
    _patch_fetch(monkeypatch, _assistant_config())
    rc = cli.main(["candidate", "hash", "--provider", "nope",
                   "--assistant", "asst_123", "--api-key", "k"])
    assert rc == 2


def test_candidate_hash_missing_key_is_exit_2(monkeypatch, capsys):
    _patch_fetch(monkeypatch, _assistant_config())
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    rc = cli.main(["candidate", "hash", "--provider", "vapi",
                   "--assistant", "asst_123"])
    assert rc == 2


def test_candidate_hash_api_key_falls_back_to_env(monkeypatch, capsys):
    cfg = _assistant_config()
    _patch_fetch(monkeypatch, cfg)
    monkeypatch.setenv("VAPI_API_KEY", "env-key")
    rc = cli.main(["candidate", "hash", "--provider", "vapi",
                   "--assistant", "asst_123"])
    assert rc == 0
    assert _candidate.config_hash(cfg) in capsys.readouterr().out


# =========================================================================
# 5. the emitted hash feeds prove -> claim_scope candidate_revision
# =========================================================================

def _empty_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_hash_feeds_prove_and_yields_candidate_revision(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    cfg = _assistant_config()
    path = _write_config(tmp_path, cfg)

    # 1. compute the hash the pure way (no network).
    rc = cli.main(["candidate", "hash", "--config", str(path), "--format", "json"])
    assert rc == 0
    the_hash = json.loads(capsys.readouterr().out)["config_hash"]

    # 2. feed it to prove alongside a before/after lane (the test_prove.py
    #    empty-dir pattern) -> the proof reads Candidate Revision.
    before = _empty_dir(tmp_path, "before")
    after = _empty_dir(tmp_path, "after")
    out = tmp_path / "proofout"
    cli.main(["prove", "--before", str(before), "--after", str(after),
              "--candidate-config-hash", the_hash, "--provider", "vapi",
              "--out", str(out)])
    with open(out / "proof.json", encoding="utf-8") as fh:
        proof = json.load(fh)
    assert proof["claim_scope"] == "candidate_revision"
    assert proof["evidence"]["candidate_config_hash"] == the_hash
    assert proof["evidence"]["provider"] == "vapi"
    # the runner is not authenticated in this release
    assert proof["evidence_authority"] == "measured"


# =========================================================================
# 6. share-safety: no API key, no absolute path in output or the saved config
# =========================================================================

def test_saved_canonical_config_and_output_carry_no_secret_or_path(
        tmp_path, monkeypatch, capsys):
    cfg = _assistant_config()
    _patch_fetch(monkeypatch, cfg)
    saved = tmp_path / "canon.json"

    rc = cli.main(["candidate", "hash", "--provider", "vapi",
                   "--assistant", "asst_123", "--api-key", "super-secret-key",
                   "--out", str(saved)])
    assert rc == 0

    # the saved canonical config: no API key, no absolute path
    saved_text = saved.read_text(encoding="utf-8")
    assert "super-secret-key" not in saved_text
    assert str(tmp_path) not in saved_text
    # it is the canonicalized config (volatile stripped, semantic kept)
    saved_obj = json.loads(saved_text)
    assert "id" not in saved_obj
    assert saved_obj["model"]["model"] == "gpt-4o"

    # the machine surface (stdout): the hash, no API key
    stdout = capsys.readouterr().out
    assert "super-secret-key" not in stdout
    assert _candidate.config_hash(cfg) in stdout
