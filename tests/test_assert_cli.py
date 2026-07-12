"""``hotato assert init`` / ``hotato assert run``: the CLI wiring around the
deterministic assertion engine (:mod:`hotato.assert_`, ``assert.v1``).

Pinned here, against the engine's own contract in tests/test_assert.py:

  * ``assert init --from-trace`` infers a starter assertions.yaml from a
    trace's ``tool_call`` spans (one "was it called" per distinct tool, plus
    a ``require_order`` check when 2+ were observed) and, only with
    ``--stereo``, one ``outcome`` ``field_present`` starter grounded in that
    recording's own scored verdict; the generated file round-trips through
    :func:`hotato.assert_.parse_assertions_yaml` /
    ``validate_assertions_doc`` back to the exact same document;
  * ``init`` refuses to write an empty/fabricated stub when nothing is
    observable, and refuses an existing ``--out`` without ``--force``;
  * ``assert run`` builds a Context from ``--transcript`` (or ``--transcribe``
    over ``--stereo``), ``--trace``, and ``--stereo``'s own freshly scored
    timing, then evaluates ``--assertions`` -- honoring the exit-code
    convention (0 pass/inconclusive-only, 1 a FAIL, 2 usage error);
  * ``--format text`` reports per-kind PASS/FAIL/INCONCLUSIVE counts and the
    deterministic/judge tallies SEPARATELY -- never a merged score;
  * ``--transcribe`` requires ``--stereo`` and is never combined with
    ``--transcript``;
  * every subcommand is registered in ``hotato describe``'s manifest (see
    tests/test_describe_cli.py's updated `_ALL_SUBCOMMANDS`).
"""

from __future__ import annotations

import json
import os
from importlib import resources

import pytest

from hotato import cli

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))          # yields at 2.40

DEMO_OTEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "otel", "demo-trace.otel.jsonl")


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")


def _ingest(tmp_path, otel_path, name="voice_trace.jsonl"):
    out = tmp_path / name
    rc = cli.main(["trace", "ingest", "--otel", str(otel_path), "--out", str(out)])
    assert rc == 0
    return out


def _assert_schema():
    return json.loads(
        resources.files("hotato").joinpath("schema", "assert.v1.json")
        .read_text(encoding="utf-8")
    )


def _validate_envelope(env):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=env, schema=_assert_schema())


# --- assert init: inferred from tool_call spans -----------------------------

def test_init_single_tool_call_writes_starter(tmp_path):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    out = tmp_path / "assertions.yaml"
    rc = cli.main([
        "assert", "init", "--from-trace", str(vt_path), "--out", str(out),
    ])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "lookup_order-called" in text
    assert "require_order" not in text  # only one distinct tool -- no order check

    from hotato import assert_ as A
    doc = A.parse_assertions_yaml(text)
    version, assertions = A.validate_assertions_doc(doc)
    assert version == 1
    assert [a["id"] for a in assertions] == ["lookup_order-called"]
    assert assertions[0] == {"id": "lookup_order-called", "kind": "tool_call", "name": "lookup_order"}


def test_init_multiple_tools_adds_require_order(tmp_path):
    src = tmp_path / "otel.jsonl"
    _write_jsonl(src, [
        {"type": "tool_call", "start_sec": 1.0, "end_sec": 1.2, "name": "verify_identity"},
        {"type": "tool_call", "start_sec": 2.0, "end_sec": 2.3, "name": "lookup_account"},
        {"type": "tool_call", "start_sec": 3.0, "end_sec": 3.4, "name": "issue_refund"},
    ])
    vt_path = _ingest(tmp_path, src)
    out = tmp_path / "assertions.yaml"
    rc = cli.main(["assert", "init", "--from-trace", str(vt_path), "--out", str(out)])
    assert rc == 0

    from hotato import assert_ as A
    doc = A.parse_assertions_yaml(out.read_text(encoding="utf-8"))
    _version, assertions = A.validate_assertions_doc(doc)
    by_id = {a["id"]: a for a in assertions}
    assert set(by_id) == {
        "verify_identity-called", "lookup_account-called",
        "issue_refund-called", "tool-call-order",
    }
    assert by_id["tool-call-order"]["require_order"] == [
        "verify_identity", "lookup_account", "issue_refund",
    ]

    # And the generated doc actually evaluates the way it claims to.
    spans = A.load_spans_file(str(vt_path))
    ctx = A.build_context(spans=spans)
    env = A.run_assertions(doc, ctx)
    assert env["exit_code"] == 0
    assert all(r["status"] == "PASS" for r in env["results"])


def test_init_unsafe_tool_name_is_skipped_not_fabricated(tmp_path, capsys):
    src = tmp_path / "otel.jsonl"
    _write_jsonl(src, [
        {"type": "tool_call", "start_sec": 1.0, "end_sec": 1.2, "name": "weird:name"},
        {"type": "tool_call", "start_sec": 2.0, "end_sec": 2.2, "name": "issue_refund"},
    ])
    vt_path = _ingest(tmp_path, src)
    out = tmp_path / "assertions.yaml"
    capsys.readouterr()
    rc = cli.main([
        "assert", "init", "--from-trace", str(vt_path), "--out", str(out),
        "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped_tool_names"] == ["weird:name"]

    from hotato import assert_ as A
    doc = A.parse_assertions_yaml(out.read_text(encoding="utf-8"))
    _version, assertions = A.validate_assertions_doc(doc)
    # No assertion was fabricated for the unsafe name -- only the safe one.
    assert [a["id"] for a in assertions] == ["issue_refund-called"]
    assert all(a.get("name") != "weird:name" for a in assertions)


def test_init_stereo_seeds_timing_outcome_starter(tmp_path):
    src = tmp_path / "otel.jsonl"
    _write_jsonl(src, [
        {"type": "tool_call", "start_sec": 1.0, "end_sec": 1.2, "name": "issue_refund"},
    ])
    vt_path = _ingest(tmp_path, src)
    out = tmp_path / "assertions.yaml"
    rc = cli.main([
        "assert", "init", "--from-trace", str(vt_path), "--stereo", HARD,
        "--out", str(out),
    ])
    assert rc == 0
    from hotato import assert_ as A
    doc = A.parse_assertions_yaml(out.read_text(encoding="utf-8"))
    _version, assertions = A.validate_assertions_doc(doc)
    by_id = {a["id"]: a for a in assertions}
    assert "produced-a-verdict" in by_id
    assert by_id["produced-a-verdict"]["all_of"] == [{"field_present": "verdict.did_yield"}]


def test_init_without_stereo_has_no_timing_starter(tmp_path):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    out = tmp_path / "assertions.yaml"
    rc = cli.main(["assert", "init", "--from-trace", str(vt_path), "--out", str(out)])
    assert rc == 0
    assert "produced-a-verdict" not in out.read_text(encoding="utf-8")


def test_init_nothing_to_infer_is_usage_error_and_writes_nothing(tmp_path):
    src = tmp_path / "otel.jsonl"
    _write_jsonl(src, [{"type": "asr_partial", "time_sec": 1.0, "text": "hello"}])
    vt_path = _ingest(tmp_path, src)
    out = tmp_path / "assertions.yaml"
    rc = cli.main(["assert", "init", "--from-trace", str(vt_path), "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_init_refuses_existing_out_without_force(tmp_path):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    out = tmp_path / "assertions.yaml"
    assert cli.main(["assert", "init", "--from-trace", str(vt_path), "--out", str(out)]) == 0
    assert cli.main(["assert", "init", "--from-trace", str(vt_path), "--out", str(out)]) == 2
    assert cli.main([
        "assert", "init", "--from-trace", str(vt_path), "--out", str(out), "--force",
    ]) == 0


def test_init_missing_trace_file_is_usage_error(tmp_path):
    out = tmp_path / "assertions.yaml"
    rc = cli.main([
        "assert", "init", "--from-trace", str(tmp_path / "nope.jsonl"), "--out", str(out),
    ])
    assert rc == 2
    assert not out.exists()


def test_init_json_output_format(tmp_path, capsys):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    capsys.readouterr()
    out = tmp_path / "assertions.yaml"
    rc = cli.main([
        "assert", "init", "--from-trace", str(vt_path), "--out", str(out), "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "assert-init"
    assert payload["tool_names"] == ["lookup_order"]
    assert payload["used_timing"] is False
    assert payload["path"] == str(out)


def test_init_default_out_path_is_assertions_yaml(tmp_path, monkeypatch):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["assert", "init", "--from-trace", str(vt_path)])
    assert rc == 0
    assert (tmp_path / "assertions.yaml").exists()


# --- assert run: context wiring ---------------------------------------------

def _assertions_file(tmp_path, text, name="assertions.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


_DISCLOSURE_YAML = (
    "version: 1\n"
    "assertions:\n"
    "  - id: disclosure\n"
    "    kind: phrase\n"
    "    regex: \"recorded for quality\"\n"
    "    role: agent\n"
)


def test_run_transcript_file_and_trace_pass(tmp_path, capsys):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps([
        {"role": "agent", "text": "this call is recorded for quality, one moment"},
    ]))
    assertions = _assertions_file(tmp_path, _DISCLOSURE_YAML)

    rc = cli.main([
        "assert", "run", "--transcript", str(transcript), "--trace", str(vt_path),
        "--assertions", str(assertions),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "disclosure" in out
    assert "judge:" in out
    assert "overall_score" not in out
    assert "deterministic:" in out


def test_run_json_output_validates_against_schema(tmp_path, capsys):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    capsys.readouterr()  # discard the `trace ingest` stdout above
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps([
        {"role": "agent", "text": "recorded for quality, here you go"},
    ]))
    assertions = _assertions_file(tmp_path, (
        "version: 1\n"
        "assertions:\n"
        "  - id: disclosure\n"
        "    kind: phrase\n"
        "    regex: \"recorded for quality\"\n"
        "    role: agent\n"
        "  - id: refunded\n"
        "    kind: tool_call\n"
        "    name: lookup_order\n"
    ))
    rc = cli.main([
        "assert", "run", "--transcript", str(transcript), "--trace", str(vt_path),
        "--assertions", str(assertions), "--format", "json",
    ])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["exit_code"] == 0
    assert {r["kind"] for r in env["results"]} == {"phrase", "tool_call"}
    _validate_envelope(env)


def test_run_a_failing_assertion_exits_1(tmp_path):
    vt_path = _ingest(tmp_path, DEMO_OTEL)
    assertions = _assertions_file(tmp_path, (
        "version: 1\n"
        "assertions:\n"
        "  - id: never-happened\n"
        "    kind: tool_call\n"
        "    name: issue_refund\n"
    ))
    rc = cli.main([
        "assert", "run", "--trace", str(vt_path), "--assertions", str(assertions),
    ])
    assert rc == 1


def test_run_with_nothing_supplied_is_all_inconclusive_and_exits_0(tmp_path, capsys):
    assertions = _assertions_file(tmp_path, _DISCLOSURE_YAML)
    rc = cli.main(["assert", "run", "--assertions", str(assertions)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out


def test_run_malformed_assertions_file_is_usage_error(tmp_path):
    assertions = _assertions_file(tmp_path, (
        "version: 1\nassertions:\n  - id: a\n    kind: not_a_real_kind\n"
    ))
    rc = cli.main(["assert", "run", "--assertions", str(assertions)])
    assert rc == 2


def test_run_missing_assertions_file_is_usage_error(tmp_path):
    rc = cli.main([
        "assert", "run", "--assertions", str(tmp_path / "nope.yaml"),
    ])
    assert rc == 2


def test_run_missing_trace_file_is_usage_error(tmp_path):
    assertions = _assertions_file(tmp_path, _DISCLOSURE_YAML)
    rc = cli.main([
        "assert", "run", "--assertions", str(assertions),
        "--trace", str(tmp_path / "nope.jsonl"),
    ])
    assert rc == 2


def test_run_missing_transcript_file_is_usage_error(tmp_path):
    assertions = _assertions_file(tmp_path, _DISCLOSURE_YAML)
    rc = cli.main([
        "assert", "run", "--assertions", str(assertions),
        "--transcript", str(tmp_path / "nope.json"),
    ])
    assert rc == 2


def test_run_stereo_seeds_timing_context(tmp_path, capsys):
    assertions = _assertions_file(tmp_path, (
        "version: 1\n"
        "assertions:\n"
        "  - id: yielded\n"
        "    kind: outcome\n"
        "    all_of: [{field_present: verdict.did_yield}]\n"
    ))
    rc = cli.main([
        "assert", "run", "--stereo", HARD, "--assertions", str(assertions),
        "--format", "json",
    ])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["results"][0]["status"] == "PASS"


def test_run_transcribe_without_stereo_is_usage_error(tmp_path):
    assertions = _assertions_file(tmp_path, _DISCLOSURE_YAML)
    rc = cli.main([
        "assert", "run", "--transcribe", "--assertions", str(assertions),
    ])
    assert rc == 2


def test_run_transcribe_combined_with_transcript_is_usage_error(tmp_path):
    assertions = _assertions_file(tmp_path, _DISCLOSURE_YAML)
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps([{"role": "agent", "text": "hi"}]))
    rc = cli.main([
        "assert", "run", "--stereo", HARD, "--transcribe",
        "--transcript", str(transcript), "--assertions", str(assertions),
    ])
    assert rc == 2


def test_bare_assert_no_subcommand_is_usage_error():
    # The `init|run` subparser is `required=True` (matching every other
    # subcommand group -- trace/contract/fixture/...): argparse itself exits
    # 2 here rather than returning through cli.main()'s own convention.
    with pytest.raises(SystemExit) as exc:
        cli.main(["assert"])
    assert exc.value.code == 2


# --- describe manifest registration -----------------------------------------

def test_assert_registered_in_describe_manifest(capsys):
    rc = cli.main(["describe", "--format", "json"])
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    names = set()

    def _collect(cmds):
        for c in cmds:
            names.add(c["name"])
            _collect(c.get("subcommands", []))

    _collect(manifest["subcommands"])
    assert {"assert", "assert init", "assert run"} <= names
