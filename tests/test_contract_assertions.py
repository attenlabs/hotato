"""``hotato contract verify`` running a contract's embedded ``assertions``
block (schema/contract.v1.json's optional ``assertions`` -- the same
``{version, assertions}`` document ``hotato assert`` reads from an
assertions.yaml file, see ``hotato.assert_``).

Pinned here:

  * ``contract.v1.json`` and the fixture scenario schema
    (``corpus/label.schema.json``) both accept an optional ``assertions``
    block additively -- a contract/label with no ``assertions`` key at all
    still validates, exactly as before this feature existed;
  * ``contract verify`` evaluates an embedded ``assertions`` block through
    the SAME ``assert.v1`` engine ``hotato assert`` uses (phrase over a
    ``--transcript`` file, pii detectors, tool_call over the bundle's own
    attached trace) and reports it as its own ``assertions`` envelope per
    contract -- a contract with no ``assertions`` block gets ``None``, never
    a fabricated envelope;
  * a deterministic assertion FAIL contributes to the batch's nonzero exit
    code exactly like a timing regression, but the two are reported as
    SEPARATE dimensions: ``summary.passed/failed`` (timing) is never touched
    by an assertion result, and ``assertions_failed`` (a new, separate
    top-level count) is never touched by a timing regression;
  * missing required context (no ``--transcript``, no attached trace) makes
    an assertion INCONCLUSIVE, never a fabricated FAIL -- and INCONCLUSIVE
    never fails the batch;
  * a malformed embedded ``assertions`` block, or an unreadable
    ``--transcript`` file, is a clean usage error (exit 2), never an
    uncaught exception.
"""

from __future__ import annotations

import json
import os as _os
from importlib import resources

import pytest

from hotato import cli
from hotato import contract as _contract

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))          # yields at 2.40

# tests/ carries no __init__.py, so resources.files("tests") is not a valid
# package lookup; resolve the shipped fixture from this file's own location
# instead (mirrors tests/test_trace_cli.py's DEMO_OTEL).
DEMO_OTEL = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "data", "otel", "demo-trace.otel.jsonl")


def _bundle(tmp_path, cid):
    return tmp_path / (cid + ".hotato")


def _create(tmp_path, cid="ct-assert-001", src=HARD, onset="2.40", expect="yield"):
    rc = cli.main([
        "contract", "create", "--stereo", src, "--id", cid,
        "--onset", onset, "--expect", expect, "--out", str(tmp_path),
    ])
    assert rc == 0
    return _bundle(tmp_path, cid)


def _read_contract(bundle_dir):
    with open(bundle_dir / "contract.json", encoding="utf-8") as fh:
        return json.load(fh)


def _write_contract(bundle_dir, doc):
    (bundle_dir / "contract.json").write_text(json.dumps(doc), encoding="utf-8")


def _embed_assertions(bundle_dir, assertions_doc):
    doc = _read_contract(bundle_dir)
    doc["assertions"] = assertions_doc
    _write_contract(bundle_dir, doc)
    return doc


def _write_transcript(tmp_path, turns, name="transcript.json"):
    path = tmp_path / name
    path.write_text(json.dumps(turns), encoding="utf-8")
    return str(path)


def _attach_demo_trace(tmp_path, bundle_dir):
    vt_path = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    assert rc == 0
    rc = cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)])
    assert rc == 0


# --- schema: additive -------------------------------------------------------

def test_contract_schema_accepts_a_contract_with_no_assertions_block():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "contract.v1.json")
        .read_text(encoding="utf-8")
    )
    assert "assertions" not in schema["required"]


def test_contract_schema_accepts_an_embedded_assertions_block(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "contract.v1.json")
        .read_text(encoding="utf-8")
    )
    bundle_dir = _create(tmp_path)
    doc = _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase", "regex": "recorded for quality",
             "role": "agent"},
        ],
    })
    jsonschema.validate(instance=doc, schema=schema)


def test_fixture_scenario_schema_accepts_a_label_with_no_assertions_block():
    jsonschema = pytest.importorskip("jsonschema")
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    schema = json.loads(
        open(_os.path.join(repo, "corpus", "label.schema.json"),
             encoding="utf-8").read()
    )
    label = json.loads(
        open(_os.path.join(repo, "corpus", "examples", "sample-contribution.json"),
             encoding="utf-8").read()
    )
    assert "assertions" not in label
    jsonschema.validate(instance=label, schema=schema)


def test_fixture_scenario_schema_accepts_an_embedded_assertions_block():
    jsonschema = pytest.importorskip("jsonschema")
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    schema = json.loads(
        open(_os.path.join(repo, "corpus", "label.schema.json"),
             encoding="utf-8").read()
    )
    label = json.loads(
        open(_os.path.join(repo, "corpus", "examples", "sample-contribution.json"),
             encoding="utf-8").read()
    )
    label["assertions"] = {
        "version": 1,
        "assertions": [
            {"id": "no-ssn-leak", "kind": "pii", "detectors": ["ssn"],
             "mode": "must_not_leak"},
        ],
    }
    jsonschema.validate(instance=label, schema=schema)


# --- no assertions block: unchanged behavior --------------------------------

def test_verify_with_no_assertions_block_reports_none(tmp_path):
    bundle_dir = _create(tmp_path)
    v = _contract.verify_contracts(str(bundle_dir))
    assert v["exit_code"] == 0
    assert v["assertions_failed"] == 0
    assert v["results"][0]["assertions"] is None


# --- phrase: pass and fail, kept SEPARATE from the timing verdict ----------

def test_verify_passing_phrase_assertion(tmp_path):
    bundle_dir = _create(tmp_path)
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
        ],
    })
    transcript = _write_transcript(tmp_path, [
        {"role": "agent", "text": "This call is recorded for quality assurance."},
        {"role": "caller", "text": "wait, I need a refund"},
    ])
    v = _contract.verify_contracts(str(bundle_dir), transcript_path=transcript)
    assert v["exit_code"] == 0
    assert v["assertions_failed"] == 0
    r = v["results"][0]
    assert r["passed"] is True                       # timing: unaffected
    assert r["assertions"]["exit_code"] == 0
    assert r["assertions"]["results"][0]["status"] == "PASS"


def test_failing_phrase_assertion_fails_verify_but_timing_stays_separate(tmp_path):
    # HARD yields at 2.40 with default policy: the TIMING verdict genuinely
    # passes. The embedded phrase assertion looks for text that is simply
    # not in the supplied transcript, so it genuinely fails. The two
    # dimensions must disagree in the output: passed=True, assertions FAIL.
    bundle_dir = _create(tmp_path, cid="ct-assert-phrase-fail")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
        ],
    })
    transcript = _write_transcript(tmp_path, [
        {"role": "agent", "text": "Sure, let me pull that up for you."},
        {"role": "caller", "text": "wait, I need a refund"},
    ])
    v = _contract.verify_contracts(str(bundle_dir), transcript_path=transcript)

    # Batch-level: an assertion FAIL contributes to the nonzero exit, exactly
    # like a timing regression.
    assert v["exit_code"] == 1
    assert v["assertions_failed"] == 1

    # But the two dimensions are SEPARATE, never blended:
    r = v["results"][0]
    assert r["passed"] is True                       # timing untouched
    assert v["summary"] == {"passed": 1, "failed": 0}  # timing axis untouched
    assert r["assertions"]["exit_code"] == 1
    assert r["assertions"]["results"][0]["status"] == "FAIL"
    assert r["assertions"]["results"][0]["id"] == "disclosure"

    # CLI surface: same story, exit 1, and the rendered text keeps the two
    # verdicts on separate lines.
    rc = cli.main(["contract", "verify", str(bundle_dir), "--transcript", transcript])
    assert rc == 1
    text = _contract.render_verify_text(v)
    assert "[PASS] ct-assert-phrase-fail" in text
    assert "[ASSERTIONS FAIL] ct-assert-phrase-fail" in text


def test_verify_json_output_includes_per_contract_assertions_envelope(tmp_path, capsys):
    bundle_dir = _create(tmp_path, cid="ct-assert-json")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
        ],
    })
    transcript = _write_transcript(tmp_path, [
        {"role": "agent", "text": "no disclosure here"},
    ])
    capsys.readouterr()
    rc = cli.main([
        "contract", "verify", str(bundle_dir),
        "--format", "json", "--transcript", transcript,
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["assertions_failed"] == 1
    r = out["results"][0]
    assert r["assertions"]["schema"] == "assert.v1"
    assert r["assertions"]["summary"]["judge"] == {"pass": 0, "fail": 0}
    assert "overall_score" not in r["assertions"]["summary"]


# --- pii ---------------------------------------------------------------

def test_failing_pii_assertion_fails_verify(tmp_path):
    bundle_dir = _create(tmp_path, cid="ct-assert-pii-fail")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "no-ssn-leak", "kind": "pii", "detectors": ["ssn"],
             "mode": "must_not_leak"},
        ],
    })
    transcript = _write_transcript(tmp_path, [
        {"role": "agent", "text": "Your SSN on file is 123-45-6789, correct?"},
    ])
    v = _contract.verify_contracts(str(bundle_dir), transcript_path=transcript)
    assert v["exit_code"] == 1
    assert v["assertions_failed"] == 1
    r = v["results"][0]
    assert r["passed"] is True
    ares = r["assertions"]["results"][0]
    assert ares["status"] == "FAIL"
    assert ares["kind"] == "pii"
    # the raw PII text is never echoed anywhere in the result
    dumped = json.dumps(r["assertions"])
    assert "123-45-6789" not in dumped
    assert "[REDACTED]" in dumped


def test_passing_pii_assertion_does_not_fail_verify(tmp_path):
    bundle_dir = _create(tmp_path, cid="ct-assert-pii-pass")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "no-ssn-leak", "kind": "pii", "detectors": ["ssn"],
             "mode": "must_not_leak"},
        ],
    })
    transcript = _write_transcript(tmp_path, [
        {"role": "agent", "text": "I cannot share that over the phone."},
    ])
    v = _contract.verify_contracts(str(bundle_dir), transcript_path=transcript)
    assert v["exit_code"] == 0
    assert v["assertions_failed"] == 0
    assert v["results"][0]["assertions"]["results"][0]["status"] == "PASS"


# --- tool_call: reads the bundle's OWN attached trace -----------------------

def test_failing_tool_call_assertion_fails_verify(tmp_path):
    bundle_dir = _create(tmp_path, cid="ct-assert-toolcall-fail")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "must-refund", "kind": "tool_call", "name": "issue_refund"},
        ],
    })
    # The demo trace's ONLY tool_call span is "lookup_order" -- "issue_refund"
    # was genuinely never called.
    _attach_demo_trace(tmp_path, bundle_dir)

    v = _contract.verify_contracts(str(bundle_dir))
    assert v["exit_code"] == 1
    assert v["assertions_failed"] == 1
    r = v["results"][0]
    assert r["passed"] is True                        # timing: untouched
    ares = r["assertions"]["results"][0]
    assert ares["status"] == "FAIL"
    assert ares["kind"] == "tool_call"
    assert "issue_refund" in ares["reason"]


def test_passing_tool_call_assertion_does_not_fail_verify(tmp_path):
    bundle_dir = _create(tmp_path, cid="ct-assert-toolcall-pass")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "looked-up", "kind": "tool_call", "name": "lookup_order"},
        ],
    })
    _attach_demo_trace(tmp_path, bundle_dir)

    v = _contract.verify_contracts(str(bundle_dir))
    assert v["exit_code"] == 0
    assert v["assertions_failed"] == 0
    assert v["results"][0]["assertions"]["results"][0]["status"] == "PASS"


def test_tool_call_assertion_is_inconclusive_without_an_attached_trace(tmp_path):
    # No `trace attach` at all: the bundle's traces/ holds only .gitkeep.
    # A tool_call assertion must report INCONCLUSIVE (missing input), never a
    # fabricated FAIL -- and INCONCLUSIVE must never fail the batch.
    bundle_dir = _create(tmp_path, cid="ct-assert-toolcall-notrace")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "must-refund", "kind": "tool_call", "name": "issue_refund"},
        ],
    })
    v = _contract.verify_contracts(str(bundle_dir))
    assert v["exit_code"] == 0
    assert v["assertions_failed"] == 0
    ares = v["results"][0]["assertions"]["results"][0]
    assert ares["status"] == "INCONCLUSIVE"


def test_phrase_assertion_is_inconclusive_without_a_transcript_flag(tmp_path):
    bundle_dir = _create(tmp_path, cid="ct-assert-phrase-notranscript")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
        ],
    })
    v = _contract.verify_contracts(str(bundle_dir))  # no transcript_path
    assert v["exit_code"] == 0
    assert v["assertions_failed"] == 0
    ares = v["results"][0]["assertions"]["results"][0]
    assert ares["status"] == "INCONCLUSIVE"


# --- batch: mixed contracts, one assertion FAIL among several --------------

def test_batch_one_contract_assertion_fail_among_several_passes(tmp_path):
    good = _create(tmp_path, cid="ct-assert-batch-good")
    bad = _create(tmp_path, cid="ct-assert-batch-bad")
    _embed_assertions(bad, {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
        ],
    })
    transcript = _write_transcript(tmp_path, [{"role": "agent", "text": "nope"}])
    v = _contract.verify_contracts(str(tmp_path), transcript_path=transcript)
    assert v["count"] == 2
    assert v["exit_code"] == 1
    assert v["assertions_failed"] == 1
    # the timing axis never moves: both contracts genuinely pass timing
    assert v["summary"] == {"passed": 2, "failed": 0}


# --- usage errors: malformed assertions / unreadable --transcript ---------

def test_malformed_embedded_assertions_block_is_a_usage_error(tmp_path):
    bundle_dir = _create(tmp_path, cid="ct-assert-malformed")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "bad", "kind": "not-a-real-kind"},
        ],
    })
    assert cli.main(["contract", "verify", str(bundle_dir)]) == 2
    with pytest.raises(ValueError):
        _contract.verify_contracts(str(bundle_dir))


def test_unreadable_transcript_file_is_a_usage_error(tmp_path):
    bundle_dir = _create(tmp_path, cid="ct-assert-badtranscript")
    _embed_assertions(bundle_dir, {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
        ],
    })
    rc = cli.main([
        "contract", "verify", str(bundle_dir),
        "--transcript", str(tmp_path / "nope.json"),
    ])
    assert rc == 2
