"""``hotato prove``: the top-level release proof that composes the existing
evidence lanes (contracts / suite / before-after / gauntlet) into one
fail-closed, content-addressed proof.json + proof.md.

Pinned here:

  * a green contracts lane -> overall pass, exit 0, proof.json + proof.md
    written with every schema field present;
  * a failing contract (the ``start --demo`` bundle) -> overall fail, exit 1;
  * zero activated lanes -> exit 2 usage error and NOTHING written (a proof
    of nothing is refused, not an empty pass);
  * a refused-only run (an empty contracts dir) -> overall inconclusive with
    the lane refused, exit 2, never exit 0;
  * determinism: two runs over the same inputs under a pinned
    SOURCE_DATE_EPOCH write byte-identical proof.json (same content_id);
  * share-safety: no absolute path appears anywhere in the serialized proof;
  * composition honesty: the contracts lane's counts equal what
    hotato.contract.verify_contracts itself reports on the same directory --
    prove adds no scoring engine of its own.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from hotato import cli
from hotato import contract as _contract
from hotato import prove as _prove

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))          # yields at 2.40


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # The demo-contract fixture path (`start --demo`) reads HOTATO_HOME; keep
    # every run hermetic, exactly as tests/test_start_cli.py does.
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))


def _passing_contracts_dir(tmp_path):
    """One passing contract, created with the existing `contract create`
    machinery (the tests/test_contract_cli.py pattern): the bundled
    hard-interruption example yields at 2.40, so expect=yield passes."""
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "prove-pass-001",
        "--onset", "2.40", "--expect", "yield", "--out", str(cdir),
    ])
    assert rc == 0
    return cdir


def _failing_contracts_dir(tmp_path):
    """The demo failure contract `start --demo` creates (FAIL as expected):
    the existing exit-1 gate for `hotato contract verify contracts/`."""
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    rc = cli.main(["start", "--demo", "--dir", str(demo_dir)])
    assert rc == 0
    cdir = demo_dir / "contracts"
    assert (cdir / "demo-missed-interruption.hotato" / "contract.json").is_file()
    return cdir


def _read_proof(out_dir):
    with open(out_dir / "proof.json", encoding="utf-8") as fh:
        return json.load(fh)


# --- 1. green contracts lane -> pass, exit 0, both files, schema fields ----

def test_green_contracts_lane_passes_and_writes_both_files(tmp_path, capsys):
    cdir = _passing_contracts_dir(tmp_path)
    out = tmp_path / "proofout"
    rc = cli.main(["prove", "--contracts", str(cdir), "--out", str(out)])
    assert rc == 0
    assert (out / "proof.json").is_file()
    assert (out / "proof.md").is_file()

    proof = _read_proof(out)
    assert proof["tool"] == "hotato"
    assert proof["schema_version"] == "hotato.proof.v1"
    assert proof["name"] == "proof"
    assert proof["hotato_version"]
    assert proof["created_at"]
    assert proof["overall"] == "pass"
    assert proof["exit_code"] == 0
    assert proof["content_id"].startswith("sha256:")
    assert len(proof["lanes"]) == 1
    lane = proof["lanes"][0]
    assert lane["lane"] == "contracts"
    assert lane["verdict"] == "pass"
    assert lane["counts"]["passed"] == 1
    assert lane["counts"]["failed"] == 0
    assert lane["evidence"][0]["input"] == "contracts"
    assert lane["evidence"][0]["digest"].startswith("sha256:")

    # The text surface carries the lane table, the overall verdict, the
    # content address, and where the proof landed; proof.md mirrors it.
    text = capsys.readouterr().out
    assert "overall PASS" in text
    assert proof["content_id"] in text
    assert "proof.json" in text
    md = (out / "proof.md").read_text(encoding="utf-8")
    assert "PASS" in md
    assert proof["content_id"] in md
    assert "| contracts | pass |" in md


def test_content_id_matches_the_record_pattern(tmp_path):
    cdir = _passing_contracts_dir(tmp_path)
    out = tmp_path / "proofout"
    assert cli.main(["prove", "--contracts", str(cdir), "--out", str(out)]) == 0
    proof = _read_proof(out)
    # sha256 over the canonical JSON WITHOUT the content_id field (the
    # failure-record content-address pattern).
    assert _prove.compute_content_id(proof) == proof["content_id"]


# --- 2. failing contract -> overall fail, exit 1 ---------------------------

def test_failing_contract_fails_the_proof(tmp_path):
    cdir = _failing_contracts_dir(tmp_path)
    out = tmp_path / "proofout"
    rc = cli.main(["prove", "--contracts", str(cdir), "--out", str(out)])
    assert rc == 1
    proof = _read_proof(out)
    assert proof["overall"] == "fail"
    assert proof["exit_code"] == 1
    lane = proof["lanes"][0]
    assert lane["verdict"] == "fail"
    assert lane["counts"]["failed"] >= 1


# --- 3. zero lanes -> exit 2, nothing written ------------------------------

def test_zero_lanes_is_a_usage_error_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "proofout"
    rc = cli.main(["prove", "--out", str(out)])
    assert rc == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "no evidence lane activated" in err


def test_suite_without_agent_is_a_usage_error(tmp_path):
    out = tmp_path / "proofout"
    rc = cli.main(["prove", "--suite", str(tmp_path / "s.json"),
                   "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_before_without_after_is_a_usage_error(tmp_path):
    out = tmp_path / "proofout"
    rc = cli.main(["prove", "--before", str(tmp_path / "b.json"),
                   "--out", str(out)])
    assert rc == 2
    assert not out.exists()


# --- 4. refused-only -> inconclusive, exit 2, never 0 ----------------------

def test_refused_only_is_inconclusive_never_a_pass(tmp_path):
    empty = tmp_path / "contracts"
    empty.mkdir()
    out = tmp_path / "proofout"
    rc = cli.main(["prove", "--contracts", str(empty), "--out", str(out)])
    assert rc == 2
    assert rc != 0
    proof = _read_proof(out)
    assert proof["overall"] == "inconclusive"
    assert proof["exit_code"] == 2
    lane = proof["lanes"][0]
    assert lane["verdict"] == "refused"
    assert lane["counts"] == {}
    assert "refusal" in lane


# --- 5. determinism under a pinned SOURCE_DATE_EPOCH -----------------------

def test_pinned_epoch_makes_proof_json_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    cdir = _passing_contracts_dir(tmp_path)
    out_a = tmp_path / "proof-a"
    out_b = tmp_path / "proof-b"
    assert cli.main(["prove", "--contracts", str(cdir),
                     "--out", str(out_a)]) == 0
    assert cli.main(["prove", "--contracts", str(cdir),
                     "--out", str(out_b)]) == 0
    a = (out_a / "proof.json").read_bytes()
    b = (out_b / "proof.json").read_bytes()
    assert a == b
    assert json.loads(a)["created_at"] == "2023-11-14T22:13:20Z"


# --- 6. share-safety: no absolute path in the serialized proof -------------

def test_proof_json_carries_no_absolute_path(tmp_path):
    cdir = _passing_contracts_dir(tmp_path)
    out = tmp_path / "proofout"
    assert cli.main(["prove", "--contracts", str(cdir), "--out", str(out)]) == 0
    raw = (out / "proof.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in raw
    assert str(cdir) not in raw


def test_refused_proof_carries_no_absolute_path_either(tmp_path):
    # The refusal path is the one that could leak the caller's path through
    # the underlying ValueError message; the lane ships a fixed reason.
    empty = tmp_path / "contracts"
    empty.mkdir()
    out = tmp_path / "proofout"
    assert cli.main(["prove", "--contracts", str(empty),
                     "--out", str(out)]) == 2
    raw = (out / "proof.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in raw


# --- 7. composition honesty: the lane equals verify_contracts itself -------

def test_contracts_lane_equals_verify_contracts_on_the_same_dir(tmp_path):
    cdir = _passing_contracts_dir(tmp_path)
    out = tmp_path / "proofout"
    assert cli.main(["prove", "--contracts", str(cdir), "--out", str(out)]) == 0
    lane = _read_proof(out)["lanes"][0]

    res = _contract.verify_contracts(str(cdir))
    assert lane["counts"]["contracts"] == res["count"]
    assert lane["counts"]["passed"] == res["summary"]["passed"]
    assert lane["counts"]["failed"] == res["summary"]["failed"]
    assert lane["counts"]["tampered"] == res["tampered"]
    assert lane["counts"]["refused"] == res["refused"]
    assert lane["counts"]["assertions_failed"] == res["assertions_failed"]
    assert (lane["verdict"] == "pass") == (res["exit_code"] == 0)


# --- json format parity ----------------------------------------------------

def test_format_json_prints_the_proof_envelope(tmp_path, capsys):
    cdir = _passing_contracts_dir(tmp_path)
    out = tmp_path / "proofout"
    capsys.readouterr()  # drop the `contract create` text output
    rc = cli.main(["prove", "--contracts", str(cdir), "--out", str(out),
                   "--format", "json"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == _read_proof(out)


def test_gauntlet_lane_passes_and_counts_the_bundled_suite(tmp_path, capsys):
    # The gauntlet lane runs the bundled deterministic stress suite through
    # gauntlet.run_gauntlet itself; on the shipped scorer it passes 10/10, so
    # the proof is a pass with the suite's own counts as evidence.
    out = tmp_path / "proofout"
    rc = cli.main(["prove", "--gauntlet", "--out", str(out)])
    assert rc == 0
    capsys.readouterr()
    proof = _read_proof(out)
    assert proof["overall"] == "pass"
    (lane,) = proof["lanes"]
    assert lane["lane"] == "gauntlet"
    assert lane["verdict"] == "pass"
    assert lane["counts"]["total"] == 10
    assert lane["counts"]["passed"] == 10
