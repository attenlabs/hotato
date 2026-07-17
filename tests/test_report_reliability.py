"""REAL reliability numbers (pass@1 / pass@k / pass^k) wired into the report
scorecard's Reliability dimension (Phase 4).

Phase 2 built the reliability() aggregate + run_matrix; this slice retires the
"not yet measured (Phase 2)" placeholder wherever real repetition data exists and
RENDERS it -- pass@1 / pass@k / pass^k with n + the Wilson CI, per-variation
cells, the SIMULATOR_INVALID bucket (excluded from n), and an origin=simulated
label when the runs were simulated. Pins:

  * ``reliability=None`` (and any payload with genuinely no repetition data) is
    BYTE-IDENTICAL (HTML + MD) to a report built without the parameter -- purely
    additive;
  * a real run_matrix summary renders pass@1 / pass@k / pass^k + the Wilson CI +
    the per-variation-cell rows, each number labeled;
  * a SIMULATOR_INVALID bucket is shown and EXCLUDED from n (a broken fixture,
    never an agent PASS/FAIL);
  * simulated-origin runs are labelled origin=simulated (never production);
  * NO overall_score / blended number anywhere -- pass^k stands on its own;
  * ``hotato test run --repetitions N`` (N>1) threads the real reliability
    aggregate end-to-end into the report;
  * the empty-state wording is the honest one ("no repeated runs in this report").
"""

import json
import os
from importlib import resources

import pytest

from hotato import assert_ as A
from hotato import cli, report
from hotato import simulate as SIM

# --- fixtures ---------------------------------------------------------------

def _bundled_wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _demo_trace() -> str:
    return os.path.join(os.path.dirname(__file__), "data", "conversation",
                        "refund.voice_trace.jsonl")


def _empty_assertions() -> dict:
    """A valid assert.v1 envelope with no results. Reliability data ALONE is
    enough to trigger the scorecard, so this is a clean carrier for it."""
    return A.envelope_from_results([], inconclusive_policy="report")


def _dim_envelope() -> dict:
    """A dimension-tagged envelope so the scorecard renders even with NO
    reliability data (the empty-state path)."""
    ctx = A.build_context(spans=[{"type": "tool_call", "start_sec": 0.0,
                                  "end_sec": 0.5, "name": "issue_refund"}])
    doc = {"version": 1, "assertions": [
        {"id": "refunded", "kind": "tool_call", "name": "issue_refund",
         "dimension": "outcome"}]}
    return A.run_assertions(doc, ctx)


def _scenario(**over):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "refund-basic",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "facts": {"order_id": "A-1001"},
        "caller": {
            "script": [
                {"say": "Hi, my order A-1001 arrived damaged and I want a refund."},
                {"when_agent_asks": "order_id", "say": "It is A-1001."},
                {"say": "Please send the refund to my card."},
            ],
            "behavior": {"speaking_rate": 1.0,
                         "backchannels": {"probability": 0.0}},
        },
        "environment": {"noise": "clean", "locale": "en-US"},
        "variation_matrix": {
            "locale": ["en-US", "es-ES"],
            "speaking_rate": [0.9, 1.1],
            "repetitions": 2,
        },
        "seed": 7,
    }
    doc.update(over)
    return doc


def _conv_test(deterministic):
    return {
        "kind": "hotato.conversation-test", "version": 1,
        "id": "refund-test", "agent": "my-agent-v1",
        "assertions": {"deterministic": deterministic},
    }


def _scored_matrix() -> dict:
    """A run_matrix summary scored all-PASS: 2 locale x 2 rate x 1 noise x 1
    behavior x 2 reps = 8 valid runs across 4 variation cells."""
    ct = _conv_test([{"id": "asked-refund", "kind": "phrase",
                      "regex": "refund", "role": "caller"}])
    return SIM.run_matrix(_scenario(), conversation_test=ct)


def _invalid_matrix() -> dict:
    """A run_matrix summary where EVERY produced conversation is
    SIMULATOR_INVALID (the script never states the declared fact A-1001)."""
    doc = _scenario(
        facts={"order_id": "A-1001"},
        caller={"script": [{"say": "My order A-9999 is broken."}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        variation_matrix={"locale": ["en-US", "es-ES"], "repetitions": 2},
    )
    ct = _conv_test([{"id": "asked-refund", "kind": "phrase",
                      "regex": "refund", "role": "caller"}])
    return SIM.run_matrix(doc, conversation_test=ct)


# --- reliability=None is byte-identical (purely additive) -------------------

def test_reliability_none_byte_identical_html_and_md():
    env = _empty_assertions()
    a_html, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    b_html, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env,
                                         reliability=None)
    assert a_html == b_html

    a_md, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env)
    b_md, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env,
                                     reliability=None)
    assert a_md == b_md

    # ... and with no assertions section at all
    c_html, _ = report.build_report_html(stereo=_bundled_wav())
    d_html, _ = report.build_report_html(stereo=_bundled_wav(), reliability=None)
    assert c_html == d_html
    # a reliability=None report carries none of the reliability markup/CSS
    assert "relblock" not in a_html and ".reltable{" not in a_html


def test_no_repetition_data_is_treated_as_none():
    """A reliability() aggregate with n==0 (no runs) and no cells / no invalid
    runs is genuinely no data -> the honest empty-state, byte-identical to
    reliability=None. Never a fabricated zero-row table."""
    env = _dim_envelope()
    empty_agg = SIM.reliability([])  # n == 0
    a, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    b, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env,
                                    reliability=empty_agg)
    assert a == b
    assert "not measured: no repeated runs in this report" in a


# --- a real run_matrix summary renders the real numbers ---------------------

def test_run_matrix_summary_renders_real_numbers_html():
    summary = _scored_matrix()
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_empty_assertions(),
                                       reliability=summary)
    assert '<div class="scorecard">' in html
    assert '<span class="scname">Reliability</span>' in html
    # the three reliability numbers, each labeled
    assert "pass@1 (single-run pass rate)" in html
    assert "pass@k (&gt;=1 of k passed)" in html or "pass@k (>=1 of k passed)" in html
    assert "pass^k (all k passed)" in html
    # n + the Wilson CI, each labeled
    assert "n (runs in aggregate)" in html
    assert "95% Wilson CI (on pass@1)" in html and "(wilson)" in html
    # per-variation-cell rows, with a real cell label
    assert "Per-variation cells" in html
    assert "en-US rate=0.9" in html
    # deterministic caller: pass^k == pass@1 == 1.000 (all runs pass)
    assert "1.000" in html
    # the empty-state is NOT shown (real data present)
    assert "not measured: no repeated runs" not in html
    # no blended score FIELD anywhere -- pass^k stands on its own
    assert '"overall_score"' not in html and '"score"' not in html


def test_run_matrix_summary_renders_real_numbers_md():
    summary = _scored_matrix()
    md, _ = report.build_report_md(stereo=_bundled_wav(),
                                   assertions=_empty_assertions(),
                                   reliability=summary)
    assert "#### Reliability" in md
    assert "pass@1 (single-run pass rate)" in md
    assert "pass^k (all k passed)" in md
    assert "95% Wilson CI (on pass@1)" in md
    assert "Per-variation cells" in md
    assert "en-US rate=0.9" in md
    assert '"overall_score"' not in md and '"score"' not in md


# --- SIMULATOR_INVALID bucket shown + excluded from n -----------------------

def test_simulator_invalid_bucket_shown_and_excluded_from_n_html():
    summary = _invalid_matrix()
    # sanity: the summary really is all-invalid, n == 0 in the aggregate
    assert summary["counts"]["simulator_invalid"] > 0
    assert summary["reliability"]["n"] == 0
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_empty_assertions(),
                                       reliability=summary)
    assert '<span class="scname">Reliability</span>' in html
    # the bucket is shown, plainly excluded from n, never an agent PASS/FAIL
    assert "SIMULATOR_INVALID" in html
    assert "excluded from n" in html
    assert "never an agent PASS/FAIL" in html
    # n is 0 in the aggregate (invalid runs excluded, not counted as passes/fails)
    assert "n (runs in aggregate)" in html
    # each broken fixture is attributable by its reason (states the missing fact)
    assert "A-1001" in html
    # still no fabricated blended score
    assert '"overall_score"' not in html


def test_simulator_invalid_bucket_shown_md():
    summary = _invalid_matrix()
    md, _ = report.build_report_md(stereo=_bundled_wav(),
                                   assertions=_empty_assertions(),
                                   reliability=summary)
    assert "SIMULATOR_INVALID" in md
    assert "excluded from n" in md


# --- simulated-origin labeling (never production reliability) ---------------

def test_simulated_origin_labeling_present_html_and_md():
    summary = _scored_matrix()
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_empty_assertions(),
                                       reliability=summary)
    assert "origin=simulated" in html
    assert "never production reliability" in html
    md, _ = report.build_report_md(stereo=_bundled_wav(),
                                   assertions=_empty_assertions(),
                                   reliability=summary)
    assert "origin=simulated" in md
    assert "never production reliability" in md


# --- NO overall_score / blended number anywhere -----------------------------

def test_no_overall_score_with_reliability_html_and_md():
    summary = _scored_matrix()
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_empty_assertions(),
                                       reliability=summary)
    md, _ = report.build_report_md(stereo=_bundled_wav(),
                                   assertions=_empty_assertions(),
                                   reliability=summary)
    # the FIELD form must never appear (the prose may say "no overall_score")
    assert '"overall_score"' not in html and '"score"' not in html
    assert '"overall_score"' not in md and '"score"' not in md
    # and the copy states pass^k is its own number, never blended
    assert "pass^k is never" in html
    assert "there is no overall_score" in html


# --- the empty-state wording is the honest one ------------------------------

def test_empty_state_wording_is_honest_html_and_md():
    env = _dim_envelope()  # scorecard renders; reliability=None -> empty-state
    html, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    assert "not measured: no repeated runs in this report" in html
    # the retired Phase-2 framing is gone
    assert "Phase 2" not in html and "not yet measured" not in html

    md, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env)
    assert "not measured: no repeated runs in this report" in md
    assert "Phase 2" not in md


# --- a bare reliability() dict also renders ---------------------------------

def test_bare_reliability_dict_renders():
    """The reliability() aggregate dict on its own (no run_matrix wrapper) still
    renders pass@1 / pass@k / pass^k + the CI -- with no per-cell rows."""
    agg = SIM.reliability([True, True, False])  # n=3, 2 passes
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_empty_assertions(),
                                       reliability=agg)
    assert "pass@1 (single-run pass rate)" in html
    assert "95% Wilson CI (on pass@1)" in html
    # 2 of 3 passed -> pass@1 = 0.667, pass@k = 1.000, pass^k = 0.000
    assert "0.667" in html and "1.000" in html and "0.000" in html
    # a bare aggregate carries no per-variation cells
    assert "Per-variation cells" not in html


def test_bad_reliability_payload_raises():
    with pytest.raises(ValueError, match="reliability"):
        report.build_report_html(stereo=_bundled_wav(),
                                 assertions=_empty_assertions(),
                                 reliability="nope")
    with pytest.raises(ValueError, match="unrecognized reliability"):
        report.build_report_html(stereo=_bundled_wav(),
                                 assertions=_empty_assertions(),
                                 reliability={"junk": 1})


# --- end-to-end: `hotato test run --repetitions N` threads real reliability --

def test_test_run_repetitions_threads_real_reliability_into_report(tmp_path):
    doc = {
        "kind": "hotato.conversation-test", "version": 1, "id": "reps-e2e",
        "agent": "a",
        "assertions": {"deterministic": [
            {"id": "refunded", "kind": "tool_call", "name": "issue_refund",
             "dimension": "outcome"}]},
    }
    tf = tmp_path / "t.json"
    tf.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "ca"
    code = cli.main([
        "test", "run", str(tf), "--agent", "a",
        "--trace", _demo_trace(), "--audio", _bundled_wav(),
        "--repetitions", "3", "--format", "html", "--out", str(out),
    ])
    assert code == 0
    page = (out / "report.html").read_text(encoding="utf-8")
    # the REAL reliability numbers landed in the scorecard's Reliability dimension
    assert '<span class="scname">Reliability</span>' in page
    assert "pass^k (all k passed)" in page
    assert "95% Wilson CI (on pass@1)" in page
    # supplied recording files are a stored fixture -> origin=fixture, and it is
    # never laundered as a genuine live capture (real) or a simulated label
    assert "origin=fixture" in page
    assert "origin=real" not in page
    assert "origin=simulated" not in page
    # neither the empty-state nor the retired Phase-2 wording
    assert "not measured: no repeated runs" not in page
    assert "Phase 2" not in page


def test_test_run_single_repetition_shows_empty_state(tmp_path):
    """reps == 1 has no repetition data, so the report shows the honest
    empty-state -- the CLI never passes a one-sample aggregate as reliability."""
    doc = {
        "kind": "hotato.conversation-test", "version": 1, "id": "one-rep",
        "agent": "a",
        "assertions": {"deterministic": [
            {"id": "refunded", "kind": "tool_call", "name": "issue_refund",
             "dimension": "outcome"}]},
    }
    tf = tmp_path / "t.json"
    tf.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "ca"
    cli.main([
        "test", "run", str(tf), "--agent", "a",
        "--trace", _demo_trace(), "--audio", _bundled_wav(),
        "--format", "html", "--out", str(out),
    ])
    page = (out / "report.html").read_text(encoding="utf-8")
    assert "not measured: no repeated runs in this report" in page


def test_reliability_without_assertions_still_renders_never_dropped():
    """Real reliability data with NO assertions envelope must still render its
    Reliability dimension (over an honest empty envelope) -- silent data loss
    would violate the render-what-you-were-given posture. reliability=None with
    assertions=None stays byte-identical (covered elsewhere)."""
    import hotato.simulate as SIM
    agg = SIM.reliability([{"success": True}, {"success": True},
                           {"success": False}])
    html, _ = report.build_report_html(stereo=_bundled_wav(), reliability=agg)
    assert '<span class="scname">Reliability</span>' in html
    assert "pass@1 (single-run pass rate)" in html
    # the empty envelope renders honest zero-counts, never fabricated results
    assert "0 pass / 0 fail / 0 inconclusive" in html
    md, _ = report.build_report_md(stereo=_bundled_wav(), reliability=agg)
    assert "pass@1" in md
    assert '"overall_score"' not in html
