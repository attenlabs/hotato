"""Regression test for a path-traversal / arbitrary-file-read defect in
``hotato contract verify``.

``_verify_one`` resolves the audio it re-scores from
``contract.json["bundle"]["paths"]["audio"]`` -- a field read out of the
bundle's own JSON, which is untrusted third-party input (a hand-built bundle,
or one unpacked from a ``.hotato`` archive from another team, whose OWN
authenticity check does not run until AFTER this file is located and scored).

The original code did::

    audio_path = os.path.join(bundle_dir, audio_rel)

``os.path.join`` silently DISCARDS its first argument when the second is an
absolute path, so a contract.json carrying an absolute ``bundle.paths.audio``
(or a ``../`` escape) pointed ``contract verify`` at an arbitrary file on
disk instead of failing closed -- never a corruption case, a real
path-traversal / SSRF-adjacent read of local files (e.g. a decoy WAV planted
outside the bundle, or worse).

This must now be refused with a clean ``ValueError`` (CLI exit 2) BEFORE the
path is ever opened, exactly like every other untrusted-path case in this
codebase (``contract pack/unpack``'s ``_safe_member_parts`` /
``core._load_bundled_scenarios``'s realpath+commonpath containment check).
"""

from __future__ import annotations

import json
import os
import wave
from importlib import resources

import pytest

from hotato import cli
from hotato import contract as _contract

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))


def _bundle(tmp_path, cid):
    return tmp_path / (cid + ".hotato")


def _create(tmp_path, cid="ct-traversal-001"):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", cid,
        "--onset", "2.40", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    return _bundle(tmp_path, cid)


def _contract_json_path(bundle_dir):
    return bundle_dir / "contract.json"


def _load_contract_json(bundle_dir):
    with open(_contract_json_path(bundle_dir), encoding="utf-8") as fh:
        return json.load(fh)


def _write_contract_json(bundle_dir, contract):
    with open(_contract_json_path(bundle_dir), "w", encoding="utf-8") as fh:
        json.dump(contract, fh)


def _write_decoy_wav(path):
    """A real, scorable WAV outside the bundle -- if the traversal succeeded,
    verify would happily re-score THIS file (proof the escape actually
    reaches an outside file, not just a not-scorable/empty one)."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 2 * 16000)


def test_verify_refuses_an_absolute_audio_path(tmp_path):
    bundle_dir = _create(tmp_path, "ct-abs-001")
    decoy = tmp_path / "outside-secret.wav"
    _write_decoy_wav(decoy)

    contract = _load_contract_json(bundle_dir)
    contract["bundle"]["paths"]["audio"] = str(decoy)
    _write_contract_json(bundle_dir, contract)

    with pytest.raises(ValueError, match="unsafe|outside the bundle"):
        _contract.verify_contracts(str(bundle_dir))


def test_verify_refuses_a_dotdot_traversal_audio_path(tmp_path):
    bundle_dir = _create(tmp_path, "ct-dotdot-001")
    decoy = tmp_path / "outside-secret.wav"
    _write_decoy_wav(decoy)

    contract = _load_contract_json(bundle_dir)
    contract["bundle"]["paths"]["audio"] = "../outside-secret.wav"
    _write_contract_json(bundle_dir, contract)

    with pytest.raises(ValueError, match="unsafe|outside the bundle"):
        _contract.verify_contracts(str(bundle_dir))


def test_verify_never_reads_outside_the_bundle_directory(tmp_path):
    # Even when the decoy is scorable and would otherwise happily verify, the
    # traversal must be refused BEFORE any scoring is attempted -- proving
    # this is a containment check, not an incidental not-scorable failure.
    bundle_dir = _create(tmp_path, "ct-noleak-001")
    decoy = tmp_path / "sibling.hotato" / "audio" / "event.wav"
    decoy.parent.mkdir(parents=True)
    _write_decoy_wav(decoy)

    contract = _load_contract_json(bundle_dir)
    contract["bundle"]["paths"]["audio"] = "../sibling.hotato/audio/event.wav"
    _write_contract_json(bundle_dir, contract)

    with pytest.raises(ValueError):
        _contract.verify_contracts(str(bundle_dir))


def test_verify_cli_exit_code_for_traversal_audio_path_is_a_usage_error(tmp_path):
    bundle_dir = _create(tmp_path, "ct-cli-001")
    contract = _load_contract_json(bundle_dir)
    contract["bundle"]["paths"]["audio"] = "/etc/passwd"
    _write_contract_json(bundle_dir, contract)

    assert cli.main(["contract", "verify", str(tmp_path)]) == 2


def test_verify_still_accepts_the_normal_relative_audio_path(tmp_path):
    # The fix must not regress the legitimate, everyday case: a
    # bundle-relative "audio/event.wav" (what `contract create` always
    # writes) still verifies cleanly.
    bundle_dir = _create(tmp_path, "ct-normal-001")
    contract = _load_contract_json(bundle_dir)
    assert contract["bundle"]["paths"]["audio"] == "audio/event.wav"

    v = _contract.verify_contracts(str(bundle_dir))
    assert v["exit_code"] == 0
    assert v["results"][0]["passed"] is True
