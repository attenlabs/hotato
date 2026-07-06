"""scripts/pr_comment.py renders the bundled suite envelope into an honest,
deterministic Markdown comment: the counts survive, and nothing the honesty
invariants forbid (an accuracy percentage, a named vendor) leaks in.
"""

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "suite_barge-in.json"
SCRIPT = ROOT / "scripts" / "pr_comment.py"

# scripts/ is a plain directory, not a package, so load the module by path.
_spec = importlib.util.spec_from_file_location("pr_comment", SCRIPT)
pr_comment = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_comment)


def _envelope():
    return json.loads(GOLDEN.read_text())


def test_counts_are_present():
    env = _envelope()
    body = pr_comment.render_markdown(env)
    s = env["summary"]

    # The pass/fail line carries the real counts, strengths first.
    assert f"{s['passed']} of {s['events']} scenarios pass" in body
    assert f"{s['failed']} fail" in body
    assert ("No regression" in body) == (not s["regression"])

    # Every scenario shows up as a table row.
    for event in env["events"]:
        assert event["event_id"] in body

    # The hidden sticky marker leads the comment.
    assert body.startswith(pr_comment.MARKER)
    assert "### Regressions" in body


def test_no_accuracy_percentage_or_vendor_name():
    body = pr_comment.render_markdown(_envelope())

    # Honesty invariant: this tool never publishes an accuracy percentage.
    assert "%" not in body
    assert "accuracy" not in body.lower()

    # No specific voice vendor is ever named in a result.
    lowered = body.lower()
    for vendor in ("vapi", "twilio", "livekit", "pipecat", "retell"):
        assert vendor not in lowered


def test_no_em_or_en_dashes():
    body = pr_comment.render_markdown(_envelope())
    assert "—" not in body  # em dash
    assert "–" not in body  # en dash


def test_deterministic():
    env = _envelope()
    assert pr_comment.render_markdown(env) == pr_comment.render_markdown(env)


def test_base_delta_flags_slower_scenario():
    env = _envelope()

    # A baseline where one scenario yielded faster and overlapped less: the head
    # run is slower on both axes, so it must land in the regressions section.
    base = json.loads(GOLDEN.read_text())
    target = base["events"][0]["event_id"]
    base["events"][0]["verdict"]["talk_over_sec"] = 0.10
    base["events"][0]["verdict"]["seconds_to_yield"] = 0.10

    body = pr_comment.render_markdown(env, base=base)
    assert target in body
    assert "vs base" in body
