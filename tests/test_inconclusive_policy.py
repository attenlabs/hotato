"""``inconclusive_policy``: making a missing-input INCONCLUSIVE result gate CI.

The honesty gap this closes: by default an ``INCONCLUSIVE`` assertion result
(required input simply absent) never forces a non-zero exit, so a suite whose
transcript/trace never arrived stays silently green. ``inconclusive_policy``
lets a suite opt into gating on that -- WITHOUT changing the default.

Pinned here:

  * the exit-code table for all three policies over a 1 pass / 1 fail / 1
    inconclusive result set, over pass+inconclusive (no fail), and over
    only-inconclusive -- and the ``refuse`` exit-2 precedence over a FAIL;
  * the default ``"report"`` is byte-for-byte the historical gating (an
    inconclusive-only run still exits 0);
  * a bad policy value (in the document or passed explicitly) is a usage
    error (``ValueError`` -> exit 2), raised during validation before any
    assertion is evaluated;
  * the envelope ALWAYS carries the ``inconclusive_policy`` actually applied,
    and ``summary.judge`` stays the ``{"pass": 0, "fail": 0}`` quarantine;
  * the ``assert.v1`` schema validates an envelope with AND without the field;
  * the optional top-level ``inconclusive_policy`` key in an assertions.yaml
    is honored, and the ``--inconclusive-policy`` CLI flag overrides it.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from hotato import assert_ as A
from hotato import cli

# --- helpers ----------------------------------------------------------------

def _res(status, aid="x", kind="phrase"):
    """One already-evaluated result -- a valid ``assert.v1`` result shape
    (id/kind/status/deterministic), which is all ``envelope_from_results``
    and the schema need."""
    return {"id": aid, "kind": kind, "status": status, "deterministic": True}


def _turn(role, text):
    return {"role": role, "text": text, "start": 0.0, "end": 1.0}


def _schema():
    return json.loads(
        resources.files("hotato").joinpath("schema", "assert.v1.json")
        .read_text(encoding="utf-8")
    )


def _validate(env):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=env, schema=_schema())


# --- reproduce the gap first ------------------------------------------------

def test_gap_pass_plus_inconclusive_gates_only_under_fail_or_refuse():
    # The exact gap: a suite with one real PASS and one INCONCLUSIVE (missing
    # input) and NO fail. Under the default it silently stays green; the two
    # opt-in policies make the missing input gate.
    results = [_res("PASS", "seen"), _res("INCONCLUSIVE", "missing")]
    assert A.envelope_from_results(results)["exit_code"] == 0                        # report (default): green
    assert A.envelope_from_results(results, "report")["exit_code"] == 0
    assert A.envelope_from_results(results, "fail")["exit_code"] == 1                # fail: gates
    assert A.envelope_from_results(results, "refuse")["exit_code"] == 2             # refuse: refuses


# --- the exit-code table, all three policies --------------------------------

def test_exit_code_table_pass_fail_inconclusive():
    results = [_res("PASS", "a"), _res("FAIL", "b"), _res("INCONCLUSIVE", "c")]
    # report: only the FAIL gates -> 1
    assert A.envelope_from_results(results, "report")["exit_code"] == 1
    # fail: FAIL or INCONCLUSIVE -> 1 (already 1 from the FAIL)
    assert A.envelope_from_results(results, "fail")["exit_code"] == 1
    # refuse: the INCONCLUSIVE refusal (exit 2) takes PRECEDENCE over the FAIL
    assert A.envelope_from_results(results, "refuse")["exit_code"] == 2


def test_exit_code_table_pass_plus_inconclusive_no_fail():
    results = [_res("PASS", "a"), _res("INCONCLUSIVE", "b")]
    assert A.envelope_from_results(results, "report")["exit_code"] == 0
    assert A.envelope_from_results(results, "fail")["exit_code"] == 1
    assert A.envelope_from_results(results, "refuse")["exit_code"] == 2


def test_exit_code_table_only_inconclusive():
    results = [_res("INCONCLUSIVE", "a"), _res("INCONCLUSIVE", "b")]
    assert A.envelope_from_results(results, "report")["exit_code"] == 0
    assert A.envelope_from_results(results, "fail")["exit_code"] == 1
    assert A.envelope_from_results(results, "refuse")["exit_code"] == 2


def test_refuse_precedence_over_fail_is_explicit():
    # A run that both FAILs and cannot see all its inputs REFUSES (exit 2)
    # rather than reporting the partial FAIL (exit 1): the refusal wins.
    fail_and_incon = [_res("FAIL", "a"), _res("INCONCLUSIVE", "b")]
    assert A.envelope_from_results(fail_and_incon, "refuse")["exit_code"] == 2
    # ... but with NO inconclusive, refuse falls back to the ordinary FAIL/pass
    # exit codes (2 is reserved strictly for a refusal on missing input).
    assert A.envelope_from_results([_res("FAIL", "a")], "refuse")["exit_code"] == 1
    assert A.envelope_from_results([_res("PASS", "a")], "refuse")["exit_code"] == 0


def test_fail_policy_only_gates_when_something_is_inconclusive_or_failed():
    # An all-PASS run is 0 under every policy -- the policies only change how
    # INCONCLUSIVE is treated, never a clean pass.
    all_pass = [_res("PASS", "a"), _res("PASS", "b")]
    for policy in A.INCONCLUSIVE_POLICIES:
        assert A.envelope_from_results(all_pass, policy)["exit_code"] == 0


# --- default preserves the historical behavior ------------------------------

def test_default_is_report_and_matches_no_arg():
    results = [_res("PASS", "a"), _res("INCONCLUSIVE", "b")]
    default = A.envelope_from_results(results)
    explicit = A.envelope_from_results(results, "report")
    assert A.DEFAULT_INCONCLUSIVE_POLICY == "report"
    assert default["exit_code"] == explicit["exit_code"] == 0
    assert default["inconclusive_policy"] == "report"
    # historical gating: an inconclusive-only run is still exit 0
    assert A.envelope_from_results([_res("INCONCLUSIVE", "a")])["exit_code"] == 0


# --- the envelope always carries the applied policy + keeps the judge wall ---

def test_envelope_always_carries_inconclusive_policy():
    for policy in A.INCONCLUSIVE_POLICIES:
        env = A.envelope_from_results([_res("PASS", "a")], policy)
        assert env["inconclusive_policy"] == policy
    # even the no-arg default carries it
    assert A.envelope_from_results([_res("PASS", "a")])["inconclusive_policy"] == "report"


def test_summary_note_states_the_applied_policy_and_judge_stays_quarantined():
    env = A.envelope_from_results([_res("PASS", "a"), _res("INCONCLUSIVE", "b")], "fail")
    note = env["summary"]["note"]
    assert "inconclusive_policy=fail" in note
    assert "1 inconclusive" in note
    # the honesty wall is untouched: judge stays the {0,0} quarantine, no
    # blended/overall score anywhere.
    assert env["summary"]["judge"] == {"pass": 0, "fail": 0}
    dumped = json.dumps(env)
    assert "overall_score" not in dumped
    assert "\"score\"" not in dumped


# --- bad policy value is a usage error, raised before evaluation ------------

def test_bad_policy_value_in_envelope_builder_raises():
    with pytest.raises(ValueError, match="inconclusive_policy"):
        A.envelope_from_results([_res("PASS", "a")], "sometimes")


def test_bad_policy_value_in_document_raises_during_validation():
    doc = {
        "version": 1,
        "inconclusive_policy": "maybe",
        "assertions": [{"id": "a", "kind": "phrase", "regex": "x"}],
    }
    with pytest.raises(ValueError, match="inconclusive_policy"):
        A.validate_assertions_doc(doc)
    # and run_assertions raises the same way, before any assertion is evaluated
    ctx = A.build_context(transcript=[_turn("agent", "x")])
    with pytest.raises(ValueError, match="inconclusive_policy"):
        A.run_assertions(doc, ctx)


def test_bad_policy_value_passed_explicitly_raises():
    doc = {"version": 1, "assertions": [{"id": "a", "kind": "phrase", "regex": "x"}]}
    ctx = A.build_context(transcript=[_turn("agent", "x")])
    with pytest.raises(ValueError, match="inconclusive_policy"):
        A.run_assertions(doc, ctx, inconclusive_policy="loud")


# --- schema validates envelopes with AND without the field ------------------

def test_schema_validates_envelope_with_the_field():
    for policy in A.INCONCLUSIVE_POLICIES:
        env = A.envelope_from_results(
            [_res("PASS", "a"), _res("INCONCLUSIVE", "b")], policy
        )
        assert "inconclusive_policy" in env
        _validate(env)


def test_schema_validates_legacy_envelope_without_the_field():
    # An envelope produced before this field existed (no inconclusive_policy)
    # must still validate -- the field is additive/optional, never required.
    env = A.envelope_from_results([_res("PASS", "a")])
    env.pop("inconclusive_policy")
    assert "inconclusive_policy" not in env
    _validate(env)


# --- the key is honored end-to-end via run_assertions -----------------------

_PHRASE_DOC = (
    "version: 1\n"
    "assertions:\n"
    "  - id: disclosure\n"
    "    kind: phrase\n"
    "    regex: \"recorded for quality\"\n"
    "    role: agent\n"
)

# same doc, but declaring the policy as a top-level key
_PHRASE_DOC_FAIL = "version: 1\ninconclusive_policy: fail\n" + _PHRASE_DOC.split("\n", 1)[1]
_PHRASE_DOC_REFUSE = "version: 1\ninconclusive_policy: refuse\n" + _PHRASE_DOC.split("\n", 1)[1]


def test_document_key_is_read_and_drives_the_exit_code():
    # No transcript at all -> the phrase assertion is INCONCLUSIVE.
    ctx = A.build_context()
    assert A.run_assertions_from_yaml(_PHRASE_DOC, ctx)["exit_code"] == 0            # default report
    fail_env = A.run_assertions_from_yaml(_PHRASE_DOC_FAIL, ctx)
    assert fail_env["exit_code"] == 1
    assert fail_env["inconclusive_policy"] == "fail"
    refuse_env = A.run_assertions_from_yaml(_PHRASE_DOC_REFUSE, ctx)
    assert refuse_env["exit_code"] == 2
    assert refuse_env["inconclusive_policy"] == "refuse"


def test_explicit_argument_overrides_the_document_key():
    ctx = A.build_context()  # phrase -> INCONCLUSIVE
    # doc says refuse (would be exit 2); an explicit "report" wins -> exit 0
    env = A.run_assertions_from_yaml(_PHRASE_DOC_REFUSE, ctx, inconclusive_policy="report")
    assert env["exit_code"] == 0
    assert env["inconclusive_policy"] == "report"
    # doc says nothing; an explicit "fail" gates the INCONCLUSIVE -> exit 1
    env2 = A.run_assertions_from_yaml(_PHRASE_DOC, ctx, inconclusive_policy="fail")
    assert env2["exit_code"] == 1
    assert env2["inconclusive_policy"] == "fail"


def test_real_pass_plus_inconclusive_run_matches_the_gap():
    # A real evaluation (not synthetic results): a phrase that genuinely
    # PASSes over the transcript, plus a tool_call that is INCONCLUSIVE because
    # no trace was supplied. This is the gap, end to end.
    ctx = A.build_context(transcript=[_turn("agent", "this call is recorded for quality")])
    doc = {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase", "regex": "recorded for quality", "role": "agent"},
            {"id": "refunded", "kind": "tool_call", "name": "issue_refund"},
        ],
    }
    statuses = {r["id"]: r["status"] for r in A.run_assertions(doc, ctx)["results"]}
    assert statuses == {"disclosure": "PASS", "refunded": "INCONCLUSIVE"}
    assert A.run_assertions(doc, ctx, "report")["exit_code"] == 0
    assert A.run_assertions(doc, ctx, "fail")["exit_code"] == 1
    assert A.run_assertions(doc, ctx, "refuse")["exit_code"] == 2


# --- CLI: the flag overrides the document key -------------------------------

def _write(tmp_path, text, name="assertions.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_cli_flag_gates_an_inconclusive_only_run(tmp_path, capsys):
    # No transcript/trace passed -> the phrase assertion is INCONCLUSIVE.
    assertions = _write(tmp_path, _PHRASE_DOC)
    assert cli.main(["assert", "run", "--assertions", assertions]) == 0           # default report
    assert cli.main([
        "assert", "run", "--assertions", assertions, "--inconclusive-policy", "fail",
    ]) == 1
    assert cli.main([
        "assert", "run", "--assertions", assertions, "--inconclusive-policy", "refuse",
    ]) == 2


def test_cli_flag_overrides_the_document_key(tmp_path):
    # The file declares refuse; the flag forces report back -> exit 0.
    assertions = _write(tmp_path, _PHRASE_DOC_REFUSE)
    assert cli.main(["assert", "run", "--assertions", assertions]) == 2           # doc's refuse honored
    assert cli.main([
        "assert", "run", "--assertions", assertions, "--inconclusive-policy", "report",
    ]) == 0                                                                        # flag wins


def test_cli_json_output_carries_the_policy_and_validates(tmp_path, capsys):
    assertions = _write(tmp_path, _PHRASE_DOC_FAIL)
    capsys.readouterr()
    rc = cli.main(["assert", "run", "--assertions", assertions, "--format", "json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["inconclusive_policy"] == "fail"
    assert env["results"][0]["status"] == "INCONCLUSIVE"
    _validate(env)


def test_cli_bad_policy_value_in_file_is_usage_error(tmp_path):
    assertions = _write(
        tmp_path, "version: 1\ninconclusive_policy: nope\n" + _PHRASE_DOC.split("\n", 1)[1]
    )
    assert cli.main(["assert", "run", "--assertions", assertions]) == 2


def test_cli_text_output_surfaces_a_non_default_policy(tmp_path, capsys):
    assertions = _write(tmp_path, _PHRASE_DOC)
    capsys.readouterr()
    # default report: text output does not mention the policy (byte-compat)
    cli.main(["assert", "run", "--assertions", assertions])
    assert "inconclusive_policy" not in capsys.readouterr().out
    # refuse: the reason for the exit-2 is surfaced
    cli.main([
        "assert", "run", "--assertions", assertions, "--inconclusive-policy", "refuse",
    ])
    assert "inconclusive_policy: refuse" in capsys.readouterr().out
