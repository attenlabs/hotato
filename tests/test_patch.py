"""hotato patch: a fix plan -> a literal, paste-ready per-platform artifact.

Covers the two load-bearing behaviours from the spec:

* a config-fixable plan (propose_one_step) emits the correct per-platform merge
  patch + curl, with the EXACT field names carried by the knob catalogue (Vapi
  and the source-edit stacks are cross-checked against fixmap's own catalogue);
* a do_not_tune_single_threshold plan emits the vendor-neutral, numbers-free
  engagement-control pointer and NO config patch.

Plus the honesty invariants: patch never applies a change, never emits a fake
number, and refuses a non-plan input as a clean usage error.
"""

from __future__ import annotations

import json

import pytest

from hotato import cli
from hotato import fixmap as _fixmap
from hotato import fixplan as _fixplan
from hotato import patch as _patch


def _diagnosis(finding: str, *, coverage_ok: bool = True) -> dict:
    """A minimal diagnosis that drives fixplan.build_plan to propose_one_step
    for ``finding`` when the opposite-risk coverage gate is satisfied."""
    from hotato.diagnose import OPPOSITE_RISK

    coverage = {}
    key = OPPOSITE_RISK.get(finding, {}).get("coverage_key")
    if key:
        coverage[key] = coverage_ok
    return {
        "battery": {"finding": None, "failed": 1, "events": 2,
                    "opposite_risk_coverage": coverage},
        "diagnoses": [
            {"finding": finding, "config_only_safe": True, "event_id": "e1",
             "scenario_id": "s1", "notes": "measured note.", "evidence": {}},
        ],
    }


def _config_plan(stack, current, source, target_info):
    inspected = {"stack": stack,
                 "turn_taking": {source: current, "raw": {source: current}}}
    return _fixplan.build_plan(
        diagnosis=_diagnosis("missed_real_interruption"),
        inspected=inspected, stack=stack, target_info=target_info,
    )


# --- config-fixable: Vapi REST merge-patch + curl ---------------------------

def test_vapi_config_plan_emits_merge_patch_curl_with_exact_field():
    plan = _config_plan("vapi", 3, "interrupt_min_words",
                        {"assistant_id": "asst_9"})
    assert plan["decision"] == "propose_one_step"
    to = plan["changes"][0]["to"]
    assert plan["changes"][0]["field"] == "stopSpeakingPlan.numWords"

    p = _patch.build_patch(plan, source="fixplan.json")
    assert p["config_patchable"] is True
    assert p["applies_change"] is False
    art = p["artifact"]
    assert art["apply_method"] == "rest-merge-patch"
    # exact merge-patch: the dotted field nests into the real Vapi request body
    assert art["merge_patch"] == {"stopSpeakingPlan": {"numWords": to}}
    # the field name is a real Vapi knob from fixmap's catalogue
    fixmap_params = " ".join(
        k["parameter"] for k in _fixmap._KNOBS["vapi"].values()
    )
    assert "stopSpeakingPlan.numWords" in fixmap_params
    # the curl hits the verified update endpoint with the id resolved
    assert art["endpoint"]["method"] == "PATCH"
    assert art["endpoint"]["url"] == "https://api.vapi.ai/assistant/asst_9"
    assert art["endpoint"]["id_resolved"] is True
    assert "curl -X PATCH https://api.vapi.ai/assistant/asst_9" in art["curl"]
    assert "Authorization: Bearer $VAPI_API_KEY" in art["curl"]
    assert json.dumps(art["merge_patch"], sort_keys=True) in art["curl"]


def test_vapi_without_inspected_id_uses_placeholder_not_a_fake_id():
    plan = _config_plan("vapi", 3, "interrupt_min_words", {})
    p = _patch.build_patch(plan)
    assert p["artifact"]["endpoint"]["id_resolved"] is False
    assert "<assistant-id>" in p["artifact"]["endpoint"]["url"]


# --- config-fixable: Retell REST merge-patch --------------------------------

def test_retell_config_plan_emits_flat_merge_patch_and_update_agent_endpoint():
    plan = _config_plan("retell", 0.5, "interruption_sensitivity",
                        {"agent_id": "ag_1"})
    # retell reads the raw scale
    assert plan["changes"][0]["field"] == "interruption_sensitivity"
    to = plan["changes"][0]["to"]
    p = _patch.build_patch(plan)
    art = p["artifact"]
    assert art["merge_patch"] == {"interruption_sensitivity": to}
    assert art["endpoint"]["url"] == "https://api.retellai.com/update-agent/ag_1"
    assert "update-agent" in art["endpoint"]["provenance"]


# --- config-fixable: LiveKit / Pipecat are source edits, never a curl -------

def test_livekit_config_plan_emits_source_edit_not_a_curl():
    plan = _config_plan("livekit", 2, "interrupt_min_words",
                        {"config_path": "agent.py"})
    assert plan["changes"][0]["field"] == "turn_handling.interruption.min_words"
    to = plan["changes"][0]["to"]
    p = _patch.build_patch(plan)
    art = p["artifact"]
    assert art["apply_method"] == "source-edit"
    assert art["curl"] is None
    assert art["endpoint"] is None
    assert art["source_edit"]["constructor"] == "InterruptionOptions"
    assert art["source_edit"]["kwarg"] == "min_words"
    assert art["source_edit"]["value"] == to
    assert art["source_edit"]["snippet"] == f"InterruptionOptions(min_words={to})"
    # the constructor/kwarg is a real LiveKit knob in fixmap's catalogue text
    fixmap_params = " ".join(
        k["parameter"] for k in _fixmap._KNOBS["livekit"].values()
    )
    assert "min_words" in fixmap_params


def test_pipecat_config_plan_source_edit_splits_constructor_kwarg():
    plan = _config_plan("pipecat", 3, "interrupt_min_words",
                        {"config_path": "bot.py"})
    assert plan["changes"][0]["field"] == "MinWordsUserTurnStartStrategy.min_words"
    p = _patch.build_patch(plan)
    se = p["artifact"]["source_edit"]
    assert se["constructor"] == "MinWordsUserTurnStartStrategy"
    assert se["kwarg"] == "min_words"


# --- generic: names the knob family, no fabricated body ---------------------

def test_generic_plan_names_family_and_emits_no_literal_body():
    plan = _fixplan.build_plan(
        diagnosis=_diagnosis("missed_real_interruption"),
        inspected=None, stack="generic",
    )
    p = _patch.build_patch(plan)
    assert p["config_patchable"] is True
    art = p["artifact"]
    assert art["apply_method"] == "none"
    assert art["merge_patch"] is None
    assert art["curl"] is None
    assert "generic knob family" in art["note"]


# --- the both-axes case: the SAA pointer, and NO config patch ---------------

def test_threshold_funnel_plan_emits_engagement_pointer_and_no_config_patch():
    diag = {"battery": {"finding": "threshold_funnel", "failed": 2, "events": 3,
                        "opposite_risk_coverage": {}}, "diagnoses": []}
    plan = _fixplan.build_plan(diagnosis=diag, inspected=None, stack="vapi")
    assert plan["decision"] == "do_not_tune_single_threshold"

    p = _patch.build_patch(plan, source="fixplan.json")
    # NO config patch
    assert p["config_patchable"] is False
    assert p["change"] is None
    assert p["artifact"] is None
    # the vendor-neutral engagement-control pointer, with no numbers and no
    # product name
    ptr = p["saa_pointer"]
    assert ptr is not None
    assert ptr["class"] == "engagement-control"
    assert ptr["examples"]
    blob = json.dumps(ptr)
    assert not any(ch.isdigit() for ch in blob), "pointer must carry no numbers"
    for banned in ("saa", "hotato", "attention labs", "vapi", "retell"):
        assert banned not in blob.lower(), f"pointer names a product: {banned}"


def test_engagement_pointer_only_fires_on_the_real_both_axes_case():
    # A plain diagnostic_checklist plan (ambiguous slow yield) is NOT the
    # both-axes case: patch must NOT show the engagement-control pointer there.
    diag = {
        "battery": {"finding": None, "failed": 1, "events": 1,
                    "opposite_risk_coverage": {}},
        "diagnoses": [
            {"finding": "slow_yield", "config_only_safe": False,
             "event_id": "e1", "scenario_id": "s1",
             "notes": "layer unclear.", "evidence": {}},
        ],
    }
    plan = _fixplan.build_plan(diagnosis=diag, inspected=None, stack="vapi")
    assert plan["decision"] == "diagnostic_checklist"
    p = _patch.build_patch(plan)
    assert p["saa_pointer"] is None
    assert p["config_patchable"] is False
    assert "checklist" in p["reason"].lower()


# --- current-value-unknown: direction only, never a fake literal ------------

def test_current_unknown_emits_no_literal_value():
    # A vapi target with no inspected value -> propose_one_step with to=None.
    plan = _fixplan.build_plan(
        diagnosis=_diagnosis("missed_real_interruption"),
        inspected=None, stack="vapi",
        target_info={"assistant_id": "asst_1"},
    )
    assert plan["changes"][0]["to"] is None
    p = _patch.build_patch(plan)
    art = p["artifact"]
    assert art["merge_patch"] is None
    assert art["curl"] is None
    assert "no concrete target value" in art["note"].lower()


# --- honesty + usage errors -------------------------------------------------

def test_patch_never_applies_the_change():
    plan = _config_plan("vapi", 3, "interrupt_min_words", {"assistant_id": "a"})
    p = _patch.build_patch(plan)
    assert p["applies_change"] is False
    assert "never applies" in p["honest"].lower()


def test_non_plan_input_is_a_usage_error():
    with pytest.raises(ValueError):
        _patch.build_patch({"tool": "hotato", "events": []})


def test_cli_patch_text_and_json(tmp_path, capsys):
    plan = _config_plan("vapi", 3, "interrupt_min_words", {"assistant_id": "a9"})
    plan_path = tmp_path / "fixplan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    code = cli.main(["patch", str(plan_path)])
    assert code == 0
    text = capsys.readouterr().out
    assert "hotato patch [vapi]" in text
    assert "curl -X PATCH" in text

    out = tmp_path / "patch.json"
    code = cli.main(["patch", str(plan_path), "--format", "json",
                     "--out", str(out)])
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["kind"] == "patch"
    assert doc["artifact"]["merge_patch"] == {"stopSpeakingPlan": {"numWords": 2}}


def test_cli_patch_on_non_plan_exits_2(tmp_path):
    bad = tmp_path / "notplan.json"
    bad.write_text(json.dumps({"tool": "hotato", "events": []}), encoding="utf-8")
    assert cli.main(["patch", str(bad)]) == 2


def test_cli_patch_missing_file_exits_2(tmp_path):
    assert cli.main(["patch", str(tmp_path / "nope.json")]) == 2


def test_patch_rejects_non_finite_to_value(tmp_path, capsys):
    """Regression: a NaN/Infinity numeric in a schema-valid plan must be a clean
    exit-2 usage error -- never flow into the paste-ready merge-patch / curl body
    as an RFC-8259-invalid bare token. Python's json.load parses NaN, so the plan
    file loads; _validate_plan is the gate."""
    plan = tmp_path / "nan_plan.json"
    # Written literally (json.dumps(allow_nan=True) would refuse) so the file
    # carries the bare NaN token exactly as a hand-edit/other tool might.
    plan.write_text(
        '{"schema":"hotato.fixplan.v1","kind":"fix-plan",'
        '"target":{"stack":"vapi","inspected":false},'
        '"decision":"propose_one_step","changes":[{'
        '"field":"stopSpeakingPlan.voiceSeconds","direction":"decrease",'
        '"from":0.2,"to":NaN,"bounds":[0,0.5]}]}',
        encoding="utf-8",
    )
    assert cli.main(["patch", str(plan)]) == 2
    # and --format json emits VALID json (no bare NaN token) for the error
    capsys.readouterr()
    assert cli.main(["patch", str(plan), "--format", "json"]) == 2
    out = capsys.readouterr().out
    doc = json.loads(out)  # strict: raises if a bare NaN leaked through
    assert doc["ok"] is False
    assert doc["exit_code"] == 2
    assert "finite" in doc["message"]


def test_patch_rejects_non_finite_inside_bounds(tmp_path):
    """A non-finite value nested inside bounds is rejected too."""
    plan = tmp_path / "inf_bounds.json"
    plan.write_text(
        '{"schema":"hotato.fixplan.v1","kind":"fix-plan",'
        '"target":{"stack":"vapi","inspected":false},'
        '"decision":"propose_one_step","changes":[{'
        '"field":"stopSpeakingPlan.voiceSeconds","direction":"decrease",'
        '"from":0.2,"to":0.1,"bounds":[0,Infinity]}]}',
        encoding="utf-8",
    )
    assert cli.main(["patch", str(plan)]) == 2


def test_safe_json_dumps_refuses_nan():
    """The shared emitter forces allow_nan=False and raises a finite-number
    usage error instead of shipping the bare NaN/Infinity token."""
    from hotato import errors as _errors

    # a normal, finite payload round-trips unchanged
    assert _errors.safe_json_dumps({"a": 1.5}) == json.dumps({"a": 1.5})
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError) as ei:
            _errors.safe_json_dumps({"x": bad})
        assert "finite" in str(ei.value)
