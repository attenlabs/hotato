"""`hotato regression prepare`: capture-to-regression contract.

Pinned here:

  * a confirmed failure + versioned rights/redaction metadata projects into a
    share-safe bundle whose reproduction command re-renders the committed
    Failure Record byte-for-byte, and whose declared status is a failure;
  * the bundle is byte-identical on a second prepare of the same inputs;
  * a sentinel secret planted in the source payload never appears in the
    output bundle;
  * every spec failure case is a clean refusal (exit 2 / ValueError), never a
    silent pass, and never a partial bundle left at --out;
  * a path traversal and a symlink escape out of the declared workspace are
    both rejected;
  * the private-regression and public-corpus profiles differ.

Synthetic sources only (tests/_failure_sources.py): they establish schema,
privacy, determinism, and refusal behaviour, never agent performance.
"""

import copy
import json
import os
import subprocess
import sys

import pytest

from hotato import cli, regression, failure_record as FR

from tests._failure_sources import (
    det_row,
    make_contract_result,
    make_contract_verify,
    make_test_run,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    return path


def _rights(tmp_path, **over):
    doc = {
        "contributor": "acme-eng (pseudonym)",
        "source_description": "internal refund flow regression, call 2026-07",
        "rights_basis": "own recording, internal QA",
        "license": "CC-BY-4.0",
        "consent": "all parties are internal employees, notified in writing",
        "private_data_review": "reviewed 2026-07-12 by j.doe",
        "origin": "captured",
        "intended_use": "private_regression",
        "public_release": False,
    }
    doc.update(over)
    return _write_json(str(tmp_path / "rights.json"), doc)


def _redaction(tmp_path, **over):
    doc = {
        "method": "manual span review, tone replacement",
        "reviewer": "j.doe",
        "completeness_declared": True,
        "unredacted_sentinels": ["<REDACT>"],
    }
    doc.update(over)
    return _write_json(str(tmp_path / "redaction.json"), doc)


def _contract_source(tmp_path, name="verify.json", *, secret=None):
    cv = make_contract_verify(
        [make_contract_result("refund-postcondition", passed=False)])
    if secret is not None:
        # a raw payload field the safe projection must never copy out
        cv["results"][0]["debug_transcript"] = f"caller said {secret}"
    return _write_json(str(tmp_path / name), cv)


def _tree_bytes(root):
    out = {}
    for dp, _dirs, files in os.walk(root):
        for fn in files:
            ap = os.path.join(dp, fn)
            out[os.path.relpath(ap, root)] = open(ap, "rb").read()
    return out


# --------------------------------------------------------------------------
# happy path: bundle, reproduction, determinism
# --------------------------------------------------------------------------

def test_happy_path_bundle_layout(tmp_path):
    src = _contract_source(tmp_path)
    out = str(tmp_path / "staged" / "refund-postcondition")
    res = regression.prepare(
        from_arg=src, rights_path=_rights(tmp_path),
        redaction_path=_redaction(tmp_path), out_dir=out, workspace=str(tmp_path))

    assert res["status"] == "FAIL"
    assert res["profile"] == regression.PROFILE_PRIVATE
    files = set(_tree_bytes(out))
    assert {"README.md", "manifest.json", "rights.json", "redaction.json",
            "test.json", "reproduce.sh",
            os.path.join("evidence", "evidence-index.json"),
            os.path.join("expected", "failure-record.json")} <= files

    manifest = json.loads(open(os.path.join(out, "manifest.json")).read())
    assert manifest["kind"] == "hotato.regression-candidate"
    assert manifest["profile"] == regression.PROFILE_PRIVATE
    assert manifest["reproduction"]["argv"][:3] == ["hotato", "record", "render"]
    assert manifest["reproduction_check"]["reproduced"] is True
    # the manifest digests every other committed file
    assert "expected/failure-record.json" in manifest["files"]
    assert "manifest.json" not in manifest["files"]

    # the committed Failure Record validates through the real oracle
    record = json.loads(
        open(os.path.join(out, "expected", "failure-record.json")).read())
    FR.validate_record(record)
    assert record["status"] == "FAIL"

    # test.json is a real, valid conversation-test document
    from hotato import conversation_test as CT
    test_doc = json.loads(open(os.path.join(out, "test.json")).read())
    CT.validate_conversation_test_doc(test_doc)


def test_reproduce_regenerates_byte_identical_record(tmp_path):
    src = _contract_source(tmp_path)
    out = str(tmp_path / "bundle")
    regression.prepare(
        from_arg=src, rights_path=_rights(tmp_path),
        redaction_path=_redaction(tmp_path), out_dir=out, workspace=str(tmp_path))

    committed = open(
        os.path.join(out, "expected", "failure-record.json"), "rb").read()

    # place the privately-held source at the pinned relative path and run the
    # authoritative reproduction command.
    import shutil
    shutil.copyfile(src, os.path.join(out, "source-result.json"))
    env = {**os.environ, "PYTHONPATH": os.path.abspath("src")}
    re_out = str(tmp_path / "re")
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "record", "render",
         "source-result.json", "--out", re_out],
        cwd=out, env=env, stdout=subprocess.DEVNULL)
    assert proc.returncode == 0
    regenerated = open(
        os.path.join(re_out, "failure-record.json"), "rb").read()
    assert regenerated == committed


def test_double_prepare_is_byte_identical(tmp_path):
    src = _contract_source(tmp_path)
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    kw = dict(rights_path=_rights(tmp_path),
              redaction_path=_redaction(tmp_path), workspace=str(tmp_path))
    regression.prepare(from_arg=src, out_dir=a, **kw)
    regression.prepare(from_arg=src, out_dir=b, **kw)
    assert _tree_bytes(a) == _tree_bytes(b)


def test_sentinel_secret_never_appears_in_output(tmp_path):
    secret = "SSN-123-45-6789-TOPSECRET"
    src = _contract_source(tmp_path, secret=secret)
    out = str(tmp_path / "bundle")
    regression.prepare(
        from_arg=src, rights_path=_rights(tmp_path),
        redaction_path=_redaction(tmp_path), out_dir=out, workspace=str(tmp_path))
    for rel, data in _tree_bytes(out).items():
        assert secret.encode() not in data, f"secret leaked into {rel}"


# --------------------------------------------------------------------------
# an already-projected Failure Record source
# --------------------------------------------------------------------------

def test_failure_record_direct_source(tmp_path):
    cv = make_contract_verify(
        [make_contract_result("refund-postcondition", passed=False)])
    record = FR.project(cv, source_path=None)
    fr_path = _write_json(str(tmp_path / "failure-record.json"), record)
    out = str(tmp_path / "bundle")
    res = regression.prepare(
        from_arg=fr_path, rights_path=_rights(tmp_path),
        redaction_path=_redaction(tmp_path), out_dir=out, workspace=str(tmp_path))
    assert res["reproduction"]["method"] == "content-address"
    # a content-addressed source ships no repro script
    assert not os.path.exists(os.path.join(out, "reproduce.sh"))
    assert res["record_id"] == record["record_id"]


# --------------------------------------------------------------------------
# failure cases: each a clean refusal, no partial output
# --------------------------------------------------------------------------

def _assert_refused(tmp_path, out_name, fn, *, match):
    out = str(tmp_path / out_name)
    with pytest.raises(ValueError, match=match):
        fn(out)
    assert not os.path.exists(out), "a refusal must leave NO output at --out"


def test_refuse_missing_metadata(tmp_path):
    src = _contract_source(tmp_path)
    bad_rights = _rights(tmp_path)
    doc = json.loads(open(bad_rights).read())
    del doc["consent"]
    _write_json(bad_rights, doc)
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=bad_rights,
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(tmp_path)),
        match="missing required metadata")


def test_refuse_malformed_schema(tmp_path):
    src = _contract_source(tmp_path)
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path, origin="unknown"),
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(tmp_path)),
        match="origin")


def test_refuse_digest_mismatch_cross_check(tmp_path):
    src = _contract_source(tmp_path)
    bogus = _write_json(str(tmp_path / "other-record.json"),
                        {"kind": FR.KIND, "record_id": "sha256:" + "0" * 64})
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(tmp_path), record_path=bogus),
        match="digest mismatch")


def test_refuse_digest_mismatch_present_evidence_file(tmp_path):
    # a transcript evidence file present in the workspace whose bytes do not
    # match the digest the record pins for it.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "transcript.json").write_bytes(b"tampered evidence bytes")
    conv = {"artifacts": {"transcript": {"path": "transcript.json",
                                         "sha256": "ab" * 32}}}
    src = _write_json(str(ws / "result.json"), make_test_run(conversation=conv))
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(ws)),
        match="digest mismatch")


def test_refuse_unsupported_or_mixed_audio(tmp_path):
    src = _contract_source(tmp_path)
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(tmp_path, audio={"codec": "opus"}),
            out_dir=out, workspace=str(tmp_path)),
        match="unsupported/mixed audio")


def test_refuse_ambiguous_channel_mapping(tmp_path):
    src = _contract_source(tmp_path)
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(
                tmp_path,
                audio={"codec": "pcm_s16le", "channels": {"caller": 0, "agent": 0}}),
            out_dir=out, workspace=str(tmp_path)),
        match="ambiguous channel mapping")


def test_refuse_redaction_sentinel_remains(tmp_path):
    # a FAIL row whose reason carries the sentinel flows into the record's
    # observed text -> it appears in expected/failure-record.json.
    rows = [det_row("refund-issued", "tool_call", "FAIL", dimension="outcome",
                    reason="expected refund.create; LEFTOVER_MARKER_XYZ present")]
    src = _write_json(str(tmp_path / "result.json"), make_test_run(rows=rows))
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(
                tmp_path, unredacted_sentinels=["LEFTOVER_MARKER_XYZ"]),
            out_dir=out, workspace=str(tmp_path)),
        match="redaction sentinel remains")


def test_refuse_no_failure_source(tmp_path):
    rows = [det_row("greeting", "tool_call", "PASS", dimension="outcome")]
    src = _write_json(str(tmp_path / "pass.json"),
                     make_test_run(rows=rows, exit_code=0))
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(tmp_path)),
        match="does not reproduce")


def test_refuse_public_request_for_private_only_artifact(tmp_path):
    src = _contract_source(tmp_path)
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src,
            rights_path=_rights(tmp_path, intended_use="public_corpus",
                                public_release=False),
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(tmp_path)),
        match="private-only artifact")


def test_refuse_path_traversal(tmp_path):
    conv = {"artifacts": {"transcript": {"path": "../evil.json",
                                         "sha256": "ab" * 32}}}
    src = _write_json(str(tmp_path / "result.json"),
                     make_test_run(conversation=conv))
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(tmp_path)),
        match="unsafe|traver")


def test_refuse_symlink_escape(tmp_path):
    # an evidence file that exists but is a symlink pointing outside workspace.
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFFsecret")
    link = ws / "leak.wav"
    os.symlink(str(outside), str(link))
    conv = {"artifacts": {"audio": {"path": "leak.wav", "sha256": "cd" * 32}}}
    src = _write_json(str(ws / "result.json"), make_test_run(conversation=conv))
    _assert_refused(
        tmp_path, "out",
        lambda out: regression.prepare(
            from_arg=src, rights_path=_rights(tmp_path),
            redaction_path=_redaction(tmp_path), out_dir=out,
            workspace=str(ws)),
        match="unsafe path")


# --------------------------------------------------------------------------
# private vs public profile difference
# --------------------------------------------------------------------------

def test_public_vs_private_profile_difference(tmp_path):
    src = _contract_source(tmp_path)
    priv = str(tmp_path / "priv")
    pub = str(tmp_path / "pub")
    regression.prepare(
        from_arg=src, rights_path=_rights(tmp_path),
        redaction_path=_redaction(tmp_path), out_dir=priv, workspace=str(tmp_path))
    regression.prepare(
        from_arg=src,
        rights_path=_rights(tmp_path, intended_use="public_corpus",
                            public_release=True),
        redaction_path=_redaction(tmp_path), out_dir=pub, workspace=str(tmp_path))

    priv_files = set(_tree_bytes(priv))
    pub_files = set(_tree_bytes(pub))
    # a private regression ships a local repro script; a public corpus artifact
    # does not.
    assert "reproduce.sh" in priv_files
    assert "reproduce.sh" not in pub_files

    priv_m = json.loads(open(os.path.join(priv, "manifest.json")).read())
    pub_m = json.loads(open(os.path.join(pub, "manifest.json")).read())
    assert priv_m["profile"] == regression.PROFILE_PRIVATE
    assert pub_m["profile"] == regression.PROFILE_PUBLIC


# --------------------------------------------------------------------------
# CLI surface + exit codes
# --------------------------------------------------------------------------

def test_cli_prepare_exit_zero(tmp_path, capsys):
    src = _contract_source(tmp_path)
    out = str(tmp_path / "cli-bundle")
    code = cli.main([
        "regression", "prepare", "--from", src,
        "--rights", _rights(tmp_path), "--redaction", _redaction(tmp_path),
        "--out", out, "--workspace", str(tmp_path), "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "regression-prepare"
    assert payload["status"] == "FAIL"
    assert payload["checklist"]


def test_cli_refusal_exit_two_structured(tmp_path, capsys):
    src = _contract_source(tmp_path)
    bad_rights = _rights(tmp_path, intended_use="public_corpus",
                         public_release=False)
    out = str(tmp_path / "cli-refused")
    code = cli.main([
        "regression", "prepare", "--from", src,
        "--rights", bad_rights, "--redaction", _redaction(tmp_path),
        "--out", out, "--workspace", str(tmp_path), "--format", "json"])
    assert code == 2
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False
    assert err["exit_code"] == 2
    assert not os.path.exists(out)
