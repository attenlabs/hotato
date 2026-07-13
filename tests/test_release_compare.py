"""``hotato release compare`` (hotato.release_compare): a digest-exact
per-dimension comparison of two releases from the registry -- per-dimension
deltas + new-failures / fixed-since + per-scenario status changes, never a single
blended delta score, with an honest empty state when a side has no runs.
"""

from __future__ import annotations

import os

import pytest

from hotato import release_compare as RC
from hotato.fleet.registry import Registry


def _seed_release(reg, ws, release_id, scenario_status):
    """Record a release with one run + conversation per scenario and one
    evaluation per (scenario, dimension) at the given status."""
    reg.add_release(ws, release_id, agent_id="a", prompt_digest=f"digest-{release_id}")
    for scn, dims in scenario_status.items():
        run_id = f"{release_id}:{scn}"
        conv_id = f"{release_id}:{scn}:conv"
        reg.add_run(ws, run_id, scenario_id=scn, release_id=release_id, status="completed")
        reg.add_conversation(ws, conv_id, run_id=run_id, agent_id="a", origin="simulated")
        for dim, status in dims.items():
            reg.add_evaluation(ws, f"{conv_id}:{dim}", conversation_id=conv_id,
                               evaluator_id="det", dimension=dim, status=status)


@pytest.fixture()
def reg(tmp_path):
    r = Registry(os.path.join(tmp_path, "reg"))
    yield r
    r.close()


def _compare(tmp_path, a, b, ws="w"):
    return RC.compare_releases(a, b, registry_home=os.path.join(tmp_path, "reg"),
                               workspace=ws)


def test_new_failures_and_fixed_since(tmp_path, reg):
    # baseline: refund outcome PASS, escalate policy FAIL.
    _seed_release(reg, "w", "rc1", {
        "refund": {"outcome": "PASS", "conversation": "PASS"},
        "escalate": {"policy": "FAIL"},
    })
    # candidate: refund outcome now FAILs (regression), escalate policy now PASSes.
    _seed_release(reg, "w", "rc2", {
        "refund": {"outcome": "FAIL", "conversation": "PASS"},
        "escalate": {"policy": "PASS"},
    })
    reg.close()  # release_compare opens its own connection

    cmp = _compare(tmp_path, "rc1", "rc2")
    assert cmp["comparable"] is True
    assert {"scenario_id": "refund", "dimension": "outcome"} in cmp["new_failures"]
    assert {"scenario_id": "escalate", "dimension": "policy"} in cmp["fixed_since"]
    # per-dimension deltas, never a single blended delta score.
    pd = cmp["per_dimension"]
    assert pd["outcome"]["baseline"]["pass"] == 1
    assert pd["outcome"]["candidate"]["fail"] == 1
    assert pd["outcome"]["delta"]["fail"] == 1
    assert pd["outcome"]["delta"]["pass"] == -1
    # digest-exact provenance surfaced for both sides.
    assert cmp["baseline"]["release"]["prompt_digest"] == "digest-rc1"
    assert cmp["candidate"]["release"]["prompt_digest"] == "digest-rc2"


def test_scenario_changes_listed(tmp_path, reg):
    _seed_release(reg, "w", "rc1", {"refund": {"outcome": "PASS"}})
    _seed_release(reg, "w", "rc2", {"refund": {"outcome": "FAIL"}})
    reg.close()
    cmp = _compare(tmp_path, "rc1", "rc2")
    changes = cmp["scenario_changes"]
    assert {"scenario_id": "refund", "dimension": "outcome",
            "baseline": "PASS", "candidate": "FAIL"} in changes


def test_no_single_delta_score(tmp_path, reg):
    _seed_release(reg, "w", "rc1", {"refund": {"outcome": "PASS"}})
    _seed_release(reg, "w", "rc2", {"refund": {"outcome": "FAIL"}})
    reg.close()
    cmp = _compare(tmp_path, "rc1", "rc2")

    def _has(obj, key):
        if isinstance(obj, dict):
            return key in obj or any(_has(v, key) for v in obj.values())
        if isinstance(obj, list):
            return any(_has(v, key) for v in obj)
        return False

    assert not _has(cmp, "overall_score")
    assert not _has(cmp, "delta_score")


def test_missing_side_is_honest_empty_state(tmp_path, reg):
    _seed_release(reg, "w", "rc1", {"refund": {"outcome": "PASS"}})
    reg.close()
    cmp = _compare(tmp_path, "rc1", "does-not-exist")
    assert cmp["baseline"]["present"] is True
    assert cmp["candidate"]["present"] is False
    assert cmp["candidate"]["has_runs"] is False
    assert cmp["comparable"] is False
    # rendered text states the empty side plainly (no fabricated baseline).
    text = RC.render_text(cmp)
    assert "NOT REGISTERED" in text
    assert "does-not-exist" in text


def test_registered_but_no_runs_is_empty_state(tmp_path, reg):
    reg.add_release("w", "rc1", agent_id="a")  # registered, but no runs
    _seed_release(reg, "w", "rc2", {"refund": {"outcome": "PASS"}})
    reg.close()
    cmp = _compare(tmp_path, "rc1", "rc2")
    assert cmp["baseline"]["present"] is True
    assert cmp["baseline"]["has_runs"] is False
    assert cmp["comparable"] is False
    text = RC.render_text(cmp)
    assert "NO\n" in text or "has NO" in text


def test_new_coverage_is_not_a_regression(tmp_path, reg):
    # candidate runs a scenario the baseline never ran -> new coverage, NOT a
    # new failure, even though it FAILs.
    _seed_release(reg, "w", "rc1", {"refund": {"outcome": "PASS"}})
    _seed_release(reg, "w", "rc2", {"refund": {"outcome": "PASS"},
                                    "brand-new": {"outcome": "FAIL"}})
    reg.close()
    cmp = _compare(tmp_path, "rc1", "rc2")
    assert {"scenario_id": "brand-new", "dimension": "outcome"} not in cmp["new_failures"]


def test_render_text_has_per_dimension_lines(tmp_path, reg):
    _seed_release(reg, "w", "rc1", {"refund": {"outcome": "PASS"}})
    _seed_release(reg, "w", "rc2", {"refund": {"outcome": "PASS"}})
    reg.close()
    text = RC.render_text(_compare(tmp_path, "rc1", "rc2"))
    assert "per-dimension" in text
    for dim in ("outcome", "policy", "conversation", "speech", "reliability"):
        assert dim in text
