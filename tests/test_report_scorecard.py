"""Per-dimension SCORECARD + conversation-artifact provenance in the report.

Phase-1 SLICE 3. The scorecard is an ADDITIONAL grouped VIEW of the SAME
assert.v1 deterministic results -- grouped by their optional ``dimension`` TAG
into the five report dimensions (outcome / policy / conversation / speech /
reliability) plus an "Ungrouped" bucket. Pins the honesty invariants:

  * a ``dimension`` on an assertion propagates verbatim onto its result, and is
    schema-valid; an untagged assertion's result carries no dimension.
  * the scorecard shows each dimension's OWN pass/fail/inconclusive counts --
    NO blended or overall number across dimensions or within one, and NO
    ``overall_score`` field anywhere.
  * untagged results go to Ungrouped; nothing is ever silently dropped.
  * Reliability (pass^k, Phase 2) shows an explicit "not yet measured"
    placeholder, never a fabricated value.
  * the deterministic / model-assisted (quarantined) shelf split stays intact.
  * absent by default -- an assertions envelope with NO dimensions renders the
    flat deterministic shelf byte-identically to before the scorecard existed,
    and a report with conversation=None is byte-identical to before.
  * the optional ``conversation`` param renders a "Conversation artifact
    (provenance)" section (real|simulated origin + bound digests), additive to
    the envelope, never touching any timing/verdict field.
"""

import json
from importlib import resources

import pytest

from hotato import assert_ as A
from hotato import conversation as CV
from hotato import report

jsonschema = pytest.importorskip("jsonschema")


def _bundled_wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _assert_schema():
    return json.loads(
        resources.files("hotato").joinpath(
            "schema", "assert.v1.json").read_text(encoding="utf-8")
    )


def _validate(env):
    jsonschema.validate(instance=env, schema=_assert_schema())


def _turn(role, text, start=0.0, end=1.0):
    return {"role": role, "text": text, "start": start, "end": end}


def _tc_span(idx, name):
    return {"type": "tool_call", "start_sec": float(idx),
            "end_sec": float(idx) + 0.5, "name": name}


def _dim_envelope():
    """A multi-dimension run: policy (one PASS, one FAIL), outcome (PASS),
    conversation (PASS), and one UNTAGGED result -> Ungrouped. Speech and
    Reliability have no results. Validated against the assert.v1 schema, so a
    dimension-carrying result is proven schema-valid."""
    ctx = A.build_context(
        transcript=[
            _turn("agent", "this call is recorded for quality, confirmation 42"),
            _turn("caller", "my ssn is 219-09-9999"),
        ],
        spans=[_tc_span(0, "issue_refund")],
    )
    doc = {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase", "regex": "recorded for quality",
             "role": "agent", "dimension": "policy"},
            {"id": "no-ssn", "kind": "pii", "detectors": ["ssn"],
             "mode": "must_not_leak", "dimension": "policy"},
            {"id": "refunded", "kind": "tool_call", "name": "issue_refund",
             "dimension": "outcome"},
            {"id": "empathy", "kind": "outcome",
             "all_of": [{"tool_called": "issue_refund"}], "dimension": "conversation"},
            {"id": "untagged", "kind": "phrase", "regex": "confirmation",
             "role": "agent"},
        ],
    }
    env = A.run_assertions(doc, ctx)
    _validate(env)
    return env


def _no_dim_envelope():
    """The same shape with NO dimension tags -- the flat-shelf path."""
    ctx = A.build_context(
        transcript=[_turn("agent", "this call is recorded for quality")],
    )
    doc = {
        "version": 1,
        "assertions": [
            {"id": "disclosure", "kind": "phrase",
             "regex": "recorded for quality", "role": "agent"},
        ],
    }
    env = A.run_assertions(doc, ctx)
    _validate(env)
    return env


def _conv_manifest(kind="real", **origin_extra):
    origin = {"kind": kind}
    if kind == "simulated":
        origin["simulator"] = {"model_id": "gpt-sim-x", "scenario_id": "refund-1",
                               "seed": 7}
    origin.update(origin_extra)
    return {
        "kind": "hotato.conversation", "version": 1,
        "conversation_id": "conv-abc", "agent_id": "support-v3",
        "created_at": "2026-07-12T00:00:00Z",
        "origin": origin,
        "artifacts": {
            "audio": {"sha256": "a" * 64, "path": "audio/mixed.wav", "bytes": 1234},
            "trace": {"sha256": "b" * 64, "path": "trace/voice_trace.jsonl", "bytes": 56},
        },
        "scenario_digest": "c" * 64,
    }


def _transcript_dict():
    return {"text": "hello there how can I help",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hello there"}],
            "model": "base.en", "language": "en"}


def _trace():
    return {"schema": "hotato.voice_trace.v1",
            "spans": [{"type": "tool_call", "start_sec": 1.1, "end_sec": 1.4,
                       "name": "lookup_order", "latency_ms": 320}]}


# --- dimension propagates from assertion -> result --------------------------

def test_dimension_propagates_onto_result_and_is_schema_valid():
    env = _dim_envelope()  # _validate() inside already checks the schema enum
    by_id = {r["id"]: r for r in env["results"]}
    assert by_id["disclosure"]["dimension"] == "policy"
    assert by_id["no-ssn"]["dimension"] == "policy"
    assert by_id["refunded"]["dimension"] == "outcome"
    assert by_id["empathy"]["dimension"] == "conversation"
    # an untagged assertion's result carries NO dimension (additive, not defaulted)
    assert "dimension" not in by_id["untagged"]


# --- scorecard groups into the five dimensions ------------------------------

def test_scorecard_groups_into_five_dimensions_html():
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_dim_envelope())
    assert '<div class="scorecard">' in html
    # every one of the five dimension names is present, in title case
    for name in ("Outcome", "Policy", "Conversation", "Speech", "Reliability"):
        assert f'<span class="scname">{name}</span>' in html
    # each dimension keeps its OWN counts: Policy has one PASS + one FAIL
    assert "1 pass / 1 fail / 0 inconclusive" in html   # policy
    # the typed cards still render inside the scorecard (grouped, not replaced)
    assert '<span class="kindtag mono">phrase</span>' in html
    assert '<span class="kindtag mono">pii</span>' in html


def test_scorecard_dimension_counts_are_never_blended():
    """Each dimension shows its own pass/fail/inconclusive counts; there is no
    single combined score across dimensions or within one."""
    env = _dim_envelope()
    md, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env)
    # outcome: refunded PASS -> its own 1/0/0
    assert "#### Outcome" in md
    # policy: disclosure PASS + no-ssn FAIL -> its own 1/1/0
    assert "#### Policy" in md
    assert "1 pass / 1 fail / 0 inconclusive" in md
    # the note states, in words, that dimensions are never blended
    assert "no blended or overall number across dimensions or within one" in md


def test_no_overall_score_field_anywhere_in_scorecard():
    env = _dim_envelope()
    html, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    md, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env)
    # the prose note documents "no overall_score"; what must never appear is the
    # FIELD itself (quoted as JSON would render it) or any blended score field.
    assert '"overall_score"' not in html and '"score"' not in html
    assert '"overall_score"' not in md and '"score"' not in md
    # and the machine envelope carries no such field
    assert "overall_score" not in env["summary"]


# --- untagged -> Ungrouped bucket (never dropped) ---------------------------

def test_untagged_result_goes_to_ungrouped_bucket_html():
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_dim_envelope())
    assert '<span class="scname">Ungrouped (no dimension tag)</span>' in html
    # the untagged assertion's id is present -- it is grouped, never dropped
    assert "untagged" in html


def test_ungrouped_absent_when_every_result_is_tagged():
    ctx = A.build_context(spans=[_tc_span(0, "issue_refund")])
    doc = {"version": 1, "assertions": [
        {"id": "refunded", "kind": "tool_call", "name": "issue_refund",
         "dimension": "outcome"},
    ]}
    env = A.run_assertions(doc, ctx)
    html, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    assert "Ungrouped (no dimension tag)" not in html


# --- Reliability placeholder (Phase 2, never fabricated) --------------------

def test_reliability_shows_phase2_placeholder_html():
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_dim_envelope())
    assert '<span class="scname">Reliability</span>' in html
    assert "not yet measured" in html and "Phase 2" in html
    assert "scplaceholder" in html


def test_reliability_shows_phase2_placeholder_md():
    md, _ = report.build_report_md(stereo=_bundled_wav(),
                                   assertions=_dim_envelope())
    assert "#### Reliability" in md
    assert "not yet measured" in md and "Phase 2" in md


# --- deterministic vs model-assisted shelf split stays intact ---------------

def test_quarantine_shelf_still_present_with_scorecard():
    html, _ = report.build_report_html(stereo=_bundled_wav(),
                                       assertions=_dim_envelope())
    # the scorecard replaces only the FLAT deterministic cards; the two-shelf
    # split (headline + quarantined judge shelf) is untouched.
    assert "deterministic pass" in html and "judge-scored (advisory)" in html
    assert "Model-assisted (advisory, quarantined)" in html
    assert "No judge-scored assertions in this build." in html


# --- byte-identity: no dimensions + no conversation -------------------------

def test_no_dimensions_and_no_conversation_byte_identical():
    """A report built with an assertions envelope that carries NO dimensions and
    conversation=None is byte-identical (HTML + MD) to the same call without
    those features touched -- proving the scorecard/conversation code is purely
    additive and never perturbs the pre-existing flat rendering."""
    env = _no_dim_envelope()
    a_html, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    b_html, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env,
                                         conversation=None)
    assert a_html == b_html
    # the flat deterministic shelf path is taken -- no scorecard markup or CSS
    assert "scorecard" not in a_html
    assert ".scdim{" not in a_html
    assert "Conversation artifact (provenance)" not in a_html

    a_md, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env)
    b_md, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env,
                                     conversation=None)
    assert a_md == b_md
    assert "#### Outcome" not in a_md
    assert "## Conversation artifact (provenance)" not in a_md


def test_scorecard_render_is_byte_stable():
    env = _dim_envelope()
    a, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    b, _ = report.build_report_html(stereo=_bundled_wav(), assertions=env)
    assert a == b
    ma, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env)
    mb, _ = report.build_report_md(stereo=_bundled_wav(), assertions=env)
    assert ma == mb


# --- conversation-artifact provenance (real|simulated + digests) ------------

def test_conversation_default_absent_byte_identical():
    a, _ = report.build_report_html(stereo=_bundled_wav())
    b, _ = report.build_report_html(stereo=_bundled_wav(), conversation=None)
    assert a == b
    assert "Conversation artifact (provenance)" not in a
    assert "cvtab" not in a
    ma, _ = report.build_report_md(stereo=_bundled_wav())
    mb, _ = report.build_report_md(stereo=_bundled_wav(), conversation=None)
    assert ma == mb


def test_conversation_real_origin_renders_digests():
    cv = _conv_manifest("real", provider="vapi", provider_call_id="c-9")
    html, env = report.build_report_html(stereo=_bundled_wav(), conversation=cv)
    assert "Conversation artifact (provenance)" in html
    assert '<span class="cvchip"' in html
    assert ">real<" in html
    assert "provider vapi" in html and "call c-9" in html
    # the bound digests render (name + sha256)
    assert "a" * 64 in html and "b" * 64 in html
    assert "scenario_digest" in html and "c" * 64 in html
    # folded into the envelope additively
    assert env["conversation"]["origin"]["kind"] == "real"


def test_conversation_simulated_origin_shows_simulator_block():
    cv = _conv_manifest("simulated")
    html, _ = report.build_report_html(stereo=_bundled_wav(), conversation=cv)
    assert ">simulated<" in html
    # a simulated origin declares its model/scenario/seed -- synthetic is never
    # conflated with real
    assert "simulator: model gpt-sim-x" in html
    assert "scenario refund-1" in html and "seed 7" in html


def test_conversation_bad_type_raises_value_error():
    with pytest.raises(ValueError, match="conversation"):
        report.build_report_html(stereo=_bundled_wav(), conversation="nope")


def test_conversation_missing_origin_kind_raises():
    bad = _conv_manifest("real")
    bad["origin"] = {"provider": "vapi"}  # no kind
    with pytest.raises(ValueError, match="origin.kind"):
        report.build_report_html(stereo=_bundled_wav(), conversation=bad)


def test_conversation_never_changes_measurements():
    cv = _conv_manifest("simulated")
    _, plain = report.build_report_html(stereo=_bundled_wav())
    _, withcv = report.build_report_html(stereo=_bundled_wav(), conversation=cv)
    assert plain["summary"] == withcv["summary"]
    assert plain["events"][0]["verdict"] == withcv["events"][0]["verdict"]
    assert "conversation" not in plain


def test_conversation_md_renders_origin_and_digests():
    cv = _conv_manifest("simulated")
    md, _ = report.build_report_md(stereo=_bundled_wav(), conversation=cv)
    assert "## Conversation artifact (provenance)" in md
    assert "- origin: simulated" in md
    assert "simulator: model gpt-sim-x" in md
    assert "a" * 64 in md  # a bound digest


def test_conversation_from_real_build_manifest_renders(tmp_path):
    """A conversation manifest built the real way (CV.build_manifest, binding
    children by sha256) renders its origin + digests -- not just a hand-built
    dict."""
    audio = tmp_path / "mixed.wav"
    audio.write_bytes(b"RIFFsynthetic-pcm")
    manifest = CV.build_manifest(
        conversation_id="conv-real-1", agent_id="support-v3",
        origin={"kind": "real", "provider": "vapi"},
        created_at="2026-07-12T00:00:00Z",
        artifact_files={"audio": str(audio)}, base_dir=str(tmp_path),
    )
    html, env = report.build_report_html(stereo=_bundled_wav(), conversation=manifest)
    assert "Conversation artifact (provenance)" in html
    assert manifest["artifacts"]["audio"]["sha256"] in html
    assert env["conversation"]["origin"]["kind"] == "real"


# --- unified report: timing + transcript + trace + assertions(scorecard) + conversation ---

def test_unified_report_shows_all_sections_plus_scorecard():
    """A single report unifying the base timing report PLUS transcript PLUS trace
    PLUS assertions-with-dimensions (the scorecard) PLUS a conversation manifest
    renders every section as distinct, clearly-labelled content."""
    cv = _conv_manifest("simulated")
    html, env = report.build_report_html(
        stereo=_bundled_wav(),
        transcript=_transcript_dict(),
        trace=_trace(),
        assertions=_dim_envelope(),
        conversation=cv,
    )
    # 1) base timing report
    assert "time to yield" in html and "Thresholds used" in html
    # 2) transcript context
    assert "Transcript (context, not a score)" in html
    # 3) trace context
    assert "Trace (context, not a score)" in html
    # 4) assertions, grouped into the per-dimension scorecard
    assert "deterministic pass" in html and '<div class="scorecard">' in html
    assert '<span class="scname">Reliability</span>' in html
    # 5) conversation provenance
    assert "Conversation artifact (provenance)" in html and ">simulated<" in html
    # the envelope carries all the additive context keys
    assert env["events"][0]["transcript_context"]["text"] == "hello there how can I help"
    assert env["trace_context"]["meta"]["schema"] == "hotato.voice_trace.v1"
    assert env["conversation"]["origin"]["kind"] == "simulated"
    # honesty: no blended score field anywhere on the unified page
    assert '"overall_score"' not in html

    md, _ = report.build_report_md(
        stereo=_bundled_wav(),
        transcript=_transcript_dict(),
        trace=_trace(),
        assertions=_dim_envelope(),
        conversation=cv,
    )
    assert "## Conversation artifact (provenance)" in md
    assert "## Transcripts (context, not a score)" in md
    assert "## Trace (context, not a score)" in md
    assert "## Assertions" in md and "#### Reliability" in md
