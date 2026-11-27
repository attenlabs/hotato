import datetime as dt
import json

import builder
import common
import freshness
import leakscan
import make_hashes
import policy
import reviewer
import scorecard
import upkeep

UTC = dt.timezone.utc
AT = dt.datetime(2026, 11, 26, 12, tzinfo=UTC)
GREEN = {c: "success" for c in ["test", "leak-scan", "benchmark", "copy-check"]}


# --- gates
def test_docs_auto_merges_outside_shadow():
    d = policy.decide(["docs/a.md"], GREEN, approved=True, shadow=False)
    assert d["decision"] == "auto_merge" and d["class"] == "docs"


def test_shadow_mode_blocks_auto_merge():
    assert policy.decide(["docs/a.md"], GREEN, approved=True)["decision"] == "owner"


def test_gate_files_are_owner_only_even_mixed_with_docs():
    d = policy.decide(["docs/a.md", "autopilot/gates/policy.json"], GREEN, approved=True, shadow=False)
    assert d["decision"] == "owner" and d["class"] == "owner_only"


def test_unknown_path_is_feature_needs_owner():
    d = policy.decide(["src/hotato/new.py"], GREEN, approved=True, shadow=False)
    assert d["decision"] == "owner" and d["class"] == "feature"


def test_red_check_or_no_review_blocks():
    bad = dict(GREEN, benchmark="failure")
    assert policy.decide(["docs/a.md"], bad, approved=True, shadow=False)["decision"] == "blocked"
    assert policy.decide(["docs/a.md"], GREEN, approved=False, shadow=False)["decision"] == "blocked"


def test_pricing_label_forces_owner():
    d = policy.decide(["docs/a.md"], GREEN, ["pricing"], True, shadow=False)
    assert d["decision"] == "owner"


def test_glob():
    assert policy.glob_re("tests/**").match("tests/a/b.py")
    assert not policy.glob_re("*.md").match("docs/a.md")


# --- scanner
def test_scanner_hash_only(tmp_path):
    hashes = set(make_hashes.hash_lines(["Secret Project", "Zed"]))
    assert all(len(h) == 64 for h in hashes)
    assert leakscan.scan_text("we ship the SECRET   project today", hashes)[0][0] == 1
    assert leakscan.scan_text("zed's tool", hashes)
    assert not leakscan.scan_text("secret sauce project", hashes)
    assert not leakscan.scan_text("anything", set())


def test_empty_hash_file_is_clean(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    assert leakscan.main(["--hashes", str(tmp_path / "none"), str(f)]) == 0


def test_helper_rejects_long_token():
    import pytest
    with pytest.raises(ValueError):
        make_hashes.hash_lines(["a b c d"])


# --- freshness
def test_versions():
    ok, _ = freshness.check_version("Install version 0.4.0 now", "0.4.0")
    assert ok
    ok, d = freshness.check_version("hotato==0.3.1", "0.4.0")
    assert not ok and d["bad"] == ["0.3.1"]
    assert not freshness.check_version("no version here", "0.4.0")[0]


def test_figures_and_age():
    assert freshness.check_figures({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ["b"]
    assert freshness.check_figures({"c": 1}, {}) == ["c"]
    assert freshness.atlas_fresh("2026-11-25T14:00:00Z", AT)
    assert not freshness.atlas_fresh("2026-11-24T00:00:00Z", AT)


def test_stale_issues_and_red():
    issues = [{"number": 1, "created_at": "2026-11-01T00:00:00Z", "comments": 0},
              {"number": 2, "created_at": "2026-11-01T00:00:00Z", "comments": 2},
              {"number": 3, "created_at": "2026-11-25T00:00:00Z", "comments": 0},
              {"number": 4, "created_at": "2026-11-01T00:00:00Z", "comments": 0, "pull_request": {}}]
    assert freshness.stale_issues(issues, AT) == [1]
    assert freshness.red_main([{"name": "t", "conclusion": "failure"}, {"name": "u", "conclusion": "success"}]) == ["t"]


def test_sitemap_and_md():
    xml = '<urlset xmlns="x"><url><loc>https://h.dev/</loc></url></urlset>'
    assert freshness.sitemap_current(xml, ["https://h.dev/"]) == []
    assert freshness.sitemap_current(xml, ["https://h.dev/a"]) == ["https://h.dev/a"]
    md = "```bash\n$ hotato run --help\nls\n```\n[x](https://a.b/c)"
    assert freshness.readme_commands(md) == ["hotato run --help"]
    assert freshness.links(md) == ["https://a.b/c"]


# --- scorecard
def test_scorecard_math():
    assert scorecard.pct_change(110, 100) == 0.1 and scorecard.pct_change(1, 0) is None
    assert scorecard.falling_two_weeks([5, 4, 3]) and not scorecard.falling_two_weeks([5, 4, 4])
    assert scorecard.release_pace(["2026-11-20T00:00:00Z", "2025-01-01T00:00:00Z"], AT) == 1 * 30 / 90
    assert scorecard.proven_fit([.4] * 8, .3) and not scorecard.proven_fit([.4] * 7, .3)
    assert not scorecard.proven_fit([.4] * 7 + [.1], .3)
    assert scorecard.new_competitor_features(["a"], ["a", "b"]) == ["b"]
    assert scorecard.pull_score(3, 2, 1) == 12


def _backlog_md():
    import os
    return open(os.path.join(os.path.dirname(__file__), "..", "BACKLOG.md")).read()


def test_backlog_reorder_roundtrip():
    head, items = scorecard.parse_backlog(_backlog_md())
    assert len(items) == 38
    new = scorecard.reorder(items, top="human review queue")
    assert new[0].startswith("W2.6a ") and sorted(new) == sorted(items)
    assert scorecard.reorder(items) == items
    assert scorecard.parse_backlog(scorecard.render_backlog(head, new))[1] == new


def test_backlog_items_fit_a_pr_title_and_builder_takes_the_first():
    md = _backlog_md()
    _, items = scorecard.parse_backlog(md)
    assert items and all(len(t) <= 250 for t in items)
    assert builder.top_item(md) == items[0] and items[0].startswith("W2.1a ")


def test_every_measure_maps_to_exactly_one_backlog_item():
    import os
    path = os.path.join(os.path.dirname(scorecard.__file__), "measure_items.json")
    serves = json.load(open(path))
    _, items = scorecard.parse_backlog(_backlog_md())
    assert sorted(serves) == sorted(scorecard.MEASURES)
    for measure, sub in serves.items():
        hits = [t for t in items if sub.lower() in t.lower()]
        assert len(hits) == 1, (measure, sub, hits)


# --- upkeep
def test_upkeep():
    pins = upkeep.parse_pins("requests==2.0.0\n# c\nfoo==1.9.0 ; x\nbar>=1")
    assert pins == {"requests": "2.0.0", "foo": "1.9.0"}
    assert upkeep.outdated(pins, {"requests": "2.10.0", "foo": "1.9.0"}) == {"requests": ("2.0.0", "2.10.0")}
    assert upkeep.drifted("a", "b") and not upkeep.drifted(None, "b") and not upkeep.drifted("a", "a")


# --- builder / reviewer
def test_builder_logic():
    assert builder.top_item("# x\n1. [x] done\n2. next one\n3. later") == "next one"
    assert builder.slug("Slack/PagerDuty alerting!") == "slack-pagerduty-alerting"
    assert builder.forbidden_paths(["docs/a.md", "autopilot/BACKLOG.md", "autopilot/gates/policy.py", "autopilot/jobs/x.py"]) == ["autopilot/gates/policy.py", "autopilot/jobs/x.py"]
    import os
    os.environ["GITHUB_BOT_TOKEN"] = "x"
    assert "GITHUB_BOT_TOKEN" not in builder.agent_env()


def test_reviewer_fails_closed():
    assert reviewer.parse_verdict('x {"verdict":"approve","reasons":["ok"]} y')[0] == "approve"
    assert reviewer.parse_verdict("looks good")[0] == "changes"
    assert reviewer.parse_verdict('{"verdict":"maybe"}')[0] == "changes"
    pr = {"labels": [{"name": "autopilot"}], "head": {"sha": "a"}}
    assert reviewer.needs_review(pr, []) and not reviewer.needs_review(pr, ["a"])


# --- kill switch and run record
def test_kill_switch_skips_work(tmp_path):
    s = common.Store(local_dir=str(tmp_path))
    s.put_bytes(common.KILL_KEY, b"1")
    called = []
    assert common.run_job("t", lambda st, r: called.append(1), s) == 0
    assert not called
    rec = json.loads(next((tmp_path / "runs" / "t").iterdir()).read_text())
    assert rec["status"] == "killed"


def test_run_record_and_error(tmp_path):
    s = common.Store(local_dir=str(tmp_path))
    assert common.run_job("ok", lambda st, r: {"n": 1}, s) == 0

    def boom(st, r):
        raise RuntimeError("x")
    import pytest
    with pytest.raises(RuntimeError):  # job_crash is alerted, recorded, then re-raised
        common.run_job("bad", boom, s)
    rec = json.loads(next((tmp_path / "runs" / "bad").iterdir()).read_text())
    assert rec["status"] == "error"


def test_gemini_engine_interface():
    import sys
    sys.path.insert(0, "jobs")
    import atlas_job as job

    class R:
        text = '{"x":1}'

    class C:
        class models:
            @staticmethod
            def generate_content(**kw):
                return R
    e = job.GeminiEngine(client=C)
    assert e.name and e.run("p", {}) == '{"x":1}'


def test_budget_function():
    import sys
    sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "infra", "function"))
    import main as fn
    assert fn.should_kill({"costAmount": 100, "budgetAmount": 100})
    assert not fn.should_kill({"costAmount": 99, "budgetAmount": 100})
    assert not fn.should_kill({})
    assert fn.should_kill({"budgetDisplayName": "hotato monthly", "costAmount": 9, "budgetAmount": 5}, "sprint") is False
    assert fn.should_kill({"budgetDisplayName": "hotato sprint (6 weeks)", "costAmount": 9, "budgetAmount": 5}, "sprint")


def test_simulate_plan_allowlist_and_budget():
    import simulate
    ps = [{"name": f"p{i}"} for i in range(5)]
    calls, refused = simulate.plan_calls(ps, ["+1", "+2"], ["+1"], 1.0, 3.0)
    assert len(calls) == 3 and all(c["to"] == "+1" for c in calls) and refused == ["+2"]
    assert simulate.plan_calls(ps, ["+2"], ["+1"], 1.0, 9)[0] == []
    assert simulate.parse_personas('x [{"name":"a","goal":"g"},{"nope":1}] y', 5)[0]["name"] == "a"
    assert simulate.parse_personas("garbage", 5) == []


def test_spend_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("PHASE", "sprint")
    s = common.Store(local_dir=str(tmp_path))
    assert common.debit(s, "sim", 6, 10) and not common.debit(s, "sim", 6, 10)
    assert not common.debit(s, "sim", 1, 0)   # a zero cap never spends
    assert common.debit(s, "sim", 4, 10)


def test_benchmark_regressions():
    import benchmark_check as b
    base = {"higher": {"f1": 0.9}, "lower": {"ms": 100}}
    assert b.regressions(base, {"f1": 0.9, "ms": 100}) == []
    assert len(b.regressions(base, {"f1": 0.8, "ms": 120})) == 2
    assert b.regressions(base, {"f1": 0.9}) == ["ms: None > baseline 100"]
    assert b.regressions(base, {"f1": 0.88, "ms": 100}, tol=0.05) == []


def test_copycheck():
    import copycheck
    assert copycheck.find("Coming soon!\nfine") == [(1, "Coming soon")]
    assert copycheck.find("all good") == []


def test_benchmark_flatten_reads_hotato_aggregate():
    import benchmark_check as b
    agg = {"error_stats_ms": {"onset_sec": {"n": 15, "max_ms": 10.0}},
           "confusion": {"missed_yield": 1, "ok": True}, "sets": [1, 2]}
    assert b.flatten(agg) == {"error_stats_ms.onset_sec.n": 15,
                              "error_stats_ms.onset_sec.max_ms": 10.0,
                              "confusion.missed_yield": 1}


def test_dot_directories_stay_owner_only():
    cfg = policy.load_config()
    for f in [".github/workflows/a.yml", "./.github/workflows/a.yml", "/.github/x", "./autopilot/gates/policy.json"]:
        assert policy.classify_file(f, cfg) == "owner_only", f
    assert builder.forbidden_paths([".github/workflows/tests.yml"]) == [".github/workflows/tests.yml"]
    assert policy.classify_file("./docs/a.md", cfg) == "docs"
