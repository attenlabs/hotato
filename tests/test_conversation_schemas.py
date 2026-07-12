"""Phase-1 anchor schemas: conversation-test.v1 + conversation.v1 (+ suite/release).

Pins the honesty properties that are the point of this slice: a well-formed
conversation-test validates against both the Python validator and the JSON
Schema; every malformed variant raises ``ValueError`` up front (never a partial
result); a conversation manifest binds its children by sha256 and ``verify``
re-hashes them, PASSING when intact and REFUSING (never silently accepting) when
a child is tampered; ``origin.kind`` is required and a simulated origin carries
its simulator block; and NONE of the four new schemas permit an
``overall_score`` -- structurally, not by convention.
"""

import copy
import json
from importlib import resources

import pytest

from hotato import conversation as CV
from hotato import conversation_test as CT

jsonschema = pytest.importorskip("jsonschema")


def _schema(name):
    return json.loads(
        resources.files("hotato").joinpath("schema", name).read_text(encoding="utf-8")
    )


def _validate_json(instance, schema_name):
    jsonschema.validate(instance=instance, schema=_schema(schema_name))


# --------------------------------------------------------------------------
# valid documents
# --------------------------------------------------------------------------

def _valid_test_doc():
    return {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "refund-happy-path",
        "agent": "support-agent-v3",
        "scenario": "scenarios/refund.yaml",
        "caller": {
            "persona": "frustrated customer",
            "goal": "get a refund",
            "facts": {"order_id": "A-100", "amount": 42},
            "behavior": {
                "interrupt_at": ["greeting"],
                "correction": {"field": "order_id", "from": "A-100", "to": "A-200"},
                "speaking_rate": 1.1,
                "backchannels": {"rate": "low"},
            },
        },
        "environment": {"route": "pstn", "locale": "en-US", "codec": "g711", "noise_profile": "cafe"},
        "assertions": {
            "deterministic": [
                {"id": "refund-issued", "kind": "tool_call", "dimension": "outcome"},
                {"id": "no-ssn", "kind": "pii", "dimension": "policy"},
            ],
            "rubric": [
                {"id": "empathy", "kind": "judge_rubric", "dimension": "conversation"},
            ],
        },
        "repetitions": 3,
        "inconclusive_policy": "fail",
        "success": {
            "required": ["all_deterministic_assertions_pass", "no_rubric_failure"],
            "report_dimensions": ["outcome", "policy", "conversation"],
        },
    }


def test_valid_conversation_test_validates_both_ways():
    doc = _valid_test_doc()
    norm = CT.validate_conversation_test_doc(doc)
    assert norm["repetitions"] == 3
    assert norm["inconclusive_policy"] == "fail"
    _validate_json(doc, "conversation-test.v1.json")


def test_defaults_applied_when_absent():
    doc = {
        "kind": "hotato.conversation-test",
        "version": 1,
        "id": "minimal",
        "agent": "a1",
        "assertions": {"deterministic": []},
    }
    norm = CT.validate_conversation_test_doc(doc)
    assert norm["repetitions"] == 1
    assert norm["inconclusive_policy"] == "report"
    # an absent success defaults to the safe boolean condition, never a score
    assert norm["success"]["required"] == ["all_deterministic_assertions_pass"]
    assert "overall_score" not in norm["success"]
    _validate_json(doc, "conversation-test.v1.json")


# --------------------------------------------------------------------------
# malformed conversation-test docs -> ValueError (each variant)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("id"), "id"),
    (lambda d: d.pop("agent"), "agent"),
    (lambda d: d.pop("assertions"), "assertions"),
    (lambda d: d.__setitem__("kind", "hotato.wrong"), "kind"),
    (lambda d: d.__setitem__("version", 2), "version"),
    (lambda d: d.__setitem__("inconclusive_policy", "bogus"), "inconclusive_policy"),
    (lambda d: d.__setitem__("repetitions", 0), "repetitions"),
    (lambda d: d["success"].__setitem__("required", ["make_it_green"]), "closed vocabulary"),
    (lambda d: d["success"].__setitem__("report_dimensions", ["vibes"]), "dimension"),
    (lambda d: d["assertions"]["deterministic"][0].__setitem__("dimension", "vibes"), "dimension"),
    (lambda d: d["assertions"]["deterministic"][0].pop("id"), "id"),
    (lambda d: d.__setitem__("overall_score", 0.9), "overall_score"),
    (lambda d: d["success"].__setitem__("overall_score", 0.9), "overall_score"),
])
def test_malformed_conversation_test_raises(mutate, frag):
    doc = _valid_test_doc()
    mutate(doc)
    with pytest.raises(ValueError) as exc:
        CT.validate_conversation_test_doc(doc)
    assert frag in str(exc.value)


def test_duplicate_assertion_id_across_lanes_raises():
    doc = _valid_test_doc()
    doc["assertions"]["rubric"][0]["id"] = "refund-issued"  # clashes with a deterministic id
    with pytest.raises(ValueError, match="duplicate assertion id"):
        CT.validate_conversation_test_doc(doc)


# --------------------------------------------------------------------------
# conversation manifest: build + verify (pass), tamper -> refuse
# --------------------------------------------------------------------------

def _write_children(tmp_path):
    (tmp_path / "transcript").mkdir()
    (tmp_path / "trace").mkdir()
    (tmp_path / "evaluations").mkdir()
    tr = tmp_path / "transcript" / "transcript.json"
    tr.write_text(json.dumps({"segments": [{"role": "agent", "text": "hi"}]}), encoding="utf-8")
    trace = tmp_path / "trace" / "voice_trace.jsonl"
    trace.write_text('{"type":"tool_call","name":"issue_refund"}\n', encoding="utf-8")
    ev = tmp_path / "evaluations" / "deterministic.json"
    ev.write_text(json.dumps({"schema": "assert.v1", "results": []}), encoding="utf-8")
    return {"transcript": str(tr), "trace": str(trace), "assertions": str(ev)}


def _build(tmp_path, origin):
    files = _write_children(tmp_path)
    manifest = CV.build_manifest(
        conversation_id="conv-1",
        agent_id="support-agent-v3",
        origin=origin,
        created_at="2026-07-12T00:00:00Z",
        artifact_files=files,
        base_dir=str(tmp_path),
    )
    CV.write_conversation(manifest, str(tmp_path))
    return manifest


def test_manifest_build_and_verify_passes(tmp_path):
    manifest = _build(tmp_path, {"kind": "real", "provider": "vapi", "provider_call_id": "c-9"})
    _validate_json(manifest, "conversation.v1.json")
    verdict = CV.verify(str(tmp_path))
    assert verdict["ok"] is True
    assert verdict["refused"] is False
    assert sorted(verdict["verified"]) == ["assertions", "trace", "transcript"]


def test_verify_refuses_on_tampered_child(tmp_path):
    _build(tmp_path, {"kind": "real"})
    # tamper a bound child AFTER the manifest pinned its digest
    (tmp_path / "transcript" / "transcript.json").write_text(
        json.dumps({"segments": [{"role": "agent", "text": "TAMPERED"}]}), encoding="utf-8"
    )
    verdict = CV.verify(str(tmp_path))
    assert verdict["ok"] is False
    assert verdict["refused"] is True
    assert any(m["artifact"] == "transcript" for m in verdict["mismatches"])
    assert "REFUSED" in verdict["reason"]


def test_verify_refuses_on_missing_child(tmp_path):
    _build(tmp_path, {"kind": "real"})
    (tmp_path / "trace" / "voice_trace.jsonl").unlink()
    verdict = CV.verify(str(tmp_path))
    assert verdict["ok"] is False and verdict["refused"] is True
    assert any(m["artifact"] == "trace" for m in verdict["missing"])


# --------------------------------------------------------------------------
# origin invariants (synthetic never conflated with real)
# --------------------------------------------------------------------------

def test_origin_kind_missing_rejected(tmp_path):
    with pytest.raises(ValueError, match="origin.kind is REQUIRED"):
        _build(tmp_path, {"provider": "vapi"})


def test_origin_kind_bad_enum_rejected():
    with pytest.raises(ValueError, match="origin.kind is REQUIRED"):
        CV.validate_conversation_doc({
            "kind": "hotato.conversation", "version": 1,
            "conversation_id": "c", "agent_id": "a", "created_at": "t",
            "origin": {"kind": "fabricated"}, "artifacts": {},
        })


def test_simulated_origin_validates_with_simulator(tmp_path):
    origin = {"kind": "simulated", "simulator": {"model_id": "gpt-x", "scenario_id": "s1", "seed": 7}}
    manifest = _build(tmp_path, origin)
    _validate_json(manifest, "conversation.v1.json")
    assert CV.verify(str(tmp_path))["ok"] is True


def test_simulated_origin_without_simulator_rejected(tmp_path):
    with pytest.raises(ValueError, match="simulator"):
        _build(tmp_path, {"kind": "simulated"})


# --------------------------------------------------------------------------
# suite.v1 + release.v1
# --------------------------------------------------------------------------

def _valid_suite():
    return {
        "kind": "hotato.suite", "version": 1, "suite_id": "regression",
        "name": "Refund regression", "purpose": "gate releases",
        "required_for_release": True, "inconclusive_policy": "fail",
        "tests": ["refund-happy-path", "refund-edge"],
    }


def _valid_release():
    return {
        "kind": "hotato.release", "version": 1, "release_id": "rel-2026-07-12",
        "agent_id": "support-agent-v3", "prompt_digest": "abc123", "model": "m1",
        "voice": "v1", "tool_schema_digest": "def456", "workflow_digest": "ghi789",
        "provider_config_digest": "jkl012", "created_at": "2026-07-12T00:00:00Z",
    }


def test_suite_validates_both_ways():
    doc = _valid_suite()
    CT.validate_suite(doc)
    _validate_json(doc, "suite.v1.json")


def test_release_validates_both_ways():
    doc = _valid_release()
    CT.validate_release(doc)
    _validate_json(doc, "release.v1.json")


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("suite_id"), "suite_id"),
    (lambda d: d.__setitem__("tests", "not-a-list"), "tests"),
    (lambda d: d.__setitem__("kind", "hotato.wrong"), "kind"),
])
def test_malformed_suite_raises(mutate, frag):
    doc = _valid_suite()
    mutate(doc)
    with pytest.raises(ValueError) as exc:
        CT.validate_suite(doc)
    assert frag in str(exc.value)


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("release_id"), "release_id"),
    (lambda d: d.pop("created_at"), "created_at"),
    (lambda d: d.__setitem__("prompt_digest", 123), "prompt_digest"),
])
def test_malformed_release_raises(mutate, frag):
    doc = _valid_release()
    mutate(doc)
    with pytest.raises(ValueError) as exc:
        CT.validate_release(doc)
    assert frag in str(exc.value)


# --------------------------------------------------------------------------
# honesty invariant: NONE of the new schemas permit overall_score
# --------------------------------------------------------------------------

@pytest.mark.parametrize("schema_name,instance_fn", [
    ("conversation-test.v1.json", _valid_test_doc),
    ("suite.v1.json", _valid_suite),
    ("release.v1.json", _valid_release),
])
def test_schemas_reject_overall_score(schema_name, instance_fn):
    good = instance_fn()
    _validate_json(good, schema_name)  # sanity: valid without it
    bad = copy.deepcopy(good)
    bad["overall_score"] = 0.87
    with pytest.raises(jsonschema.ValidationError):
        _validate_json(bad, schema_name)


def test_conversation_manifest_schema_rejects_overall_score(tmp_path):
    manifest = _build(tmp_path, {"kind": "real"})
    _validate_json(manifest, "conversation.v1.json")
    bad = copy.deepcopy(manifest)
    bad["overall_score"] = 0.87
    with pytest.raises(jsonschema.ValidationError):
        _validate_json(bad, "conversation.v1.json")
    # and the Python validator rejects it too
    with pytest.raises(ValueError, match="overall_score"):
        CV.validate_conversation_doc(bad)
