"""Multi-record rendering: ``hotato record render SOURCE --all`` and the
closed ``hotato.failure-record-index.v1`` index (acceptance gate G4).

Pinned here:

  * two failing suite tests produce two content-addressed digit directories
    and a count-2 index in source order;
  * a mixed pass/fail source produces records only for the non-passing units;
  * an all-pass source writes a zero-record index and NO child directory (it
    never fabricates a failure);
  * a duplicate unit id and a SOURCE#id combined with --all are refused BEFORE
    anything is written;
  * ``--limit`` reports total_failures / rendered / truncated honestly;
  * a hostile test id can never affect a directory path (paths are digests, and
    an unsafe id is refused at projection);
  * repeated rendering of the same source is byte-identical;
  * every index entry cross-checks its own failure-record.json, and tampering
    is detected.
"""

import json
import os
import re
from importlib import resources

import pytest

from hotato import cli
from hotato import failure_record as FR
from hotato import failure_render as FRR
from tests._failure_sources import make_suite_run, make_suite_test

_DIR_RE = re.compile(r"^sha256-[0-9a-f]{64}$")
_CHILD_FILES = ("failure-record.json", "failure-record.md",
                "failure-record.html", "failure-record.svg")


def _failing_test(test_id):
    return make_suite_test(
        test_id, exit_code=1,
        dim_counts={"conversation": {"pass": 0, "fail": 1, "inconclusive": 0}},
        dim_reason={"conversation": "latency: too slow"},
    )


def _passing_test(test_id):
    return make_suite_test(test_id, exit_code=0)


def _write(tmp_path, doc, name="suite-run.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _child_dirs(out):
    return sorted(d for d in os.listdir(out) if d.startswith("sha256-"))


def _schema_validate_index(index):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath(
            "schema", "failure-record-index.v1.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=index, schema=schema)


def _all_bytes(root):
    """Every file under ``root`` as ``{relpath: bytes}`` for byte-identity."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            out[os.path.relpath(full, root)] = open(full, "rb").read()
    return out


# --------------------------------------------------------------------------
# two failing tests -> two digest directories + a count-2 index
# --------------------------------------------------------------------------

def test_two_failing_tests_render_two_digest_dirs_and_index(tmp_path):
    suite = make_suite_run([_failing_test("t-one"), _failing_test("t-two")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", src, "--all", "--out", str(out)])
    assert code == 0

    index = json.loads((out / "index.json").read_text("utf-8"))
    assert index["kind"] == "hotato.failure-record-index.v1"
    assert index["version"] == "1.0"
    assert index["source"]["kind"] == "hotato.suite-run"
    assert index["source"]["digest"].startswith("sha256:")
    assert index["total_failures"] == 2
    assert index["rendered"] == 2
    assert index["truncated"] is False
    assert [e["test_id"] for e in index["records"]] == ["t-one", "t-two"]
    _schema_validate_index(index)

    dirs = _child_dirs(out)
    assert len(dirs) == 2
    for entry in index["records"]:
        assert _DIR_RE.match(entry["directory"])
        assert entry["directory"] in dirs
        child = out / entry["directory"]
        for fname in _CHILD_FILES:
            assert (child / fname).exists()
        record = json.loads((child / "failure-record.json").read_text("utf-8"))
        FR.validate_record(record)
        assert record["record_id"] == entry["record_id"]
        assert record["subject"]["test_id"] == entry["test_id"]
        assert record["headline"] == entry["headline"]
        assert record["status"] == entry["status"]

    md = (out / "index.md").read_text("utf-8")
    for entry in index["records"]:
        assert f"{entry['directory']}/failure-record.md" in md
        assert f"{entry['directory']}/failure-record.svg" in md


# --------------------------------------------------------------------------
# mixed pass/fail -> records only for the non-passing units
# --------------------------------------------------------------------------

def test_mixed_pass_fail_records_only_non_passing(tmp_path):
    suite = make_suite_run([_failing_test("bad"), _passing_test("good")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", src, "--all", "--out", str(out)])
    assert code == 0
    index = json.loads((out / "index.json").read_text("utf-8"))
    assert index["total_failures"] == 1
    assert index["rendered"] == 1
    assert [e["test_id"] for e in index["records"]] == ["bad"]
    assert "good" not in [e["test_id"] for e in index["records"]]
    assert len(_child_dirs(out)) == 1


# --------------------------------------------------------------------------
# all-pass -> zero-record index, no child directory, never fabricated
# --------------------------------------------------------------------------

def test_all_pass_writes_zero_record_index_and_no_child(tmp_path):
    suite = make_suite_run([_passing_test("a"), _passing_test("b")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", src, "--all", "--out", str(out)])
    assert code == 0
    index = json.loads((out / "index.json").read_text("utf-8"))
    assert index["total_failures"] == 0
    assert index["rendered"] == 0
    assert index["truncated"] is False
    assert index["records"] == []
    assert index["source"]["kind"] == "hotato.suite-run"
    assert _child_dirs(out) == []
    assert (out / "index.md").exists()
    _schema_validate_index(index)


# --------------------------------------------------------------------------
# refusals happen BEFORE any write
# --------------------------------------------------------------------------

def test_duplicate_unit_ids_refuse_before_writing(tmp_path, capsys):
    suite = make_suite_run([_failing_test("dup"), _failing_test("dup")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", src, "--all", "--out", str(out)])
    assert code == 2
    assert "duplicate" in capsys.readouterr().err
    assert not out.exists()


def test_selector_combined_with_all_is_refused(tmp_path, capsys):
    suite = make_suite_run([_failing_test("t-one"), _failing_test("t-two")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", f"{src}#t-one", "--all",
                     "--out", str(out)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--all" in err
    assert not out.exists()


def test_limit_without_all_is_refused(tmp_path, capsys):
    suite = make_suite_run([_failing_test("t-one")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", src, "--limit", "1", "--out", str(out)])
    assert code == 2
    assert "--limit" in capsys.readouterr().err
    assert not out.exists()


# --------------------------------------------------------------------------
# --limit is honest about truncation
# --------------------------------------------------------------------------

def test_limit_reports_total_rendered_and_truncated(tmp_path):
    suite = make_suite_run([_failing_test("t-one"), _failing_test("t-two")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", src, "--all", "--limit", "1",
                     "--out", str(out)])
    assert code == 0
    index = json.loads((out / "index.json").read_text("utf-8"))
    assert index["total_failures"] == 2
    assert index["rendered"] == 1
    assert index["truncated"] is True
    # the first failing unit in source order is the one rendered
    assert [e["test_id"] for e in index["records"]] == ["t-one"]
    assert len(_child_dirs(out)) == 1
    _schema_validate_index(index)


# --------------------------------------------------------------------------
# hostile test ids can never affect a path
# --------------------------------------------------------------------------

def test_directories_are_digests_not_test_ids(tmp_path):
    suite = make_suite_run([_failing_test("refund"), _failing_test("greeting")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    assert cli.main(["record", "render", src, "--all", "--out", str(out)]) == 0
    dirs = _child_dirs(out)
    assert all(_DIR_RE.match(d) for d in dirs)
    # the test ids never appear as a directory name
    assert "refund" not in os.listdir(out)
    assert "greeting" not in os.listdir(out)


def test_hostile_test_id_is_refused_and_writes_nothing(tmp_path, capsys):
    hostile = make_suite_test(
        "../../etc/passwd", exit_code=1,
        dim_counts={"conversation": {"pass": 0, "fail": 1, "inconclusive": 0}},
        dim_reason={"conversation": "latency: too slow"},
    )
    suite = make_suite_run([hostile])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    code = cli.main(["record", "render", src, "--all", "--out", str(out)])
    assert code == 2
    assert "safe identifier" in capsys.readouterr().err
    assert not out.exists()


# --------------------------------------------------------------------------
# repeated rendering is byte-identical
# --------------------------------------------------------------------------

def test_repeated_render_is_byte_identical(tmp_path):
    suite = make_suite_run([_failing_test("t-one"), _failing_test("t-two")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    assert cli.main(["record", "render", src, "--all", "--out", str(out)]) == 0
    first = _all_bytes(out)
    assert cli.main(["record", "render", src, "--all", "--out", str(out)]) == 0
    assert _all_bytes(out) == first


# --------------------------------------------------------------------------
# index/child cross-check detects tampering
# --------------------------------------------------------------------------

def _crosscheck(out):
    index = json.loads((out / "index.json").read_text("utf-8"))
    for entry in index["records"]:
        child = json.loads(
            (out / entry["directory"] / "failure-record.json").read_text("utf-8"))
        # the child must re-derive to exactly the advertised identity
        assert FR.compute_record_id(child) == entry["record_id"]
        assert child["record_id"] == entry["record_id"]
        assert FRR.record_directory(child) == entry["directory"]
        assert child["headline"] == entry["headline"]
        assert child["status"] == entry["status"]
        assert child["subject"]["test_id"] == entry["test_id"]


def test_index_child_crosscheck_detects_tampering(tmp_path):
    suite = make_suite_run([_failing_test("t-one"), _failing_test("t-two")])
    src = _write(tmp_path, suite)
    out = tmp_path / "records"
    assert cli.main(["record", "render", src, "--all", "--out", str(out)]) == 0
    # a clean set cross-checks
    _crosscheck(out)

    index = json.loads((out / "index.json").read_text("utf-8"))
    entry = index["records"][0]
    child_json = out / entry["directory"] / "failure-record.json"
    original = entry["record_id"]
    hexpart = original.split(":", 1)[1]
    mutated = "sha256:" + ("b" if hexpart[0] != "b" else "c") + hexpart[1:]
    child_json.write_text(
        child_json.read_text("utf-8").replace(original, mutated),
        encoding="utf-8")

    with pytest.raises(AssertionError):
        _crosscheck(out)
