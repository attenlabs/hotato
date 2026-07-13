#!/usr/bin/env python3
"""Seed a self-hosted hotato workspace with a small, clearly-labelled EXAMPLE
dataset so the five ``hotato serve`` views render with content on first boot.

Why this exists (and why not ``hotato start --demo``): ``hotato start --demo``
writes a *sweep report* (``hotato-sweep.json`` + an HTML dashboard) into a
directory; it does not populate the fleet registry's 8-entity model, which is
what the team workspace (``hotato serve``) reads. To make the workspace show
release readiness, a scenario matrix, failure clusters, and production health
immediately, this script writes a handful of Agent / Release / Suite / Scenario
/ Run / Conversation / Evaluation / Review / assertion-run rows through the
PUBLIC ``hotato.fleet.registry`` API -- the same API the CLI writes through.

This is EXAMPLE data, honestly labelled:

* the workspace name says "example data";
* ``origin`` is set truthfully per conversation (``real`` vs ``simulated``) so
  the always-separate real/simulated axis is never conflated;
* evaluation statuses are a fixed illustrative mix (PASS / FAIL / INCONCLUSIVE),
  not a claim about any real agent.

Keep your own calls clear of it by ingesting into a different workspace id (the
demo lives in ``default``), or reset the volume (``docker compose down -v``).
``--clear`` prints reset guidance; the registry exposes no row-delete API, so a
volume reset is the reliable way to remove the example data.

Idempotent: re-running is a no-op once the demo agent exists (unless ``--force``).
Registry-only: it opens no socket and touches no artifact store, so it depends
on nothing but the stable registry schema.
"""
from __future__ import annotations

import argparse
import sys
import time

# Two illustrative "days" (epoch seconds) so production-health has >= 2 points.
_DAY1 = 1_720_000_000.0          # older
_DAY2 = _DAY1 + 86_400.0         # newer
_REL_OLD = _DAY1 - 3_600.0
_REL_NEW = _DAY2 - 3_600.0

_AGENT = "support-bot"
_WS_NAME = "Demo Company (example data — clear before ingesting real calls)"


def _already_seeded(reg, ws: str) -> bool:
    try:
        return any(a.get("agent_id") == _AGENT for a in reg.list_agents(ws))
    except Exception:
        return False


def _clear(reg, ws: str) -> None:
    """Best-effort removal of the demo rows via whatever delete surface the
    registry exposes; if none is available, tell the operator to reset ``/data``."""
    deleter = getattr(reg, "delete_agent", None) or getattr(reg, "remove_agent", None)
    if deleter is None:
        print("seed-demo: this registry build exposes no agent-delete method; "
              "to clear the demo, stop the stack and remove the /data volume "
              "(docker compose down -v).", file=sys.stderr)
        return
    try:
        deleter(ws, _AGENT)
        print("seed-demo: cleared demo agent %r from workspace %r." % (_AGENT, ws))
    except Exception as exc:  # pragma: no cover - depends on registry build
        print("seed-demo: could not clear demo rows (%s); remove the /data volume "
              "to reset." % exc, file=sys.stderr)


def seed(home: str, ws: str) -> None:
    from hotato.fleet.registry import Registry

    reg = Registry(home=home)
    try:
        reg.ensure_workspace(ws, _WS_NAME)
        reg.add_agent(ws, _AGENT, name="Support Bot", stack="vapi")

        # Two releases: r-2026-07 is the current release, r-2026-06 the baseline.
        reg.add_release(ws, "r-2026-06", agent_id=_AGENT, model="model-a",
                        prompt_digest="p-june", created_at=_REL_OLD)
        reg.add_release(ws, "r-2026-07", agent_id=_AGENT, model="model-b",
                        prompt_digest="p-july", created_at=_REL_NEW)
        reg.set_agent_release(ws, _AGENT, current_release_id="r-2026-07")

        # One required release-gate suite; INCONCLUSIVE fails it (compliance posture).
        reg.add_suite(ws, "release-gate", name="Release gate",
                      purpose="pre-ship required suite", required_for_release=True,
                      inconclusive_policy="fail", created_at=_REL_OLD)

        reg.add_scenario(ws, "refund-after-cutoff", suite_id="release-gate",
                         goal="issue a refund requested after the policy cutoff",
                         created_at=_REL_OLD)
        reg.add_scenario(ws, "reschedule-appointment", suite_id="release-gate",
                         goal="move an existing appointment to a new day",
                         created_at=_REL_OLD)
        reg.add_scenario(ws, "check-order-status", suite_id="release-gate",
                         goal="read back the status of an order by number",
                         created_at=_REL_OLD)

        # refund-after-cutoff: 1 baseline rep + 3 current reps (reliability / pass^k).
        reg.add_run(ws, "run-refund-base", scenario_id="refund-after-cutoff",
                    release_id="r-2026-06", status="completed")
        for r in ("run-refund-1", "run-refund-2", "run-refund-3"):
            reg.add_run(ws, r, scenario_id="refund-after-cutoff",
                        release_id="r-2026-07", status="completed")
        # reschedule: fails on baseline, fixed on current.
        reg.add_run(ws, "run-resched-base", scenario_id="reschedule-appointment",
                    release_id="r-2026-06", status="completed")
        reg.add_run(ws, "run-resched-1", scenario_id="reschedule-appointment",
                    release_id="r-2026-07", status="completed")
        # check-order-status: two clean current reps.
        reg.add_run(ws, "run-status-1", scenario_id="check-order-status",
                    release_id="r-2026-07", status="completed")
        reg.add_run(ws, "run-status-2", scenario_id="check-order-status",
                    release_id="r-2026-07", status="completed")

        # Conversations: real AND simulated, across two days. origin is truthful.
        convs = [
            ("conv-refund-base", "run-refund-base", "real", _DAY1),
            ("conv-refund-1", "run-refund-1", "simulated", _DAY1),
            ("conv-refund-2", "run-refund-2", "simulated", _DAY1),
            ("conv-refund-3", "run-refund-3", "simulated", _DAY2),
            ("conv-resched-base", "run-resched-base", "real", _DAY1),
            ("conv-resched-1", "run-resched-1", "real", _DAY2),
            ("conv-status-1", "run-status-1", "simulated", _DAY2),
            ("conv-status-2", "run-status-2", "real", _DAY2),
        ]
        for cid, rid, origin, ca in convs:
            reg.add_conversation(ws, cid, run_id=rid, agent_id=_AGENT,
                                 origin=origin, created_at=ca)

        def ev(eid, conv, dim, status, ca):
            reg.add_evaluation(ws, eid, conversation_id=conv,
                               evaluator_id="assert.v1", dimension=dim,
                               status=status, created_at=ca)

        # Baseline refund rep: clean.
        ev("e-rb-o", "conv-refund-base", "outcome", "PASS", _DAY1)
        ev("e-rb-p", "conv-refund-base", "policy", "PASS", _DAY1)
        ev("e-rb-s", "conv-refund-base", "speech", "PASS", _DAY1)
        # Current refund rep 1: a policy REGRESSION + a speech INCONCLUSIVE.
        ev("e-r1-o", "conv-refund-1", "outcome", "PASS", _DAY1)
        ev("e-r1-p", "conv-refund-1", "policy", "FAIL", _DAY1)
        ev("e-r1-c", "conv-refund-1", "conversation", "PASS", _DAY1)
        ev("e-r1-s", "conv-refund-1", "speech", "INCONCLUSIVE", _DAY1)
        # Current refund rep 2: policy fails again (a second failing rep).
        ev("e-r2-o", "conv-refund-2", "outcome", "PASS", _DAY1)
        ev("e-r2-p", "conv-refund-2", "policy", "FAIL", _DAY1)
        # Current refund rep 3 (day 2): reliable rep, all pass.
        ev("e-r3-o", "conv-refund-3", "outcome", "PASS", _DAY2)
        ev("e-r3-p", "conv-refund-3", "policy", "PASS", _DAY2)
        # reschedule: outcome FAIL on baseline, PASS on current (a fix).
        ev("e-rsb-o", "conv-resched-base", "outcome", "FAIL", _DAY1)
        ev("e-rs1-o", "conv-resched-1", "outcome", "PASS", _DAY2)
        # check-order-status: two clean reps.
        ev("e-st1-o", "conv-status-1", "outcome", "PASS", _DAY2)
        ev("e-st2-o", "conv-status-2", "outcome", "PASS", _DAY2)

        # A reviewer decision on the regressed policy evaluation.
        reg.add_review(ws, "rev-policy", evaluation_id="e-r1-p", reviewer="reviewer",
                       decision="confirmed-fail",
                       rationale="the cutoff policy was not stated before the refund tool ran",
                       adjudication_state="final")

        # Assertion runs so failure-clusters + the inspector's separate lanes have
        # data: one DETERMINISTIC failure and one MODEL-JUDGED (advisory) failure.
        reg.add_assertion_run(
            ws, assertion_id="required_disclosure", agent_id=_AGENT,
            call_id="conv-refund-1", conversation_id="conv-refund-1",
            kind="required_disclosure", dimension="policy",
            deterministic=True, status="FAIL",
            reason="required disclosure 'refund_policy_v3' not spoken before tool "
                   "issue_refund")
        reg.add_assertion_run(
            ws, assertion_id="clear_explanation", agent_id=_AGENT,
            call_id="conv-refund-1", conversation_id="conv-refund-1",
            kind="judge_rubric", dimension="conversation",
            deterministic=False, status="FAIL",
            reason="the cutoff explanation read as unclear (advisory, model-judged)")
    finally:
        reg.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", default="/data", metavar="DIR",
                    help="registry home to seed (default /data, the container volume)")
    ap.add_argument("--workspace", "-w", default="default", metavar="ID",
                    help="workspace id to seed (default 'default')")
    ap.add_argument("--clear", action="store_true",
                    help="remove the demo agent/rows instead of seeding")
    ap.add_argument("--force", action="store_true",
                    help="re-seed even if the demo agent already exists")
    args = ap.parse_args(argv)

    from hotato.fleet.registry import Registry

    if args.clear:
        reg = Registry(home=args.registry)
        try:
            _clear(reg, args.workspace)
        finally:
            reg.close()
        return 0

    reg = Registry(home=args.registry)
    try:
        seeded = _already_seeded(reg, args.workspace)
    finally:
        reg.close()
    if seeded and not args.force:
        print("seed-demo: workspace %r already has the demo agent %r; nothing to do "
              "(pass --force to re-seed, --clear to remove)."
              % (args.workspace, _AGENT))
        return 0

    seed(args.registry, args.workspace)
    print("seed-demo: seeded EXAMPLE data into workspace %r at %s.\n"
          "  Open the workspace and you should see release readiness, a scenario\n"
          "  matrix, failure clusters, and production health populated.\n"
          "  This is example data -- clear it (--clear) before ingesting real calls."
          % (args.workspace, args.registry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
