"""``hotato record verify``: the public-reader verification command.

Pinned here (acceptance gate G3):

  * a valid record verifies in text and JSON modes, exit 0, and prints its id;
  * a one-byte record mutation is refused (exit 2) with the oracle's own
    ``record_id content digest mismatch`` reason;
  * a traversing evidence locator and an embedded absolute path are refused
    (exit 2);
  * ``--evidence-root`` requires and re-hashes every evidence file, detecting a
    missing or changed file, while the default (structure-only) verification
    succeeds when the private evidence files are intentionally absent;
  * verification opens NO socket (a socket-connect monkeypatch proves it) and
    mutates no file;
  * the JSON verification object is ``hotato.failure-record-verification.v1``
    with valid/record_id/status/privacy_profile/checks and carries no score.
"""

import copy
import json
import os
import shutil
import socket

from hotato import cli
from hotato import failure_record as FR
from hotato import failure_render as FRR
from tests._failure_sources import make_test_run

REFERENCE_DIR = os.path.join(os.path.dirname(__file__), "data",
                             "failure-record-reference")


def _write_record(tmp_path, record, name="failure-record.json"):
    path = tmp_path / name
    path.write_text(FRR.render_json(record), encoding="utf-8")
    return path


def _valid_record():
    return FR.project(make_test_run())


# --------------------------------------------------------------------------
# a valid record verifies in both modes and prints its id
# --------------------------------------------------------------------------

def test_valid_record_verifies_text_and_prints_id(tmp_path, capsys):
    record = _valid_record()
    path = _write_record(tmp_path, record)
    code = cli.main(["record", "verify", str(path)])
    assert code == 0
    out = capsys.readouterr().out
    assert record["record_id"] in out
    assert "VALID" in out
    # the reproduction is labelled as a REGENERATE, never a replay
    assert "Regenerate from the private source result" in out
    assert "replay" not in out.lower()


def test_valid_record_verifies_json_object(tmp_path, capsys):
    record = _valid_record()
    path = _write_record(tmp_path, record)
    code = cli.main(["record", "verify", str(path), "--format", "json"])
    assert code == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["kind"] == "hotato.failure-record-verification.v1"
    assert obj["valid"] is True
    assert obj["record_id"] == record["record_id"]
    assert obj["status"] == record["status"]
    assert obj["privacy_profile"] == "share-safe-v1"
    assert isinstance(obj["checks"], list) and obj["checks"]
    # no blended / aggregate score FIELD on the verification object
    assert "score" not in obj and "overall_score" not in obj


# --------------------------------------------------------------------------
# a one-byte mutation is refused with the oracle's content-address reason
# --------------------------------------------------------------------------

def test_one_byte_record_mutation_is_refused(tmp_path, capsys):
    record = _valid_record()
    path = _write_record(tmp_path, record)
    original = record["record_id"]
    hexpart = original.split(":", 1)[1]
    flipped = ("b" if hexpart[0] != "b" else "c") + hexpart[1:]
    mutated = "sha256:" + flipped
    data = path.read_text(encoding="utf-8")
    new = data.replace(original, mutated)
    assert new != data and new.count(mutated) == 1
    path.write_text(new, encoding="utf-8")

    code = cli.main(["record", "verify", str(path)])
    assert code == 2
    assert "record_id content digest mismatch" in capsys.readouterr().err


def test_one_byte_mutation_json_mode_reports_invalid(tmp_path, capsys):
    record = _valid_record()
    path = _write_record(tmp_path, record)
    original = record["record_id"]
    hexpart = original.split(":", 1)[1]
    mutated = "sha256:" + ("b" if hexpart[0] != "b" else "c") + hexpart[1:]
    path.write_text(path.read_text("utf-8").replace(original, mutated),
                    encoding="utf-8")
    code = cli.main(["record", "verify", str(path), "--format", "json"])
    assert code == 2
    obj = json.loads(capsys.readouterr().out)
    assert obj["valid"] is False
    assert "record_id content digest mismatch" in obj["reason"]


# --------------------------------------------------------------------------
# unsafe internal paths (traversal / absolute) are refused
# --------------------------------------------------------------------------

def test_traversing_evidence_locator_is_refused(tmp_path, capsys):
    record = copy.deepcopy(_valid_record())
    record["evidence"][0]["locator"] = "../private.json"
    record["record_id"] = FR.compute_record_id(record)
    path = _write_record(tmp_path, record)
    code = cli.main(["record", "verify", str(path)])
    assert code == 2
    assert "unsafe evidence locator" in capsys.readouterr().err


def test_embedded_absolute_path_is_refused(tmp_path, capsys):
    record = copy.deepcopy(_valid_record())
    record["headline"] = "verification failed at /var/log/hotato/secret.log now"
    record["record_id"] = FR.compute_record_id(record)
    path = _write_record(tmp_path, record)
    code = cli.main(["record", "verify", str(path)])
    assert code == 2
    assert "absolute path embedded" in capsys.readouterr().err


# --------------------------------------------------------------------------
# --evidence-root: require + re-hash every evidence file
# --------------------------------------------------------------------------

def test_evidence_root_verifies_present_files(tmp_path):
    kit = tmp_path / "kit"
    shutil.copytree(REFERENCE_DIR, kit)
    code = cli.main(["record", "verify", str(kit / "failure-record.json"),
                     "--evidence-root", str(kit)])
    assert code == 0


def test_evidence_root_detects_a_changed_file(tmp_path, capsys):
    kit = tmp_path / "kit"
    shutil.copytree(REFERENCE_DIR, kit)
    tampered = kit / "evidence" / "tool-call.json"
    tampered.write_text(tampered.read_text("utf-8") + "\n", encoding="utf-8")
    code = cli.main(["record", "verify", str(kit / "failure-record.json"),
                     "--evidence-root", str(kit)])
    assert code == 2
    assert "evidence digest mismatch" in capsys.readouterr().err


def test_evidence_root_detects_a_missing_file(tmp_path, capsys):
    # The record alone, with an --evidence-root that has no evidence tree:
    # every required evidence locator is missing.
    lonely = tmp_path / "share"
    lonely.mkdir()
    shutil.copy(os.path.join(REFERENCE_DIR, "failure-record.json"),
                lonely / "failure-record.json")
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    code = cli.main(["record", "verify", str(lonely / "failure-record.json"),
                     "--evidence-root", str(empty_root)])
    assert code == 2
    assert "evidence file missing" in capsys.readouterr().err


def test_evidence_root_detects_a_missing_required_artifact(tmp_path, capsys):
    # Regression: --evidence-root claims to verify against the private source
    # tree. The reproduction required_artifacts (source-result / test-definition)
    # ARE that private source. Deleting them while keeping every evidence
    # locator must be refused -- their existence is enforced symmetrically with
    # evidence files, not silently accepted when absent.
    kit = tmp_path / "kit"
    shutil.copytree(REFERENCE_DIR, kit)
    (kit / "evidence" / "source-result.json").unlink()
    (kit / "evidence" / "test-definition.json").unlink()
    # every EVIDENCE locator file is still present, proving the refusal is about
    # the required artifact and not an evidence file.
    code = cli.main(["record", "verify", str(kit / "failure-record.json"),
                     "--evidence-root", str(kit)])
    assert code == 2
    assert "required-artifact file missing" in capsys.readouterr().err


def test_evidence_root_detects_a_changed_required_artifact(tmp_path, capsys):
    kit = tmp_path / "kit"
    shutil.copytree(REFERENCE_DIR, kit)
    tampered = kit / "evidence" / "source-result.json"
    tampered.write_text(tampered.read_text("utf-8") + "\n", encoding="utf-8")
    code = cli.main(["record", "verify", str(kit / "failure-record.json"),
                     "--evidence-root", str(kit)])
    assert code == 2
    assert "required-artifact digest mismatch" in capsys.readouterr().err


def test_default_verify_succeeds_when_private_evidence_absent(tmp_path):
    # No --evidence-root: structure-only verification does not require the
    # private evidence files to sit next to the shared record.
    lonely = tmp_path / "share"
    lonely.mkdir()
    shutil.copy(os.path.join(REFERENCE_DIR, "failure-record.json"),
                lonely / "failure-record.json")
    assert not (lonely / "evidence").exists()
    code = cli.main(["record", "verify", str(lonely / "failure-record.json")])
    assert code == 0


# --------------------------------------------------------------------------
# malformed input
# --------------------------------------------------------------------------

def test_not_a_json_object_is_exit_2(tmp_path, capsys):
    path = tmp_path / "nope.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    code = cli.main(["record", "verify", str(path)])
    assert code == 2
    assert "not a JSON object" in capsys.readouterr().err


# --------------------------------------------------------------------------
# zero network + zero mutation
# --------------------------------------------------------------------------

def test_verify_opens_no_socket(tmp_path, monkeypatch):
    record = _valid_record()
    path = _write_record(tmp_path, record)

    def guard(*args, **kwargs):
        raise AssertionError("network attempted during hotato record verify")

    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)
    code = cli.main(["record", "verify", str(path)])
    assert code == 0


def test_verify_does_not_mutate_the_record_file(tmp_path):
    record = _valid_record()
    path = _write_record(tmp_path, record)
    before = path.read_bytes()
    cli.main(["record", "verify", str(path)])
    assert path.read_bytes() == before
