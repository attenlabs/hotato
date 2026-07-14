"""hotato apply: the guarded, CLONE-ONLY staged apply.

This is the one command that can mutate external platform state, so the tests
pin every safety rule the spec makes structural:

* CLONE-ONLY: a non---clone invocation is a clean usage error; there is no
  production-apply path.
* REFUSAL-FIRST: a both-axes threshold-funnel patch is REFUSED before anything,
  with the EXACT canon refusal text and a distinct, documented exit code.
* OPPOSITE-RISK REQUIRED: apply refuses unless the battery carries BOTH a yield
  and a hold fixture.
* GATED SIDE EFFECT: the default dry run prints the clone it WOULD create and
  touches NO network; only --yes reaches the platform, and the create reads the
  source (GET) then creates a NEW assistant (POST) -- it never mutates the
  source.
* NAME REQUIRED.
"""

from __future__ import annotations

import json

import pytest

from hotato import apply as _apply
from hotato import cli
from hotato import fixplan as _fixplan
from hotato import patch as _patch
from hotato.diagnose import OPPOSITE_RISK

# --- fixtures: real patches, and an opposite-risk battery on disk ------------

def _diagnosis(finding: str) -> dict:
    coverage = {}
    key = OPPOSITE_RISK.get(finding, {}).get("coverage_key")
    if key:
        coverage[key] = True
    return {
        "battery": {"finding": None, "failed": 1, "events": 2,
                    "opposite_risk_coverage": coverage},
        "diagnoses": [
            {"finding": finding, "config_only_safe": True, "event_id": "e1",
             "scenario_id": "s1", "notes": "measured note.", "evidence": {}},
        ],
    }


def _config_plan(stack="vapi", current=3, source="interrupt_min_words",
                 target_info=None):
    inspected = {"stack": stack,
                 "turn_taking": {source: current, "raw": {source: current}}}
    return _fixplan.build_plan(
        diagnosis=_diagnosis("missed_real_interruption"),
        inspected=inspected, stack=stack,
        target_info=target_info if target_info is not None
        else {"assistant_id": "asst_9"},
    )


def _write_patch(tmp_path, plan, *, plan_name="fixplan.json",
                 patch_name="patch.json"):
    """Write the plan and its patch to disk exactly as the CLI would, so the
    apply command reads a real patch.json (and can resolve the referenced plan)."""
    plan_path = tmp_path / plan_name
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    p = _patch.build_patch(plan, source=plan_name)
    patch_path = tmp_path / patch_name
    patch_path.write_text(json.dumps(p), encoding="utf-8")
    return patch_path


@pytest.fixture()
def config_patch(tmp_path):
    return _write_patch(tmp_path, _config_plan())


@pytest.fixture()
def funnel_patch(tmp_path):
    diag = {"battery": {"finding": "threshold_funnel", "failed": 2, "events": 3,
                        "opposite_risk_coverage": {}}, "diagnoses": []}
    plan = _fixplan.build_plan(diagnosis=diag, inspected=None, stack="vapi")
    return _write_patch(tmp_path, plan)


@pytest.fixture()
def battery(tmp_path):
    """A directory with BOTH a yield and a hold fixture (two scenario JSONs)."""
    d = tmp_path / "battery"
    d.mkdir()
    (d / "yield.json").write_text(
        json.dumps({"id": "y1", "expected": {"yield": True}}), encoding="utf-8")
    (d / "hold.json").write_text(
        json.dumps({"id": "h1", "expected": {"yield": False}}), encoding="utf-8")
    return d


# --- CLONE-ONLY: no production-apply path ------------------------------------

def test_non_clone_invocation_is_a_clean_usage_error(config_patch, battery, capsys):
    rc = cli.main([
        "apply", str(config_patch), "--name", "staging-x", "--battery",
        str(battery),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "production apply is not supported" in err
    assert "--clone" in err


def test_build_apply_refuses_without_clone():
    with pytest.raises(ValueError) as exc:
        _apply.build_apply({"tool": "hotato", "kind": "patch", "stack": "vapi"},
                           name="x", clone=False, battery_dir=None)
    assert "production apply is not supported" in str(exc.value)


# --- REFUSAL-FIRST: the both-axes threshold funnel ---------------------------

def test_threshold_funnel_patch_triggers_the_exact_refusal(
        funnel_patch, battery, capsys):
    rc = cli.main([
        "apply", str(funnel_patch), "--clone", "--name", "staging-x",
        "--battery", str(battery),
    ])
    # the documented, distinct refusal code -- NOT a usage error, NOT success
    assert rc == 3
    assert rc == _apply.REFUSAL_EXIT_CODE
    out = capsys.readouterr().out
    # the exact canon refusal, verbatim
    assert "No config patch will be applied" in out
    assert ("Reason: both missed real interruption and false stop on "
            "backchannel, one threshold cannot safely fix both") in out
    assert ("Recommended: enable or add engagement-control / backchannel-aware "
            "turn detection") in out


def test_threshold_funnel_refuses_before_battery_or_name(funnel_patch, capsys):
    # REFUSAL-FIRST: even with NO --name and NO --battery, the funnel patch
    # refuses (it never reaches those gates), and still exits the refusal code.
    rc = cli.main(["apply", str(funnel_patch), "--clone"])
    assert rc == _apply.REFUSAL_EXIT_CODE
    assert "No config patch will be applied" in capsys.readouterr().out


def test_threshold_funnel_refusal_json_shape(funnel_patch, battery, capsys):
    rc = cli.main([
        "apply", str(funnel_patch), "--clone", "--name", "x", "--battery",
        str(battery), "--format", "json",
    ])
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "apply"
    assert payload["refused"] is True
    assert payload["applies_change"] is False
    assert payload["created"] is False
    assert payload["exit_code"] == 3
    assert payload["refusal"]["lines"] == list(_apply.REFUSAL_LINES)
    # vendor-neutral: no product name, no digits in the recommendation
    blob = json.dumps(payload["refusal"])
    for banned in ("saa", "vapi", "retell", "hotato patch"):
        assert banned not in blob.lower()


# --- OPPOSITE-RISK REQUIRED --------------------------------------------------

def test_missing_battery_refuses(config_patch, capsys):
    rc = cli.main([
        "apply", str(config_patch), "--clone", "--name", "staging-x",
    ])
    assert rc == 2
    assert "opposite-risk battery" in capsys.readouterr().err


def test_one_sided_battery_is_refused(config_patch, tmp_path, capsys):
    # A battery with only a yield fixture (no hold) cannot catch the opposite
    # risk, so applying would be blind: refused.
    d = tmp_path / "one-sided"
    d.mkdir()
    (d / "yield.json").write_text(
        json.dumps({"id": "y", "expected": {"yield": True}}), encoding="utf-8")
    rc = cli.main([
        "apply", str(config_patch), "--clone", "--name", "x", "--battery",
        str(d),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "hold fixture" in err


def test_battery_classes_reads_both_labels(battery):
    classes = _apply.battery_classes(str(battery))
    assert classes["has_yield"] is True
    assert classes["has_hold"] is True
    assert classes["n"] == 2


# --- NAME REQUIRED -----------------------------------------------------------

def test_name_is_required(config_patch, battery, capsys):
    rc = cli.main([
        "apply", str(config_patch), "--clone", "--battery", str(battery),
    ])
    assert rc == 2
    assert "--name" in capsys.readouterr().err


# --- GATED SIDE EFFECT: dry run touches no network ---------------------------

def test_dry_run_prints_the_clone_and_touches_no_network(
        config_patch, battery, capsys, monkeypatch):
    # Tripwire: the sole network primitive must never be called on the dry-run
    # path (nor create_clone).
    def boom(*a, **k):
        raise AssertionError("apply hit the network on the dry-run path")

    monkeypatch.setattr(_apply, "_http_json", boom)
    monkeypatch.setattr(_apply, "create_clone", boom)

    rc = cli.main([
        "apply", str(config_patch), "--clone", "--name", "staging-refund-fix",
        "--battery", str(battery),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would create: POST https://api.vapi.ai/assistant" in out
    assert "staging-refund-fix" in out
    # the patch it WOULD apply is shown; nothing was created
    assert '"stopSpeakingPlan": {"numWords": 2}' in out
    assert "dry run: nothing was created" in out
    assert "hotato verify" in out


def test_dry_run_json_shape(config_patch, battery, capsys):
    rc = cli.main([
        "apply", str(config_patch), "--clone", "--name", "staging-x",
        "--battery", str(battery), "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "apply"
    assert payload["dry_run"] is True
    assert payload["created"] is False
    assert payload["applies_change"] is False
    assert payload["clone_only"] is True
    assert payload["production_apply_supported"] is False
    assert payload["clone"]["name"] == "staging-x"
    assert payload["clone"]["based_on_source_id"] == "asst_9"
    assert payload["clone"]["create"]["method"] == "POST"
    assert payload["clone"]["merge_patch"] == {"stopSpeakingPlan": {"numWords": 2}}
    assert payload["opposite_risk"]["has_yield"] is True
    assert payload["opposite_risk"]["has_hold"] is True


# --- the pure clone payload: source + patch, source NEVER mutated ------------

def test_apply_patch_to_config_merges_without_mutating_source():
    source = {"name": "prod", "stopSpeakingPlan": {"numWords": 3,
                                                   "voiceSeconds": 0.2}}
    original = json.loads(json.dumps(source))  # a deep snapshot
    merged = _apply.apply_patch_to_config(source, {"stopSpeakingPlan":
                                                   {"numWords": 2}})
    # the patch deep-merges (voiceSeconds preserved, numWords replaced)
    assert merged["stopSpeakingPlan"] == {"numWords": 2, "voiceSeconds": 0.2}
    assert merged["name"] == "prod"
    # the source object is byte-for-byte unchanged: never mutated in place
    assert source == original


def test_build_clone_config_is_source_plus_patch_named_and_source_safe():
    source = {"id": "asst_9", "orgId": "org_1", "name": "prod",
              "stopSpeakingPlan": {"numWords": 3, "voiceSeconds": 0.2}}
    original = json.loads(json.dumps(source))
    clone = _apply.build_clone_config(
        source, stack="vapi", name="staging-x",
        merge_patch={"stopSpeakingPlan": {"numWords": 2}})
    # source + patch
    assert clone["stopSpeakingPlan"] == {"numWords": 2, "voiceSeconds": 0.2}
    # the NEW name is set; the server-assigned ids are dropped so it is a fresh
    # object, never an overwrite of the source
    assert clone["name"] == "staging-x"
    assert "id" not in clone and "orgId" not in clone
    # the source is untouched
    assert source == original


# --- --yes: the one networked path reads the source, creates a NEW assistant -

def test_yes_reads_source_then_posts_a_new_assistant_never_mutating_source(
        config_patch, battery, capsys, monkeypatch):
    calls = []
    source_config = {"id": "asst_9", "name": "prod",
                     "stopSpeakingPlan": {"numWords": 3, "voiceSeconds": 0.2}}
    source_snapshot = json.loads(json.dumps(source_config))

    def fake_http(method, url, *, headers, body, timeout):
        calls.append((method, url, body))
        if method == "GET":
            return source_config
        # the create call: a NEW assistant id comes back
        return {"id": "asst_CLONE"}

    monkeypatch.setattr(_apply, "_http_json", fake_http)
    rc = cli.main([
        "apply", str(config_patch), "--clone", "--name", "staging-x",
        "--battery", str(battery), "--yes", "--api-key", "sk_test",
    ])
    assert rc == 0
    # exactly one GET (read source) then one POST (create the clone)
    methods = [c[0] for c in calls]
    assert methods == ["GET", "POST"]
    # never a PATCH/PUT against the source
    assert "PATCH" not in methods and "PUT" not in methods
    get_url = calls[0][1]
    post_url, post_body = calls[1][1], calls[1][2]
    assert get_url == "https://api.vapi.ai/assistant/asst_9"
    assert post_url == "https://api.vapi.ai/assistant"
    # the POSTed clone is source + patch, renamed, with the source id dropped
    assert post_body["stopSpeakingPlan"] == {"numWords": 2, "voiceSeconds": 0.2}
    assert post_body["name"] == "staging-x"
    assert "id" not in post_body
    # the fetched source object was not mutated
    assert source_config == source_snapshot
    out = capsys.readouterr().out
    assert "CREATED staging assistant 'asst_CLONE'" in out


def test_http_json_refuses_a_mutating_method():
    # The one HTTP primitive refuses PUT/PATCH by construction, so the create
    # path can never overwrite the source even if mis-called.
    with pytest.raises(ValueError) as exc:
        _apply._http_json("PATCH", "https://api.vapi.ai/assistant/asst_9",
                          headers={}, body={}, timeout=1)
    assert "refusing PATCH" in str(exc.value)


# --- stacks with no assistant to clone ---------------------------------------

def test_livekit_patch_has_no_platform_clone(tmp_path, battery, capsys):
    plan = _config_plan("livekit", 2, "interrupt_min_words",
                        {"config_path": "agent.py"})
    patch_path = _write_patch(tmp_path, plan)
    rc = cli.main([
        "apply", str(patch_path), "--clone", "--name", "x", "--battery",
        str(battery),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "agent SOURCE" in err or "no platform assistant" in err.lower()


def test_patch_with_no_concrete_value_is_refused(tmp_path, battery, capsys):
    # A vapi target with no inspected value -> to=None -> no merge_patch: apply
    # refuses rather than apply blind.
    plan = _fixplan.build_plan(
        diagnosis=_diagnosis("missed_real_interruption"),
        inspected=None, stack="vapi", target_info={"assistant_id": "asst_1"})
    patch_path = _write_patch(tmp_path, plan)
    rc = cli.main([
        "apply", str(patch_path), "--clone", "--name", "x", "--battery",
        str(battery),
    ])
    assert rc == 2
    assert "no concrete value" in capsys.readouterr().err


# --- input hygiene -----------------------------------------------------------

def test_non_patch_input_is_a_usage_error(tmp_path, battery, capsys):
    bad = tmp_path / "notpatch.json"
    bad.write_text(json.dumps({"tool": "hotato", "events": []}), encoding="utf-8")
    rc = cli.main([
        "apply", str(bad), "--clone", "--name", "x", "--battery", str(battery),
    ])
    assert rc == 2
    assert "not a hotato patch" in capsys.readouterr().err


def test_missing_patch_file_exits_2(tmp_path, battery):
    rc = cli.main([
        "apply", str(tmp_path / "nope.json"), "--clone", "--name", "x",
        "--battery", str(battery),
    ])
    assert rc == 2
