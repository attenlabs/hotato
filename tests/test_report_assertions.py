"""``assert.v1`` results rendered by the HTML/Markdown report, per-dimension
typed cards under two visually separate shelves.

report.py never evaluates an assertion itself -- exactly like ``base`` (a
previous run envelope) and ``transcript`` (an already-produced ASR artifact),
it renders an already-built ``assert.v1`` envelope purely as data. Pins:

  * absent by default -- a report built with no ``assertions`` (or
    ``assertions=None``) is byte-identical to one built before this feature
    existed: no "Assertions" section, no new CSS classes, nothing.
  * two shelves, ALWAYS: "Deterministic (audio / timing / transcript / trace
    derived)" (one PER-DIMENSION TYPED card per result) and "Model-assisted
    (advisory, quarantined)" (always empty in this build, with a note).
  * the headline is ALWAYS "N deterministic pass / M fail  K judge-scored
    (advisory)" -- two counts side by side, never one blended number.
  * NO ``overall_score`` anywhere on the page, ever.
  * a malformed/non-envelope ``assertions`` value is rejected up front
    (ValueError), never silently rendered as an empty shelf.
  * ``pii`` results never leak the raw matched text into the rendered page.
  * byte-stable: the same envelope renders identically across repeated calls.
  * the Markdown renderer mirrors the same two shelves as tables.
"""

import json
from importlib import resources

import pytest

from hotato import assert_ as A
from hotato import report

jsonschema = pytest.importorskip("jsonschema")


def _bundled_wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _schema():
    return json.loads(
        resources.files("hotato").joinpath(
            "schema", "assert.v1.json").read_text(encoding="utf-8")
    )


def _validate(env):
    jsonschema.validate(instance=env, schema=_schema())


def _turn(role, text, start=0.0, end=1.0):
    return {"role": role, "text": text, "start": start, "end": end}


def _tc_span(idx, name, args=None):
    s = {"type": "tool_call", "start_sec": float(idx), "end_sec": float(idx) + 0.5,
         "name": name}
    if args is not None:
        s["arguments"] = args
    return s


def _mixed_envelope():
    """One result per kind: two PASS, one FAIL (pii, with a real SSN hit),
    one PASS tool_call, one PASS outcome -- a realistic multi-kind run."""
    ctx = A.build_context(
        transcript=[
            _turn("agent", "this call is recorded for quality, here is your "
                           "confirmation number 42"),
            _turn("caller", "my ssn is 219-09-9999"),
        ],
        spans=[_tc_span(0, "issue_refund")],
    )
    doc = {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
            {"id": "refunded", "kind": "tool_call", "name": "issue_refund"},
            {"id": "no-ssn", "kind": "pii", "detectors": ["ssn"],
             "mode": "must_not_leak"},
            {"id": "policy", "kind": "policy"},
            {"id": "outcome", "kind": "outcome",
             "all_of": [{"tool_called": "issue_refund"}]},
        ],
    }
    env = A.run_assertions(doc, ctx)
    _validate(env)
    return env


# --- absent by default: byte-identical -------------------------------------

def test_default_html_has_no_assertions_section():
    html, _ = report.build_report_html(suite="barge-in")
    assert "Assertions" not in html
    assert "deterministic pass" not in html
    assert "judge-scored" not in html
    assert "acard" not in html
    assert "overall_score" not in html


def test_explicit_none_is_byte_identical_to_omitted_html():
    a, _ = report.build_report_html(stereo=_bundled_wav())
    b, _ = report.build_report_html(stereo=_bundled_wav(), assertions=None)
    assert a == b


def test_default_md_has_no_assertions_section():
    md, _ = report.build_report_md(suite="barge-in")
    assert "## Assertions" not in md
    assert "deterministic pass" not in md


def test_explicit_none_is_byte_identical_to_omitted_md():
    a, _ = report.build_report_md(stereo=_bundled_wav())
    b, _ = report.build_report_md(stereo=_bundled_wav(), assertions=None)
    assert a == b


def test_no_assertions_css_added_when_absent():
    html, _ = report.build_report_html(suite="barge-in")
    assert ".acard{" not in html
    assert ".shelf-title{" not in html


# --- malformed input: rejected up front, never a silent empty shelf --------

def test_non_dict_assertions_raises_value_error():
    with pytest.raises(ValueError, match="assert.v1"):
        report.build_report_html(suite="barge-in", assertions="nope")


def test_wrong_schema_raises_value_error():
    with pytest.raises(ValueError, match="assert.v1"):
        report.build_report_html(
            suite="barge-in",
            assertions={"schema": "something.else", "results": [], "summary": {}},
        )


def test_missing_results_or_summary_raises_value_error():
    with pytest.raises(ValueError, match="results"):
        report.build_report_html(
            suite="barge-in", assertions={"schema": "assert.v1"}
        )


def test_malformed_assertions_raises_before_any_html_written(tmp_path):
    out = tmp_path / "r.html"
    with pytest.raises(ValueError):
        report.write_report(str(out), suite="barge-in", assertions={"nope": 1})
    assert not out.exists()


# --- headline: always two counts, never a merged score ---------------------

def test_headline_is_two_counts_never_merged():
    env = _mixed_envelope()
    assert env["summary"]["deterministic"] == {"pass": 4, "fail": 1, "inconclusive": 0}
    assert env["summary"]["judge"] == {"pass": 0, "fail": 0}

    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "4 deterministic pass / 1 fail  0 judge-scored (advisory)" in html
    # the prose note itself says "no overall_score anywhere" (documentation,
    # not a field) -- what must never appear is the FIELD, quoted as JSON would
    # render it.
    assert '"overall_score"' not in html
    assert '"score"' not in html

    md, _ = report.build_report_md(suite="barge-in", assertions=env)
    assert "4 deterministic pass / 1 fail  0 judge-scored (advisory)" in md


def test_headline_reflects_all_pass_all_zero_judge():
    ctx = A.build_context(transcript=[_turn("agent", "hello there")])
    env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "a", "kind": "phrase", "regex": "hello"},
        ]}, ctx,
    )
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "1 deterministic pass / 0 fail  0 judge-scored (advisory)" in html


def test_inconclusive_does_not_change_the_headline_shape():
    """INCONCLUSIVE never inflates pass/fail and never forces a third number
    into the headline; it is a plain supplementary note (and visible on its
    own card), never blended into "N deterministic pass / M fail"."""
    ctx = A.build_context()  # nothing supplied at all
    env = A.run_assertions(
        {"version": 1, "assertions": [{"id": "a", "kind": "phrase", "regex": "x"}]},
        ctx,
    )
    assert env["results"][0]["status"] == "INCONCLUSIVE"
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "0 deterministic pass / 0 fail  0 judge-scored (advisory)" in html
    assert "1 inconclusive" in html
    assert "INCONCLUSIVE" in html


# --- two shelves: deterministic populated, judge always empty -------------

def test_two_shelves_present_with_headings():
    env = _mixed_envelope()
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "Deterministic (audio / timing / transcript / trace derived)" in html
    assert "Model-assisted (advisory, quarantined)" in html
    # the judge shelf carries no per-result cards, ever
    det_shelf = html.split('class="shelf det-shelf"')[1].split('class="shelf judge-shelf"')[0]
    judge_shelf = html.split('class="shelf judge-shelf"')[1].split("</section>")[0]
    assert det_shelf.count('class="acard"') == 5  # one typed card per result
    assert "acard" not in judge_shelf
    assert "No judge-scored assertions in this build" in judge_shelf
    assert "docs/ASSERTIONS.md" in judge_shelf


def test_judge_shelf_always_empty_even_with_only_failures():
    ctx = A.build_context(transcript=[_turn("agent", "nope")], spans=[])
    env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "a", "kind": "phrase", "regex": "recorded for quality"},
        ]}, ctx,
    )
    assert env["exit_code"] == 1
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    judge_shelf = html.split('class="shelf judge-shelf"')[1].split("</section>")[0]
    assert "acard" not in judge_shelf


def test_empty_results_list_renders_empty_state_note():
    env = {
        "schema": "assert.v1", "exit_code": 0, "results": [],
        "summary": {
            "deterministic": {"pass": 0, "fail": 0, "inconclusive": 0},
            "judge": {"pass": 0, "fail": 0},
            "note": "0 deterministic assertion(s) in this run",
        },
    }
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "No deterministic assertions in this run." in html
    assert "0 deterministic pass / 0 fail  0 judge-scored (advisory)" in html


# --- per-kind typed rendering ------------------------------------------------

def test_phrase_card_renders_kind_and_status():
    ctx = A.build_context(transcript=[_turn("agent", "recorded for quality")])
    env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "disclosure", "kind": "phrase", "regex": "recorded for quality"},
        ]}, ctx,
    )
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert ">phrase<" in html
    assert ">disclosure<" in html
    assert ">PASS<" in html


def test_tool_call_card_renders_span_ids():
    ctx = A.build_context(spans=[_tc_span(0, "issue_refund")])
    env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "refunded", "kind": "tool_call", "name": "issue_refund"},
        ]}, ctx,
    )
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "spans: s_0" in html


def test_outcome_card_renders_met_of_fraction():
    ctx = A.build_context(spans=[_tc_span(0, "issue_refund")])
    env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "o", "kind": "outcome",
             "all_of": [{"tool_called": "issue_refund"}, {"tool_called": "escalate"}]},
        ]}, ctx,
    )
    assert env["results"][0]["met"] == 1 and env["results"][0]["of"] == 2
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "1 of 2 predicate(s) met" in html


def test_policy_card_renders_pack_and_matched_rules():
    ctx = A.build_context(transcript=[_turn("agent", "well hell, that's odd")])
    env = A.run_assertions(
        {"version": 1, "assertions": [{"id": "p", "kind": "policy"}]}, ctx,
    )
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "pack default v1" in html
    assert "no-profanity" in html


def test_pii_card_shows_hit_count_and_redacted_transcript_only():
    ctx = A.build_context(transcript=[_turn("caller", "my ssn is 219-09-9999")])
    env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "leak", "kind": "pii", "detectors": ["ssn"], "mode": "must_not_leak"},
        ]}, ctx,
    )
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "1 hit(s): ssn" in html
    assert "[REDACTED]" in html
    assert "219-09-9999" not in html
    assert "219099999" not in html


def test_pii_never_leaks_raw_text_anywhere_in_the_page():
    env = _mixed_envelope()
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    md, _ = report.build_report_md(suite="barge-in", assertions=env)
    for page in (html, md):
        assert "219-09-9999" not in page
        assert "219099999" not in page


# --- markdown mirrors the same two shelves ----------------------------------

def test_markdown_renders_both_shelves_and_headline():
    env = _mixed_envelope()
    md, _ = report.build_report_md(suite="barge-in", assertions=env)
    assert "## Assertions" in md
    assert "### Deterministic (audio / timing / transcript / trace derived)" in md
    assert "### Model-assisted (advisory, quarantined)" in md
    assert "No judge-scored assertions in this build" in md
    assert "| disclosure | phrase | PASS | true |" in md
    assert '"overall_score"' not in md


# --- structural placement + determinism -------------------------------------

def test_assertions_section_between_analytics_and_thresholds():
    env = _mixed_envelope()
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    i_an = html.index(">Analytics<")
    i_as = html.index('class="card assertions"')
    i_th = html.index("Thresholds used")
    assert i_an < i_as < i_th


def test_byte_stable_across_repeated_renders():
    env = _mixed_envelope()
    a, _ = report.build_report_html(suite="barge-in", assertions=env)
    b, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert a == b
    ma, _ = report.build_report_md(suite="barge-in", assertions=env)
    mb, _ = report.build_report_md(suite="barge-in", assertions=env)
    assert ma == mb


def test_assertions_never_change_timing_verdicts():
    plain_html, plain_env = report.build_report_html(suite="barge-in")
    env = _mixed_envelope()
    tagged_html, tagged_env = report.build_report_html(suite="barge-in", assertions=env)
    assert plain_env["summary"] == tagged_env["summary"]
    assert [e["verdict"] for e in plain_env["events"]] == [
        e["verdict"] for e in tagged_env["events"]
    ]


# --- write_report passthrough (fmt="html" and fmt="md") ---------------------

def test_write_report_html_carries_assertions_through(tmp_path):
    env = _mixed_envelope()
    out = tmp_path / "r.html"
    report.write_report(str(out), fmt="html", suite="barge-in", assertions=env)
    text = out.read_text(encoding="utf-8")
    assert "4 deterministic pass / 1 fail  0 judge-scored (advisory)" in text


def test_write_report_md_carries_assertions_through(tmp_path):
    env = _mixed_envelope()
    out = tmp_path / "r.md"
    report.write_report(str(out), fmt="md", suite="barge-in", assertions=env)
    text = out.read_text(encoding="utf-8")
    assert "## Assertions" in text
    assert "4 deterministic pass / 1 fail  0 judge-scored (advisory)" in text
