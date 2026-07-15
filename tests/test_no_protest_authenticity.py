"""Adversarial regression: no protest-authenticity wording ("honestly",
"not fabricated", "genuine(ly)", "no fabrication") in shipped reliability
copy (R-10).

The deterministic lane has zero run-to-run variance by construction (a
scripted replay is byte-identical), which is a plain factual property -- it
does not need a reader to be reassured that the number is "honest" or "not
fabricated". Insisting on it reads as protest-too-much and, per the fleet's
show-what-IS / no-protest-authenticity conventions, is stripped in favor of
stating the mechanism directly ("byte-identical", "zero variance").

These pin the actual shipped surfaces that emit the reliability note:
``hotato.simulate.reliability()``, ``hotato.test_run``'s per-run reliability
aggregate (CLI json), and ``hotato simulate --help``. All three FAIL against
the pre-fix strings (which contained "reported honestly, not fabricated
variance") and PASS once the note is restated factually.
"""

import json
import re

import pytest

from hotato import cli
from hotato import simulate as SIM

_PROTEST_RE = re.compile(
    r"\b(honestly|not fabricated|genuine(ly)?|no fabrication)\b", re.IGNORECASE
)


def _write_test(tmp_path, *, name="t1", agent="a", deterministic=None,
                repetitions=None):
    doc = {
        "kind": "hotato.conversation-test", "version": 1, "id": name,
        "agent": agent,
        "assertions": {"deterministic": deterministic or []},
    }
    if repetitions is not None:
        doc["repetitions"] = repetitions
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _demo_trace():
    import os
    return os.path.join(os.path.dirname(__file__), "data", "conversation",
                        "refund.voice_trace.jsonl")


@pytest.fixture(autouse=True)
def _no_judge_daemon(monkeypatch, tmp_path):
    # Keep the (advisory, unrelated) rubric lane from reaching out anywhere.
    monkeypatch.setenv("HOTATO_JUDGE_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_simulate_reliability_note_has_no_protest_authenticity_wording():
    # A small deterministic fixture: every run passes (a seeded replay is
    # byte-identical for the scripted caller), so pass^k == pass@1.
    agg = SIM.reliability([True, True, True])
    note = agg["note"]
    assert agg["pass_at_1"] == agg["pass_caret_k"] == 1.0
    hit = _PROTEST_RE.search(note)
    assert hit is None, f"protest-authenticity wording in simulate.reliability() note: {hit.group(0)!r} ({note!r})"


def test_test_run_reliability_note_via_capsys(tmp_path, capsys):
    tf = _write_test(
        tmp_path, name="reps2",
        deterministic=[{"id": "refunded", "kind": "tool_call",
                        "name": "issue_refund", "dimension": "outcome"}],
    )
    code = cli.main(["test", "run", tf, "--agent", "a", "--trace", _demo_trace(),
                     "--repetitions", "3", "--format", "json"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    note = result["reliability"]["aggregate"]["note"]
    hit = _PROTEST_RE.search(note)
    assert hit is None, f"protest-authenticity wording in test_run reliability note: {hit.group(0)!r} ({note!r})"


def test_simulate_help_has_no_protest_authenticity_wording(capsys):
    with pytest.raises(SystemExit):
        cli.main(["simulate", "--help"])
    out = capsys.readouterr().out
    assert "reported honestly" not in out
    assert "not fabricated" not in out.lower()
