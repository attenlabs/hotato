"""hotato verify --out verify.html: the self-contained offline proof artifact.

Covers the load-bearing behaviours of the HTML report:

* --out <PATH>.html writes ONE valid, self-contained HTML file (no external
  assets: no remote src/href, no <link>, no <script>) while --out <PATH>.json
  keeps writing the proof JSON (the extension dispatches);
* the headline reflects the verdict: a clean battery-scale improvement reads
  "Fix verification: PASSED", a regression reads "Fix verification: FAILED",
  and a low-n battery is FAILED (never earns the PASSED stamp);
* both the TARGET (talk-over p95 before -> after) and the OPPOSITE-RISK
  (false-yield before -> after) sections are present;
* the honest conclusion says "coincidence, not causation";
* the rendered output contains no em or en dashes.
"""

from __future__ import annotations

import json

from hotato import cli
from hotato import verify as _verify


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


def _write(tmp_path, name, events):
    p = tmp_path / name
    p.write_text(json.dumps(_env(events)), encoding="utf-8")
    return str(p)


def _passing_sides(tmp_path):
    """Four previously-failing yield fixtures fixed + a hold guard still passing:
    a clean, min-n-supported improvement with no regression -> PASSED."""
    before = _write(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9, 2.1),
        _ev("f3", True, False, 1.5), _ev("f4", True, False, 0.8, 1.9),
        _ev("h1", False, True, 0.0),
    ])
    after = _write(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("f4", True, True, 0.3, 0.4),
        _ev("h1", False, True, 0.0),
    ])
    return before, after


NO_EM = "—"   # em dash
NO_EN = "–"   # en dash


def _assert_self_contained(html: str) -> None:
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    assert "<style>" in html
    # zero external assets: no remote references, no external stylesheet/script
    assert "http://" not in html and "https://" not in html
    assert 'src="http' not in html and 'href="http' not in html
    assert "<link" not in html
    assert "<script" not in html
    # theme-consistent honesty rule for the whole product: no em/en dashes
    assert NO_EM not in html and NO_EN not in html


# --- render_html directly ---------------------------------------------------

def test_render_html_passed_is_self_contained_with_all_sections(tmp_path):
    before, after = _passing_sides(tmp_path)
    v = _verify.verify_sides(before, after, min_n=3)
    html = _verify.render_html(v)

    _assert_self_contained(html)
    # headline reflects pass
    assert "Fix verification: PASSED" in html
    # both required sections present
    assert "Target failure improvement" in html
    assert "talk-over p95" in html
    assert "Opposite-risk check" in html
    assert "false yields" in html
    # honest conclusion
    assert "coincidence, not causation" in html
    assert "timing improved on this battery" in html.lower()
    # the target rollup is a real measured number
    assert "4 of 4" in html


def test_render_html_failed_on_regression(tmp_path):
    # every fixture regressed (talk-over got worse); nothing newly passes
    before = _write(tmp_path, "b.json",
                    [_ev(f"f{i}", True, False, 0.1) for i in range(3)])
    after = _write(tmp_path, "a.json",
                   [_ev(f"f{i}", True, False, 0.9) for i in range(3)])
    v = _verify.verify_sides(before, after, min_n=3)
    html = _verify.render_html(v)

    _assert_self_contained(html)
    assert "Fix verification: FAILED" in html
    assert "Fix verification: PASSED" not in html
    assert "coincidence, not causation" in html
    assert "regressed" in html.lower()


def test_render_html_low_n_is_failed_not_passed(tmp_path):
    # only two previously-failing fixtures fixed, below --min-n 3
    before = _write(tmp_path, "b.json",
                    [_ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9)])
    after = _write(tmp_path, "a.json",
                   [_ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5)])
    v = _verify.verify_sides(before, after, min_n=3)
    html = _verify.render_html(v)
    assert "Fix verification: FAILED" in html
    assert "min-n 3" in html
    assert "coincidence, not causation" in html


def test_opposite_risk_reports_new_false_yields(tmp_path):
    # a fix that trades talk-over for a false yield on a hold guard: the target
    # improves but the opposite-risk guardrail must catch the broken hold fixture
    before = _write(tmp_path, "b.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 1.1),
        _ev("f3", True, False, 1.3),
        _ev("h1", False, True, 0.0),
    ])
    after = _write(tmp_path, "a.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.3, 0.4),
        _ev("h1", False, False, 0.9, 0.2),   # hold guard now yields = false yield
    ])
    v = _verify.verify_sides(before, after, min_n=3)
    m = _verify.verdict_model(v)
    # the guardrail sees the new false yield and the verdict is not PASSED
    assert m["new_false_yields"] == 1
    assert m["passed"] is False
    html = _verify.render_html(v)
    _assert_self_contained(html)
    assert "Fix verification: FAILED" in html
    assert "violated" in html   # the false-yield guardrail is flagged violated


def test_verdict_model_derives_target_and_opposite_risk(tmp_path):
    before, after = _passing_sides(tmp_path)
    v = _verify.verify_sides(before, after, min_n=3)
    m = _verify.verdict_model(v)
    assert m["passed"] is True
    assert m["verdict"] == "PASSED"
    # target: talk-over p95 fell, failing fixtures fell to zero
    assert m["talk_over_p95_after"] < m["talk_over_p95_before"]
    assert m["before_failed"] == 4
    assert m["after_failed"] == 0
    # opposite risk: hold guard kept passing, no new false yields
    assert m["hold_guards"] == 1
    assert m["hold_still_pass"] == 1
    assert m["new_false_yields"] == 0
    assert m["false_yield_before"] == 0
    assert m["false_yield_after"] == 0


# --- CLI: extension dispatch ------------------------------------------------

def test_cli_out_html_writes_report(tmp_path):
    before, after = _passing_sides(tmp_path)
    out = tmp_path / "verify.html"
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    _assert_self_contained(html)
    assert "Fix verification: PASSED" in html


def test_cli_out_json_still_writes_json(tmp_path):
    """The extension dispatch must not break the long-standing JSON --out."""
    before, after = _passing_sides(tmp_path)
    out = tmp_path / "proof.json"
    assert cli.main(["verify", "--before", before, "--after", after,
                     "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["kind"] == "verify"
