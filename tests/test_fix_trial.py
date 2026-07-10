"""``hotato fix trial`` (S4): compose apply's clone-only offline gate + verify's
battery-scale rollup + contract verify + explain into ONE before/after proof,
fail-closed.

Covers the load-bearing behaviours from the spec:

* an improving before/after battery -> verdict "improved", exit 0;
* a hold/opposite-risk fixture flipping pass -> fail -> verdict "regressed",
  exit 1 (fail-closed, never a soft pass);
* too few previously-failing fixtures (below --min-n) -> verdict
  "inconclusive", exit 1 (fail-closed, NOT a pass);
* a --contracts regression, and a --policy violation, each independently
  force "regressed" even when the battery itself improved;
* the both-axes threshold-funnel patch REFUSES before any before/after
  evidence is read (exit 3, the same code hotato apply uses);
* fix trial never creates a clone and never touches the network (the same
  clone-only, production-unmutatable guarantee hotato apply's dry run gives);
* the attribution section folds in hotato explain's output on the BEFORE
  evidence;
* --out / --html write the JSON proof / the self-contained HTML report;
* no em or en dashes in any rendered output.
"""

from __future__ import annotations

import json
import os

import pytest

from hotato import apply as _apply
from hotato import cli
from hotato import fix_trial as _fix_trial
from hotato import fixplan as _fixplan
from hotato import patch as _patch
from hotato.diagnose import OPPOSITE_RISK

HARD = None
BACKCHANNEL = None
try:
    from importlib import resources
    HARD = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    BACKCHANNEL = str(resources.files("hotato").joinpath(
        "data", "audio", "02-backchannel-mhm.example.wav"))
except Exception:  # pragma: no cover - resource lookup always succeeds here
    pass


# --- shared fixture builders (mirrors test_apply_clone.py / test_verify.py) --

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


def _funnel_plan():
    diag = {"battery": {"finding": "threshold_funnel", "failed": 2, "events": 3,
                        "opposite_risk_coverage": {}}, "diagnoses": []}
    return _fixplan.build_plan(diagnosis=diag, inspected=None, stack="vapi")


def _write_patch(tmp_path, plan, *, plan_name="fixplan.json",
                 patch_name="patch.json"):
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
    return _write_patch(tmp_path, _funnel_plan())


def _ev(eid, expected_yield, passed, tov=0.0, tty=None, scorable=True):
    v = {
        "passed": passed,
        "did_yield": expected_yield if passed else (not expected_yield),
        "talk_over_sec": tov,
        "seconds_to_yield": tty,
        "reasons": [] if passed else ["out of bound"],
    }
    e = {"event_id": eid, "scenario_id": eid,
         "expected_yield": expected_yield, "verdict": v}
    if not scorable:
        e["scorable"] = False
    return e


def _env(events):
    passed = sum(1 for e in events if e["verdict"]["passed"])
    return {
        "tool": "hotato", "mode": "suite", "stack": "vapi", "offline": True,
        "events": events,
        "summary": {"events": len(events), "passed": passed,
                    "failed": len(events) - passed},
        "exit_code": 0,
    }


def _write_env(tmp_path, name, events):
    p = tmp_path / name
    p.write_text(json.dumps(_env(events)), encoding="utf-8")
    return str(p)


def _improving_sides(tmp_path):
    """Three previously-failing yield fixtures fixed + a hold guard still
    passing: a clean, min-n-supported improvement with no regression."""
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9, 2.1),
        _ev("f3", True, False, 1.5), _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("h1", False, True, 0.0),
    ])
    return before, after


def _run(tmp_path, patch_path, before, after, *extra):
    return cli.main([
        "fix", "trial", str(patch_path), "--name", "staging-x",
        "--before", before, "--after", after, *extra,
    ])


# --- IMPROVED -----------------------------------------------------------

def test_improvement_path_verdict_and_exit_code(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_IMPROVED == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "improved"
    assert payload["exit_code"] == 0
    assert payload["verify"]["regression_axis"]["now_pass"] == 3
    assert payload["verify"]["hold_axis"]["still_pass"] == 1
    assert payload["contract_verify"] is None
    assert payload["refusal"] is None
    assert payload["apply"]["clone"]["name"] == "staging-x"


def test_run_trial_improved_reuses_verify_and_apply_exactly(tmp_path, config_patch):
    before, after = _improving_sides(tmp_path)
    with open(config_patch, encoding="utf-8") as fh:
        patch = json.load(fh)
    plan = _apply.load_referenced_plan(patch, str(config_patch))
    t = _fix_trial.run_trial(
        patch, name="staging-x", before=before, after=after,
        patch_source=str(config_patch), plan=plan,
    )
    assert t["verdict"] == _fix_trial.VERDICT_IMPROVED
    assert t["exit_code"] == 0
    assert t["apply"]["clone"]["name"] == "staging-x"
    assert t["clone_only"] is True
    assert t["production_apply_supported"] is False
    # the default --battery reuses --before (it already carries the labels)
    assert t["battery"] == before


# --- REGRESSED: opposite-risk / hold-axis regression forces fail ------------

def test_opposite_risk_regression_forces_fail(tmp_path, config_patch, capsys):
    # f1/f2/f3 fixed (an apparent improvement), but the hold guard h1 flips
    # from passing to failing: a naive bandaid that trades talk-over for a
    # false yield. This must fail the trial even though 3 fixtures improved.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9, 2.1),
        _ev("f3", True, False, 1.5), _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("h1", False, False, 1.1),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert "h1" in payload["verify"]["regressions"]
    assert "REGRESSED" in payload["conclusion"]
    assert "fail-closed" in payload["conclusion"]


def test_any_fixture_regression_forces_fail(tmp_path, config_patch, capsys):
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, True, 0.2, 0.4),
        _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.2, 0.3),   # fixed
        _ev("f2", True, False, 1.0, 2.0),  # regressed (used to pass)
        _ev("h1", False, True, 0.0),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--min-n", "1",
             "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert "f2" in payload["verify"]["regressions"]


# --- INCONCLUSIVE: fail-closed, never a soft pass ----------------------------

def test_low_n_is_inconclusive_and_fails_closed(tmp_path, config_patch, capsys):
    # Only 2 previously-failing fixtures, below the default --min-n 3: the
    # claim is refused at the verify layer, and the trial must not pass.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9),
        _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("h1", False, True, 0.0),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert "INCONCLUSIVE" in payload["conclusion"]
    assert "not a soft pass" in payload["conclusion"]
    assert payload["verify"]["claim"]["supported"] is False


def test_zero_improvement_is_inconclusive(tmp_path, config_patch, capsys):
    # Enough previously-failing fixtures (>= min-n) but NONE now pass: not a
    # regression (nothing that used to pass now fails), but not an
    # improvement either.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9),
        _ev("f3", True, False, 1.1), _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9),
        _ev("f3", True, False, 1.1), _ev("h1", False, True, 0.0),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--min-n", "3",
             "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"


# --- --contracts and --policy independently force a regression --------------

@pytest.mark.skipif(HARD is None, reason="bundled example audio unavailable")
def test_contract_regression_forces_fail_even_when_battery_improved(
        tmp_path, config_patch, capsys):
    contracts_dir = tmp_path / "contracts"
    # HARD actually yields; labelling it "hold" makes this contract fail its
    # own policy immediately, independent of anything the trial changed --
    # exactly the "neighbouring cases" check: any contract regression fails.
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "neighbour-1",
        "--onset", "2.40", "--expect", "hold", "--out", str(contracts_dir),
    ])
    assert rc in (0, 1)  # create scores immediately; a failing label is fine
    capsys.readouterr()  # discard the `contract create` output

    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--contracts",
             str(contracts_dir), "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert payload["contract_verify"]["summary"]["failed"] >= 1
    assert "contract" in payload["conclusion"].lower()


def test_policy_violation_forces_fail(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    policy_path = tmp_path / "hotato.verify.yaml"
    policy_path.write_text(
        "guardrails:\n  max_new_false_yields: 0\n  require_hold_fixture: true\n"
        "target:\n  improve:\n    talk_over_sec_p95: -100\n",  # impossible target
        encoding="utf-8",
    )
    rc = _run(tmp_path, config_patch, before, after, "--policy",
             str(policy_path), "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert payload["verify"]["policy"]["passed"] is False


# --- REFUSED: the both-axes threshold funnel, before any evidence is read ---

def test_threshold_funnel_refuses_before_reading_before_after(
        tmp_path, funnel_patch, capsys, monkeypatch):
    # Even nonexistent before/after paths never get opened: the refusal
    # fires before verify (or explain, or contract verify) ever runs.
    from hotato import verify as _verify

    def boom(*a, **k):
        raise AssertionError("fix trial read before/after on the refused path")

    monkeypatch.setattr(_verify, "verify_sides", boom)
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--name", "staging-x",
        "--before", str(tmp_path / "does-not-exist-before.json"),
        "--after", str(tmp_path / "does-not-exist-after.json"),
        "--format", "json",
    ])
    assert rc == _fix_trial.EXIT_REFUSED == _apply.REFUSAL_EXIT_CODE == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["verify"] is None
    assert payload["contract_verify"] is None
    assert payload["attribution"] is None
    assert "No config patch will be applied" in payload["refusal"]["headline"]
    assert payload["refusal"]["reason"] == _apply.REFUSAL_REASON


def test_threshold_funnel_refuses_without_name_or_before_after_text(
        funnel_patch, capsys):
    # REFUSAL-FIRST holds even with no --before/--after/--name (argparse
    # still requires --before/--after; give harmless nonexistent paths).
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--before", "nope-b.json",
        "--after", "nope-a.json",
    ])
    assert rc == 3
    out = capsys.readouterr().out
    assert "No config patch will be applied" in out
    assert "Recommended: enable or add engagement-control" in out


# --- clone isolation: never creates a clone, never touches the network ------

def test_never_creates_a_clone_or_touches_the_network(
        tmp_path, config_patch, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fix trial hit the network / created a clone")

    monkeypatch.setattr(_apply, "_http_json", boom)
    monkeypatch.setattr(_apply, "create_clone", boom)

    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clone_only"] is True
    assert payload["production_apply_supported"] is False
    # the tripwire above proves it: had run_trial called either networked
    # primitive, monkeypatch's `boom` would have raised and this line would
    # never be reached.


# --- attribution: folds in hotato explain on the BEFORE evidence ------------

def test_attribution_section_is_populated_from_explain(
        tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    a = payload["attribution"]
    assert a["schema"] == "hotato.explain.v1"
    assert a["sources"] == [before]
    assert len(a["explanations"]) == 1
    assert a["explanations"][0]["input_kind"] == "run_envelope"
    # 3 missed-interruption events with opposite-risk coverage (the hold
    # fixture) -> explain attributes them safe_to_patch, not a refusal.
    assert a["attributions"], a


def test_attribution_degrades_honestly_on_unreadable_before(
        tmp_path, config_patch, capsys):
    before = tmp_path / "before-dir"
    before.mkdir()
    (before / "not-an-envelope.json").write_text(
        json.dumps({"tool": "hotato", "kind": "not-a-run", "events": []}),
        encoding="utf-8")
    # a same-shaped envelope so verify still has something to pair; use the
    # improving after/before pair but drop the extra file into --before's dir.
    before_env, after = _improving_sides(tmp_path)
    import shutil
    shutil.copy(before_env, before / "before.json")
    rc = _run(tmp_path, config_patch, str(before), after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    a = payload["attribution"]
    assert a["unreadable"], a
    assert any("not-an-envelope.json" in u["source"] for u in a["unreadable"])


# --- usage errors: the same gates apply already enforces ---------------------

def test_missing_name_is_a_usage_error(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = cli.main([
        "fix", "trial", str(config_patch), "--before", before, "--after", after,
    ])
    assert rc == 2
    assert "--name" in capsys.readouterr().err


def test_bad_contracts_dir_is_a_usage_error(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    empty = tmp_path / "no-contracts-here"
    empty.mkdir()
    rc = _run(tmp_path, config_patch, before, after, "--contracts", str(empty))
    assert rc == 2
    assert "no hotato contracts" in capsys.readouterr().err


def test_missing_patch_file_is_a_usage_error(tmp_path, capsys):
    before, after = _improving_sides(tmp_path)
    rc = cli.main([
        "fix", "trial", str(tmp_path / "nope.json"), "--name", "x",
        "--before", before, "--after", after,
    ])
    assert rc == 2


# --- --out / --html ----------------------------------------------------------

def test_out_writes_full_json_proof(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    out_path = tmp_path / "fix-trial.json"
    rc = _run(tmp_path, config_patch, before, after, "--out", str(out_path))
    assert rc == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["verdict"] == "improved"
    assert written["schema"] == "hotato.fix_trial.v1"


def test_html_writes_a_self_contained_report(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    html_path = tmp_path / "fix-trial.html"
    rc = _run(tmp_path, config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "hotato fix trial" in html
    assert "IMPROVED" in html
    assert "coincidence" in html


def test_refused_html_report_renders(tmp_path, funnel_patch, capsys):
    html_path = tmp_path / "refused.html"
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--name", "x",
        "--before", "nope-b.json", "--after", "nope-a.json",
        "--html", str(html_path),
    ])
    assert rc == 3
    html = html_path.read_text(encoding="utf-8")
    assert "No config patch will be applied" in html


# --- no em or en dashes anywhere ---------------------------------------------

def test_no_em_or_en_dashes_in_any_rendered_output(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after)
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "—" not in text_out
    assert "–" not in text_out

    html_path = tmp_path / "fix-trial.html"
    rc = _run(tmp_path, config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    capsys.readouterr()
    html = html_path.read_text(encoding="utf-8")
    assert "—" not in html
    assert "–" not in html
