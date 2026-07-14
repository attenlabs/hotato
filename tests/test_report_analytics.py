"""P6/P11: analytics block, base comparison deltas, and the Markdown format.

The analytics block must be computed from the same real measurements as the
event cards (nothing invented), the base comparison must mark regressions
clearly per scenario, and the Markdown format must mirror the content with
tables while honouring the same honesty rules (no accuracy percentage, no
em/en dashes, vendor-neutral).
"""

import copy
import json
import re
from importlib import resources

from hotato import cli, report
from hotato.core import run_suite
from tests.test_report import _assert_no_fetched_assets


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


# --- analytics presence -----------------------------------------------------

def test_analytics_block_present_with_all_charts():
    html, env = report.build_report_html(suite="barge-in")
    assert "Analytics" in html
    assert "Time to yield" in html
    assert "Talk-over histogram" in html
    assert "Failure clusters" in html
    # analytics charts are inline SVG with their own class, separate from the
    # per-event timelines
    assert html.count('<svg class="an-svg"') == 2


def test_analytics_renders_after_the_event_cards_it_aggregates():
    html, env = report.build_report_html(suite="barge-in")
    assert env["summary"]["events"] == 8
    analytics_idx = html.index('<section class="card an">')
    last_timeline_idx = html.rindex('<svg class="tl-svg"')
    assert analytics_idx > last_timeline_idx, (
        "the analytics rollup must render after every per-event card, not before"
    )


def test_analytics_skipped_below_three_events():
    # A single-recording report (one event) has nothing for a rollup to add
    # over the card itself, so the analytics section is skipped entirely.
    html, _ = report.build_report_html(stereo=_bundled("01-hard-interruption"))
    assert "Analytics" not in html
    assert '<svg class="an-svg"' not in html
    assert "Failure clusters" not in html


def test_analytics_present_at_exactly_three_events():
    env = run_suite(suite="barge-in")
    env = copy.deepcopy(env)
    env["events"] = env["events"][:3]
    env["summary"]["events"] = 3
    from hotato._engine.score import ScoreConfig

    cfg = ScoreConfig()
    models = [report._event_model(e, [], cfg.hop_ms / 1000.0, cfg)
              for e in env["events"]]
    html = report._render_page(env, models, cfg)
    assert "Analytics" in html
    assert '<svg class="an-svg"' in html


def test_latency_strip_uses_real_measurements():
    html, env = report.build_report_html(suite="barge-in")
    # every measured seconds_to_yield appears in a strip dot tooltip
    for e in env["events"]:
        tty = e["verdict"]["seconds_to_yield"]
        if tty is not None:
            assert f"{tty:.2f}s" in html
    # the distribution caption states n and the order statistics
    n = sum(1 for e in env["events"] if e["verdict"]["seconds_to_yield"] is not None)
    assert f"n={n}" in html
    assert "median" in html and "p90" in html


def test_frame_inspector_per_event_collapsible():
    html, env = report.build_report_html(suite="barge-in")
    # one collapsible inspector per event that has frame data (all 8 bundled)
    assert html.count('<details class="inspector">') == env["summary"]["events"]
    assert "frame inspector" in html
    # table columns from the frame dump
    for col in ("t (s)", "caller dBFS", "agent dBFS", "caller active",
                "agent active", "caller thr dB", "agent thr dB"):
        assert col in html, f"missing frame inspector column {col!r}"
    assert "noise floor" in html


def test_frame_inspector_rows_match_frame_dump():
    from hotato.core import dump_frames_for_input

    wav = _bundled("01-hard-interruption")
    html, env = report.build_report_html(stereo=wav)
    dump = dump_frames_for_input(stereo=wav)
    assert f"frame inspector: {len(dump['frames'])} frames" in html


def test_analytics_keeps_report_self_contained_and_honest():
    html, _ = report.build_report_html(suite="barge-in")
    assert "%" not in html
    _assert_no_fetched_assets(html)
    assert "xmlns" not in html
    assert "–" not in html and "—" not in html


# --- base comparison ---------------------------------------------------------

def test_base_comparison_same_run_marks_all_same():
    base = run_suite(suite="barge-in")
    html, _ = report.build_report_html(suite="barge-in", base=base,
                                       base_label="base.json")
    assert "Vs base (base.json)" in html
    assert html.count(">SAME<") == base["summary"]["events"]
    assert ">WORSE<" not in html and ">BETTER<" not in html


def test_base_comparison_marks_worse_and_better():
    env = run_suite(suite="barge-in")
    base = copy.deepcopy(env)
    # simulate a historical base where scenario 01 had LESS talk-over (so the
    # current run is worse) and scenario 06 had a SLOWER yield (so the current
    # run is better). The deltas are then real subtractions.
    e01 = next(e for e in base["events"] if e["scenario_id"] == "01-hard-interruption")
    e01["verdict"]["talk_over_sec"] = round(e01["verdict"]["talk_over_sec"] - 0.30, 3)
    e06 = next(e for e in base["events"] if e["scenario_id"] == "06-double-talk")
    e06["verdict"]["seconds_to_yield"] = round(
        (e06["verdict"]["seconds_to_yield"] or 0.0) + 0.40, 3)

    html, _ = report.build_report_html(suite="barge-in", base=base)
    assert ">WORSE<" in html
    assert ">BETTER<" in html
    assert "+0.30s" in html   # talk-over grew by exactly the injected delta
    assert "-0.40s" in html   # yield got faster by exactly the injected delta


def test_base_comparison_pass_transition_dominates():
    env = run_suite(suite="barge-in")
    base = copy.deepcopy(env)
    e = base["events"][0]
    e["verdict"]["passed"] = False   # base failed, current passes -> better
    html, _ = report.build_report_html(suite="barge-in", base=base)
    assert ">BETTER<" in html
    assert "FAIL to" in html


def test_base_comparison_flags_new_and_missing_scenarios():
    env = run_suite(suite="barge-in")
    base = copy.deepcopy(env)
    removed = base["events"].pop(0)
    ghost = copy.deepcopy(base["events"][0])
    ghost["scenario_id"] = ghost["event_id"] = "99-retired-scenario"
    base["events"].append(ghost)
    html, _ = report.build_report_html(suite="barge-in", base=base)
    assert ">NEW<" in html
    assert "99-retired-scenario" in html
    assert "in base but not in this run" in html


def test_report_cli_base_flag_renders_deltas(tmp_path):
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(run_suite(suite="barge-in")), encoding="utf-8")
    out = tmp_path / "report.html"
    code = cli.main(["report", "--suite", "barge-in", "--base", str(base_path),
                     "--out", str(out)])
    assert code == 0
    html = out.read_text(encoding="utf-8")
    assert "Vs base (base.json)" in html
    assert ">SAME<" in html
    assert "%" not in html


def test_report_cli_base_rejects_non_envelope(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": "world"}', encoding="utf-8")
    code = cli.main(["report", "--suite", "barge-in", "--base", str(bad),
                     "--out", str(tmp_path / "r.html")])
    assert code == 2


def test_report_cli_base_missing_file_is_usage_error(tmp_path):
    code = cli.main(["report", "--suite", "barge-in",
                     "--base", str(tmp_path / "nope.json"),
                     "--out", str(tmp_path / "r.html")])
    assert code == 2


# --- markdown format ----------------------------------------------------------

def test_md_report_mirrors_content():
    md, env = report.build_report_md(suite="barge-in")
    assert md.startswith("# hotato report")
    assert "## Summary" in md
    assert "## Analytics" in md
    assert "### Talk-over histogram" in md
    assert "## Thresholds used" in md
    assert "## Method and limits" in md
    # every event and its real measurements appear as table rows
    for e in env["events"]:
        assert (e.get("scenario_id") or e["event_id"]) in md
        tov = e["verdict"]["talk_over_sec"]
        if tov is not None:
            assert f"{tov:.2f}s" in md
    assert f"**{env['summary']['passed']} of {env['summary']['events']} events pass.**" in md


def test_md_report_is_honest_plain_text():
    md, _ = report.build_report_md(suite="barge-in")
    assert "%" not in md
    assert re.search(r"\d\s*%", md) is None
    assert "–" not in md and "—" not in md
    assert "<svg" not in md and "<script" not in md
    assert "No accuracy score" in md


def test_md_report_with_base_deltas():
    base = run_suite(suite="barge-in")
    md, _ = report.build_report_md(suite="barge-in", base=base,
                                   base_label="base.json")
    assert "## Vs base (base.json)" in md
    assert "| SAME |" in md


def test_report_cli_format_md_default_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["report", "--suite", "barge-in", "--format", "md"])
    assert code == 0
    out = tmp_path / "hotato-report.md"
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("# hotato report")


def test_report_cli_format_md_explicit_out(tmp_path):
    out = tmp_path / "r.md"
    code = cli.main(["report", "--suite", "barge-in", "--format", "md",
                     "--out", str(out)])
    assert code == 0
    md = out.read_text(encoding="utf-8")
    assert "## Analytics" in md and "%" not in md


def test_html_report_ships_print_css_for_future_pdf():
    html, _ = report.build_report_html(suite="barge-in")
    assert "@media print" in html
