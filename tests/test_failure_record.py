"""Failure Record v1 (failure-record.v1): projection + validation contract.

Pinned here:

  * a FAIL / INCONCLUSIVE / ERROR / advisory-only source projects into a
    record that passes both the ported reference-kit oracle
    (``validate_record``) and the shipped JSON Schema;
  * every hostile-mutation class from the reference kit is refused with the
    kit's own reason;
  * the record is content-addressed (same semantics = same id; no wall-clock
    field participates);
  * transcript text can never establish an outcome claim (refused at
    projection AND validation);
  * the safe projection never leaks a planted sentinel secret, and an
    all-pass source is never relabeled as a failure;
  * the committed reference-kit golden record passes the same ported oracle
    (digest-verified against its evidence files).
"""

import copy
import json
import os
from importlib import resources

import pytest

from hotato import failure_record as FR
from hotato import failure_render as FRR
from tests._failure_sources import (
    det_row,
    make_contract_result,
    make_contract_verify,
    make_suite_run,
    make_suite_test,
    make_test_run,
)

REFERENCE_DIR = os.path.join(os.path.dirname(__file__), "data",
                             "failure-record-reference")


def _schema_validate(record):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath(
            "schema", "failure-record.v1.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=record, schema=schema)


def _reid(record):
    record["record_id"] = FR.compute_record_id(record)
    return record


# --------------------------------------------------------------------------
# valid sources: FAIL / INCONCLUSIVE / ERROR / advisory-only
# --------------------------------------------------------------------------

def test_fail_test_run_projects_a_valid_record():
    record = FR.project(make_test_run())
    assert record["status"] == "FAIL"
    assert record["gate"]["status"] == "FAIL"
    assert record["dimensions"]["outcome"]["status"] == "FAIL"
    assert record["dimensions"]["policy"]["status"] == "PASS"
    assert record["headline"].startswith("refund-issued failed: ")
    checks = FR.validate_record(record)
    assert "five separate dimensions" in checks
    assert "authority boundary" in checks
    _schema_validate(record)


def test_inconclusive_test_run_projects_inconclusive():
    rows = [det_row("disclosure-present", "policy", "INCONCLUSIVE",
                    dimension="policy",
                    reason="no transcript was provided in the context")]
    record = FR.project(make_test_run(rows, exit_code=0))
    assert record["status"] == "INCONCLUSIVE"
    assert record["gate"]["status"] == "INCONCLUSIVE"
    lane = record["dimensions"]["policy"]
    assert lane["status"] == "INCONCLUSIVE"
    assert lane["assertions"][0]["missing_evidence"] == ["transcript"]
    FR.validate_record(record)
    _schema_validate(record)


def test_all_invalid_simulated_suite_projects_error():
    suite = make_suite_run([
        make_suite_test("broken-fixture", exit_code=1, valid_runs=0,
                        simulator_invalid=[{"run_id": "r1", "reason": "x"}]),
    ])
    record = FR.project(suite)
    assert record["status"] == "ERROR"
    assert record["gate"]["status"] == "ERROR"
    conv = record["dimensions"]["conversation"]
    assert conv["assertions"][0]["assertion_id"] == "simulator-validity"
    FR.validate_record(record)
    _schema_validate(record)


def test_advisory_only_failure_keeps_gate_pass_and_gates_record():
    rows = [det_row("refund-issued", "tool_call", "PASS", dimension="outcome")]
    src = make_test_run(
        rows,
        rubric_results=[{"id": "tone", "status": "FAIL"}],
        rubric_gated=True,
        exit_code=1,
    )
    record = FR.project(src)
    assert record["gate"]["status"] == "PASS"
    assert record["advisory"] == {"status": "FAIL", "gate_enabled": True}
    assert record["status"] == "FAIL"
    assert record["headline"].startswith("advisory-gate failed:")
    FR.validate_record(record)
    _schema_validate(record)


def test_advisory_unavailable_never_changes_the_deterministic_gate():
    src = make_test_run()
    src["rubric"]["results"] = []
    record = FR.project(src)
    assert record["advisory"]["status"] == "UNAVAILABLE"
    assert record["advisory"]["reason_code"] == "backend-not-requested"
    assert record["status"] == record["gate"]["status"] == "FAIL"
    FR.validate_record(record)


def test_all_pass_source_is_refused_never_relabeled():
    rows = [det_row("refund-issued", "tool_call", "PASS", dimension="outcome")]
    with pytest.raises(FR.NoFailureError) as err:
        FR.project(make_test_run(rows, exit_code=0))
    assert "no failure" in str(err.value)


# --------------------------------------------------------------------------
# the reference kit's hostile-mutation classes, each refused with its reason
# --------------------------------------------------------------------------

def _mutate_aggregate(record):
    record["overall_score"] = 0.8


def _mutate_advisory_overrides_gate(record):
    record["status"] = "INCONCLUSIVE"


def _mutate_dangling_evidence(record):
    record["dimensions"]["outcome"]["assertions"][0]["evidence_refs"] \
        .append("missing-ref")


def _mutate_path_traversal(record):
    record["evidence"][0]["locator"] = "../private.json"


def _mutate_raw_audio_default(record):
    record["privacy"]["raw_audio_embedded"] = True


def _mutate_wrong_pass_at_k(record):
    record["dimensions"]["reliability"]["pass_at_k"] = 0.0


@pytest.mark.parametrize("mutate,expected_error", [
    (_mutate_aggregate, "aggregate score is forbidden"),
    (_mutate_advisory_overrides_gate,
     "advisory-disabled record changed deterministic status"),
    (_mutate_dangling_evidence, "dangling evidence reference"),
    (_mutate_path_traversal, "unsafe evidence locator"),
    (_mutate_raw_audio_default, "share-safe privacy field must be false"),
    (_mutate_wrong_pass_at_k, "pass@k semantics mismatch"),
], ids=["aggregate-score", "advisory-overrides-gate", "dangling-evidence",
        "path-traversal", "raw-audio-default", "wrong-pass-at-k"])
def test_kit_mutation_class_is_refused(mutate, expected_error):
    record = FR.project(make_test_run())
    candidate = copy.deepcopy(record)
    mutate(candidate)
    _reid(candidate)
    with pytest.raises(ValueError) as err:
        FR.validate_record(candidate)
    assert expected_error in str(err.value)


def test_top_level_contract_is_closed():
    record = copy.deepcopy(FR.project(make_test_run()))
    record["generated_at"] = "2026-01-01T00:00:00Z"
    _reid(record)
    with pytest.raises(ValueError) as err:
        FR.validate_record(record)
    assert "top-level keys differ" in str(err.value)


def test_tampered_record_id_is_refused():
    record = copy.deepcopy(FR.project(make_test_run()))
    record["record_id"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError) as err:
        FR.validate_record(record)
    assert "record_id content digest mismatch" in str(err.value)


def test_gate_cannot_disagree_with_deterministic_assertions():
    record = copy.deepcopy(FR.project(make_test_run()))
    record["gate"]["status"] = "PASS"
    _reid(record)
    with pytest.raises(ValueError) as err:
        FR.validate_record(record)
    assert "deterministic gate does not match" in str(err.value)


# --------------------------------------------------------------------------
# content addressing
# --------------------------------------------------------------------------

def test_record_id_is_the_digest_of_canonical_identity():
    record = FR.project(make_test_run())
    assert record["record_id"] == "sha256:" + __import__("hashlib").sha256(
        FR.canonical_identity_bytes(record)).hexdigest()


def test_same_semantics_same_id_regardless_of_source_key_order():
    src = make_test_run()
    reordered = json.loads(json.dumps(dict(reversed(list(src.items())))))
    assert FR.project(src)["record_id"] == FR.project(reordered)["record_id"]


def test_semantic_change_changes_the_id():
    a = FR.project(make_test_run())
    rows = copy.deepcopy(make_test_run()["assertions"]["results"])
    rows[0]["reason"] = "a different observed failure"
    b = FR.project(make_test_run(rows))
    assert a["record_id"] != b["record_id"]


def test_record_carries_no_wall_clock_field():
    record = FR.project(make_test_run())
    text = json.dumps(record, sort_keys=True).lower()
    for token in ("created_at", "generated_at", "timestamp", "wall_clock"):
        assert token not in text


def test_output_directory_is_presentation_not_identity(tmp_path):
    src = make_test_run()
    path = tmp_path / "result.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    a = FR.project(json.loads(path.read_text()), source_path=str(path))
    b = FR.project(json.loads(path.read_text()), source_path=str(path))
    assert a["record_id"] == b["record_id"]
    assert a["reproduction"]["argv"][-1] == "record"


# --------------------------------------------------------------------------
# authority: transcript text can never establish an outcome
# --------------------------------------------------------------------------

def test_transcript_only_outcome_is_refused_at_projection():
    rows = [det_row("agent-said-refunded", "phrase", "FAIL",
                    dimension="outcome",
                    reason="the agent's words claimed a refund")]
    with pytest.raises(ValueError) as err:
        FR.project(make_test_run(rows))
    message = str(err.value)
    assert "transcript text" in message and "outcome" in message


def test_suite_outcome_failure_without_tool_state_kind_is_refused():
    suite = make_suite_run([
        make_suite_test(
            "refund-claimed", exit_code=1,
            dim_counts={"outcome": {"pass": 0, "fail": 1, "inconclusive": 0}},
            dim_reason={"outcome": "phrase: regex 'refund' matched"},
        ),
    ])
    with pytest.raises(ValueError) as err:
        FR.project(suite)
    assert "transcript text" in str(err.value)


def test_validation_refuses_outcome_claim_without_tool_state_evidence():
    record = copy.deepcopy(FR.project(make_test_run()))
    for item in record["evidence"]:
        if item["kind"] in FR.OUTCOME_EVIDENCE_KINDS:
            item["kind"] = "transcript_span"
    _reid(record)
    with pytest.raises(ValueError) as err:
        FR.validate_record(record)
    assert "transcript text can never establish an outcome" in str(err.value)


# --------------------------------------------------------------------------
# safe projection
# --------------------------------------------------------------------------

SENTINEL = "SENTINEL-SECRET-9f3a77"


def test_sentinel_secret_never_reaches_any_output():
    rows = [
        det_row("refund-issued", "tool_call", "FAIL", dimension="outcome",
                reason="expected a refund.create tool call; none was found",
                span_ids=[SENTINEL], args={"api_key": SENTINEL}),
        det_row("no-pii", "pii", "PASS", dimension="policy",
                hits=[{"text": SENTINEL}]),
    ]
    src = make_test_run(rows)
    src["transcript_turns"] = [{"role": "agent", "text": SENTINEL}]
    src["rubric"]["results"] = [
        {"id": "tone", "status": "PASS", "citations": [SENTINEL]}]
    record = FR.project(src)
    for name, content in FRR.render_all(record).items():
        assert SENTINEL not in content, f"sentinel leaked into {name}"


def test_absolute_paths_are_scrubbed_out_of_summaries():
    rows = [det_row("state-check", "state", "FAIL", dimension="outcome",
                    reason="state adapter query for /home/user/secret/db.json "
                           "failed: boom")]
    record = FR.project(make_test_run(rows))
    observed = record["dimensions"]["outcome"]["assertions"][0]["observed"]
    assert "/home/user" not in observed
    assert "[path]" in observed
    FR.validate_record(record)


def test_windows_and_unc_absolute_paths_are_scrubbed_out_of_summaries():
    # A Windows drive path, a UNC path, and a mixed-separator path -- all
    # EMBEDDED inside a sentence, none at the start -- must be scrubbed just
    # like a POSIX absolute path, and the projected record must still validate.
    rows = [det_row(
        "state-check", "state", "FAIL", dimension="outcome",
        reason=(r"state adapter query for C:\Users\alice\secret\db.json and "
                r"\\fileserver\private\creds.json (mixed C:\a/b\c.txt) failed"))]
    record = FR.project(make_test_run(rows))
    observed = record["dimensions"]["outcome"]["assertions"][0]["observed"]
    assert "C:\\Users" not in observed
    assert "alice" not in observed
    assert "fileserver" not in observed
    assert "creds.json" not in observed
    assert "[path]" in observed
    FR.validate_record(record)


@pytest.mark.parametrize("planted", [
    r"failure at C:\Users\alice\secret\db.json during replay",
    r"see \\fileserver\share\private\evidence.json for detail",
    "the log said /var/log/hotato/secret.log was truncated mid-sentence",
    r"mixed C:\a/b\c.txt embedded here",
])
def test_embedded_absolute_path_anywhere_is_refused(planted):
    # The share-safe profile forbids an absolute path ANYWHERE in the record,
    # not only as a value that STARTS with one. A drive/UNC/POSIX path smuggled
    # mid-sentence into any string field must be refused by the oracle.
    record = FR.project(make_test_run())
    record["headline"] = planted
    record["record_id"] = FR.compute_record_id(record)
    with pytest.raises(ValueError) as err:
        FR.validate_record(record)
    assert "absolute path embedded" in str(err.value)


def test_relative_locators_are_not_mistaken_for_absolute_paths():
    # The embedded detector must NOT flag legitimate relative locators or
    # media types (their separators are preceded by a path/segment character).
    record = FR.project(make_test_run())
    # sanity: the reference record carries relative locators + media types.
    strings = [v for _p, v in FR._walk(record) if isinstance(v, str)]
    assert any("/" in s for s in strings)  # e.g. "application/json"
    FR.validate_record(record)


def test_privacy_flags_are_all_false_by_construction():
    record = FR.project(make_test_run())
    for field in FR.PRIVACY_FALSE_FIELDS:
        assert record["privacy"][field] is False


# --------------------------------------------------------------------------
# reliability projection (copied, never recomputed)
# --------------------------------------------------------------------------

def test_reliability_values_are_copied_with_denominators():
    record = FR.project(make_test_run())
    rel = record["dimensions"]["reliability"]
    assert (rel["trials"], rel["passes"]) == (5, 2)
    assert rel["pass_at_1"] == 0.4
    assert rel["pass_at_k"] == 1.0
    assert rel["pass_caret_k"] == 0.0
    assert rel["wilson_interval"] == {
        "method": "wilson", "confidence": 0.95,
        "lower": 0.117621, "upper": 0.76928,
    }


def test_single_run_reliability_is_honestly_null():
    single = {"pass_at_1": 0.0, "pass_at_k": 0.0, "pass_caret_k": 0.0,
              "n": 1, "k": 1, "passes": 0,
              "ci": {"low": 0.0, "high": 0.79, "method": "wilson", "z": 1.96},
              "note": "one run"}
    record = FR.project(make_test_run(reliability=single))
    rel = record["dimensions"]["reliability"]
    assert rel["trials"] == 1
    assert rel["pass_at_1"] is None
    assert rel["wilson_interval"] is None
    assert rel["status"] == "NOT_RUN"
    FR.validate_record(record)
    _schema_validate(record)


# --------------------------------------------------------------------------
# selectors (suite-run + contract-verify sources)
# --------------------------------------------------------------------------

def _two_failing_suite():
    return make_suite_run([
        make_suite_test("t-one", exit_code=1,
                        dim_counts={"conversation":
                                    {"pass": 0, "fail": 1, "inconclusive": 0}},
                        dim_reason={"conversation": "latency: too slow"}),
        make_suite_test("t-two", exit_code=1,
                        dim_counts={"conversation":
                                    {"pass": 0, "fail": 1, "inconclusive": 0}},
                        dim_reason={"conversation": "latency: too slow"}),
    ])


def test_suite_selector_zero_matches_is_a_distinct_error():
    with pytest.raises(FR.SelectorError) as err:
        FR.project(_two_failing_suite(), selector="no-such-test")
    assert "matches no test" in str(err.value)


def test_suite_multiple_failures_without_selector_is_a_distinct_error():
    with pytest.raises(FR.SelectorError) as err:
        FR.project(_two_failing_suite())
    assert "2 failing" in str(err.value)
    assert "t-one" in str(err.value) and "t-two" in str(err.value)


def test_suite_selector_picks_one_failing_test():
    record = FR.project(_two_failing_suite(), selector="t-two")
    assert record["subject"]["test_id"] == "t-two"
    assert record["subject"]["suite_id"] == "support-regression"
    assert record["subject"]["release_id"] == "rc-2"
    FR.validate_record(record)
    _schema_validate(record)


def test_contract_verify_failing_contract_projects():
    env = make_contract_verify([
        make_contract_result("greeting-yield", passed=False),
        make_contract_result("other-contract", passed=True),
    ])
    record = FR.project(env)
    assert record["subject"]["test_id"] == "greeting-yield"
    assert record["dimensions"]["conversation"]["status"] == "FAIL"
    assert record["status"] == "FAIL"
    FR.validate_record(record)
    _schema_validate(record)


def test_unsupported_source_kind_is_refused():
    with pytest.raises(ValueError) as err:
        FR.project({"kind": "hotato.mystery"})
    assert "unsupported source kind" in str(err.value)


# --------------------------------------------------------------------------
# the committed reference-kit golden passes the same ported oracle
# --------------------------------------------------------------------------

def test_reference_kit_golden_record_passes_the_ported_oracle():
    with open(os.path.join(REFERENCE_DIR, "failure-record.json"),
              encoding="utf-8") as fh:
        record = json.load(fh)
    checks = FR.validate_record(record, root=REFERENCE_DIR)
    assert "content address" in checks
    assert "evidence files and digests" in checks
    assert "reliability semantics" in checks
    _schema_validate(record)


def test_reference_kit_evidence_tamper_is_detected(tmp_path):
    import shutil
    shutil.copytree(REFERENCE_DIR, tmp_path / "kit", dirs_exist_ok=True)
    tampered = tmp_path / "kit" / "evidence" / "tool-call.json"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8")
    with open(tmp_path / "kit" / "failure-record.json", encoding="utf-8") as fh:
        record = json.load(fh)
    with pytest.raises(ValueError) as err:
        FR.validate_record(record, root=str(tmp_path / "kit"))
    assert "evidence digest mismatch" in str(err.value)
