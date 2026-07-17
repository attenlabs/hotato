"""Provenance regression: a ``state`` / ``state_change`` verdict reads Authority 2
(the post-call system of record) and drives the OUTCOME dimension + the exit
code, so the state evidence that produced it MUST be BOUND into the conversation
artifact and re-provable -- exactly as trace/transcript already are.

The defect (external bakeoff, P0): ``hotato test run --state`` scored a PASS/FAIL
off a state fixture, but the produced ``conversation.v1`` artifact bound only
audio/transcript/trace/timing/assertions -- NO state-evidence child, and
``_eval_state`` never stored the observed record. The bundle digest-verified
``ok=True`` while the state fixture/projection behind the verdict was un-bound and
un-provable: the verdict was swappable without any digest refusal.

These tests pin the fail-closed fix:
  * a determinate state read BINDS a re-hashable ``state`` evidence child
    carrying the query descriptor + the observed projection;
  * :func:`hotato.conversation.verify` REFUSES a bundle whose bound
    ``assertions`` child records a determinate state/state_change result but has
    no ``state`` evidence child (the pre-fix bundle shape) -- the crux;
  * an INCONCLUSIVE state read (no adapter) queried no authority, produced no
    evidence, and must NOT require a state child (no false refusal).
"""

import json

import pytest

from hotato import assert_ as A
from hotato import cli
from hotato import conversation as CV
from hotato.state_adapter import MockStateAdapter

# --- helpers ---------------------------------------------------------------

def _state_test_file(tmp_path, *, name="state-prov", policy="refuse"):
    doc = {
        "kind": "hotato.conversation-test", "version": 1, "id": name,
        "agent": "billing-bot",
        "assertions": {"deterministic": [
            {"id": "refund-issued", "kind": "state", "dimension": "outcome",
             "resource": "refunds", "filters": {"id": "R-100"},
             "expect": {"status": "issued"}},
        ], "rubric": []},
        "inconclusive_policy": policy,
        "success": {"required": ["no_deterministic_fail"]},
    }
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _state_fixture(tmp_path, *, status="issued"):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(
        {"refunds": [{"id": "R-100", "status": status, "amount": 4200}]}),
        encoding="utf-8")
    return str(p)


# --- 1. a state verdict binds a re-provable state-evidence child -----------

def test_state_verdict_binds_provable_state_evidence_child(tmp_path, capsys):
    out = tmp_path / "artifact"
    code = cli.main([
        "test", "run", _state_test_file(tmp_path), "--agent", "billing-bot",
        "--state", _state_fixture(tmp_path), "--out", str(out),
        "--created-at", "2026-07-16T00:00:00Z", "--format", "json",
    ])
    result = json.loads(capsys.readouterr().out)
    # The state PASS drove the outcome verdict + exit code.
    assert result["assertions"]["results"][0]["status"] == "PASS"
    assert code == 0

    manifest = json.loads((out / "conversation.json").read_text())
    # The state evidence is now a BOUND child (it was absent before the fix).
    assert "state" in manifest["artifacts"], manifest["artifacts"]

    evidence = json.loads((out / "state-evidence.json").read_text())
    entry = evidence["entries"][0]
    assert entry["id"] == "refund-issued"
    assert entry["kind"] == "state"
    assert entry["resource"] == "refunds"
    assert entry["filters"] == {"id": "R-100"}
    # The OBSERVED Authority-2 projection is captured (was absent entirely before).
    assert entry["observed"] == {"id": "R-100", "status": "issued", "amount": 4200}

    verdict = CV.verify(str(out))
    assert verdict["ok"] is True and verdict["refused"] is False
    assert "state" in verdict["verified"]
    assert verdict["unbound"] == []

    # The manifest binding a `state` child stays conformant to the schema of
    # record (the closed artifact set now includes `state`).
    jsonschema = pytest.importorskip("jsonschema")
    from importlib import resources
    schema = json.loads(resources.files("hotato").joinpath(
        "schema", "conversation.v1.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=manifest, schema=schema)


# --- 2. THE REGRESSION: a state verdict with no bound state evidence is refused

def _bundle_with_unbound_state_verdict(tmp_path, status="PASS"):
    """Build the PRE-FIX bundle shape by hand: a bound ``assertions`` child that
    records a determinate ``state`` result, and NO ``state`` evidence child. This
    is exactly what the old assembler produced -- the un-provable, swappable
    verdict. On the old ``verify`` this bundle returned ``ok=True``; the fix must
    REFUSE it."""
    art = tmp_path / "unbound"
    art.mkdir()
    env = {
        "schema": "assert.v1", "exit_code": 0, "inconclusive_policy": "refuse",
        "results": [{"id": "refund-issued", "kind": "state",
                     "deterministic": True, "dimension": "outcome",
                     "status": status}],
        "summary": {"deterministic": {"pass": 1, "fail": 0, "inconclusive": 0},
                    "judge": {"pass": 0, "fail": 0}},
    }
    (art / "assertions.json").write_text(json.dumps(env), encoding="utf-8")
    manifest = CV.build_manifest(
        conversation_id="state-prov", agent_id="billing-bot",
        origin={"kind": "fixture"}, created_at="2026-07-16T00:00:00Z",
        artifact_files={"assertions": str(art / "assertions.json")},
        base_dir=str(art),
    )
    CV.write_conversation(manifest, str(art))
    return art


def test_state_verdict_without_bound_evidence_is_refused(tmp_path):
    art = _bundle_with_unbound_state_verdict(tmp_path, status="PASS")

    verdict = CV.verify(str(art))
    # The digests are all intact -- the OLD verify (re-hash only) returned
    # ok=True here. The fix cross-checks state authority and REFUSES.
    assert verdict["refused"] is True
    assert verdict["ok"] is False
    assert verdict["mismatches"] == [] and verdict["missing"] == []
    assert verdict["unbound"] and verdict["unbound"][0]["artifact"] == "state"
    assert "refund-issued" in verdict["unbound"][0]["reason"]


def _bundle_with_irrelevant_state_evidence(tmp_path, status="PASS"):
    """Build a bundle where a determinate ``state`` verdict for id
    'refund-issued' is recorded AND a ``state`` evidence child is bound with a
    correct digest -- but its only entry covers an UNRELATED id. Every digest is
    intact and a 'state' child exists, so the presence-only check passed this;
    the coverage check must still REFUSE because the bound evidence does not
    cover the verdict it claims to prove (the swap-in-irrelevant exploit)."""
    art = tmp_path / "irrelevant"
    art.mkdir()
    env = {
        "schema": "assert.v1", "exit_code": 0, "inconclusive_policy": "refuse",
        "results": [{"id": "refund-issued", "kind": "state",
                     "deterministic": True, "dimension": "outcome",
                     "status": status}],
        "summary": {"deterministic": {"pass": 1, "fail": 0, "inconclusive": 0},
                    "judge": {"pass": 0, "fail": 0}},
    }
    (art / "assertions.json").write_text(json.dumps(env), encoding="utf-8")
    state_ev = {
        "kind": "hotato.state-evidence", "version": 1,
        "entries": [{"id": "totally-unrelated-assertion", "kind": "state",
                     "resource": "widgets", "filters": {}, "observed": {"x": 1}}],
    }
    (art / "state-evidence.json").write_text(
        json.dumps(state_ev), encoding="utf-8")
    manifest = CV.build_manifest(
        conversation_id="state-prov", agent_id="billing-bot",
        origin={"kind": "fixture"}, created_at="2026-07-16T00:00:00Z",
        artifact_files={"assertions": str(art / "assertions.json"),
                        "state": str(art / "state-evidence.json")},
        base_dir=str(art),
    )
    CV.write_conversation(manifest, str(art))
    return art


def test_state_verdict_with_irrelevant_bound_evidence_is_refused(tmp_path):
    art = _bundle_with_irrelevant_state_evidence(tmp_path, status="PASS")

    verdict = CV.verify(str(art))
    # All digests intact and a 'state' child IS bound -- but it covers the wrong
    # id. The presence-only check let this through; the coverage check REFUSES.
    assert verdict["refused"] is True
    assert verdict["ok"] is False
    assert verdict["mismatches"] == [] and verdict["missing"] == []
    assert verdict["unbound"] and verdict["unbound"][0]["artifact"] == "state"
    assert "refund-issued" in verdict["unbound"][0]["reason"]


def test_state_verdict_refusal_holds_for_fail_status(tmp_path):
    """A FAIL state verdict is just as un-provable without bound evidence: the
    refusal is about the verdict having READ state authority, not about which way
    it came out."""
    art = _bundle_with_unbound_state_verdict(tmp_path, status="FAIL")
    verdict = CV.verify(str(art))
    assert verdict["refused"] is True and verdict["ok"] is False


def test_state_verdict_refusal_surfaces_on_the_cli(tmp_path, capsys):
    art = _bundle_with_unbound_state_verdict(tmp_path, status="PASS")
    code = cli.main(["conversation", "verify", str(art)])
    out = capsys.readouterr().out
    assert code == 2  # a refusal is exit 2, never a silent accept
    assert "REFUSED" in out and "UNBOUND" in out


# --- 3. an INCONCLUSIVE state read requires no state child (no false refusal)

def test_inconclusive_state_requires_no_state_child(tmp_path, capsys):
    out = tmp_path / "art-inconc"
    # NO --state adapter: the state read is INCONCLUSIVE (queried no authority),
    # so it produces no evidence and must not demand a bound state child.
    code = cli.main([
        "test", "run", _state_test_file(tmp_path, policy="report"),
        "--agent", "billing-bot", "--out", str(out),
        "--created-at", "2026-07-16T00:00:00Z", "--format", "json",
    ])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["assertions"]["results"][0]["status"] == "INCONCLUSIVE"

    manifest = json.loads((out / "conversation.json").read_text())
    assert "state" not in manifest["artifacts"]
    assert not (out / "state-evidence.json").exists()

    verdict = CV.verify(str(out))
    assert verdict["ok"] is True and verdict["refused"] is False
    assert verdict["unbound"] == []


# --- 4. the capture seam itself: determinate reads log the projection, ------
#        INCONCLUSIVE reads log nothing

def test_eval_state_captures_observed_projection_on_determinate_read():
    ad = MockStateAdapter({"refunds": [{"id": "R-100", "status": "issued",
                                        "amount": 4200}]})
    ctx = A.build_context(state_adapter=ad)
    r = A.evaluate_assertion(
        {"id": "s1", "kind": "state", "resource": "refunds",
         "filters": {"id": "R-100"}, "expect": {"status": "issued"}}, ctx)
    assert r["status"] == "PASS"
    assert len(ctx.state_evidence) == 1
    ev = ctx.state_evidence[0]
    assert ev["id"] == "s1" and ev["kind"] == "state"
    assert ev["observed"] == {"id": "R-100", "status": "issued", "amount": 4200}


def test_eval_state_grounded_absence_is_still_captured():
    ad = MockStateAdapter({"refunds": [{"id": "OTHER"}]})
    ctx = A.build_context(state_adapter=ad)
    r = A.evaluate_assertion(
        {"id": "s1", "kind": "state", "resource": "refunds",
         "filters": {"id": "R-100"}, "expect": {"status": "issued"}}, ctx)
    assert r["status"] == "FAIL"
    assert ctx.state_evidence[0]["observed"] is None


def test_inconclusive_state_read_logs_no_evidence():
    ctx = A.build_context()  # no state adapter
    r = A.evaluate_assertion(
        {"id": "s1", "kind": "state", "resource": "refunds",
         "filters": {"id": "R-100"}, "expect": {"status": "issued"}}, ctx)
    assert r["status"] == "INCONCLUSIVE"
    assert ctx.state_evidence == []


def test_repeated_replay_records_state_evidence_once():
    ad = MockStateAdapter({"refunds": [{"id": "R-100", "status": "issued"}]})
    ctx = A.build_context(state_adapter=ad)
    a = {"id": "s1", "kind": "state", "resource": "refunds",
         "filters": {"id": "R-100"}, "expect": {"status": "issued"}}
    A.evaluate_assertion(a, ctx)
    A.evaluate_assertion(a, ctx)
    A.evaluate_assertion(a, ctx)
    assert len(ctx.state_evidence) == 1  # deduped by id, not once per replay
