"""The Atlas pipeline in the cloud: state that survives corruption and races, a publish guard
that never ships a quietly smaller Atlas, deploys that follow the live site release, collector
versions that ship and roll back on their own, and the outside watch.

Every test runs offline: the state bucket is a local directory, the site is a temp git repo, and
wrangler, Cloudflare and Gemini are fakes."""
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap

import pytest

import common
import atlas_collector as collector
import atlas_guard as guard
import atlas_site as site
import atlas_state as state
import atlas_watch as watch
import atlas_wheel as wheel

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INFRA = os.path.join(ROOT, "infra")


def read(*p):
    return open(os.path.join(ROOT, *p)).read()


@pytest.fixture
def store(tmp_path):
    return common.Store(local_dir=str(tmp_path / "bucket"))


def make_db(path, entries=3):
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE reports(id TEXT); CREATE TABLE patterns(id TEXT);"
                       "CREATE TABLE mentions(id TEXT); CREATE TABLE checks(id TEXT);")
    conn.executemany("INSERT INTO patterns VALUES (?)", [(f"p{i}",) for i in range(entries)])
    conn.commit()
    conn.close()
    return path


# ---- state -------------------------------------------------------------------------

def test_writes_are_generation_checked(store):
    o = state.Objects(store)
    g1 = o.write("atlas/x", b"1", 0)
    with pytest.raises(state.Conflict):
        o.write("atlas/x", b"2", 0)  # "only if absent" on an existing object
    g2 = o.write("atlas/x", b"2", g1)
    with pytest.raises(state.Conflict):
        o.write("atlas/x", b"3", g1)  # someone else wrote since g1
    assert o.read("atlas/x") == (b"2", g2) and o.generations("atlas/x") == [g2, g1]


def test_lock_is_exclusive_expires_and_releases(store, monkeypatch):
    t = [dt.datetime(2026, 11, 27, 12, tzinfo=dt.timezone.utc)]
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "exec-a")
    a = state.Lock(state.Objects(store), "atlas", 600, at=lambda: t[0])
    assert a.try_acquire() is None
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "exec-b")
    b = state.Lock(state.Objects(store), "atlas-recheck", 600, at=lambda: t[0])
    assert b.try_acquire()["holder"] == "atlas"  # held
    t[0] += dt.timedelta(seconds=601)
    assert b.try_acquire() is None  # expired: taken over, with an alert
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "exec-a")
    a.release()  # a's generation is gone: it must not delete b's lock
    assert json.loads(state.Objects(store).read(state.LOCK_KEY)[0])["holder"] == "atlas-recheck"
    b.release()
    assert state.Objects(store).read(state.LOCK_KEY) == (None, 0)


def test_corrupt_store_is_restored_from_an_earlier_generation(store, tmp_path, capsys):
    o = state.Objects(store)
    good = make_db(str(tmp_path / "good.db"))
    g1 = o.write_file(state.DB_KEY, good, 0)
    o.write(state.DB_KEY, b"garbage, not sqlite", g1)
    path = str(tmp_path / "run.db")
    gen = state.fetch_db(store, path, "atlas")
    assert state.db_problem(path) is None and gen == o.generation(state.DB_KEY)
    assert "atlas_db_restored" in capsys.readouterr().out


def test_snapshot_is_the_second_line_of_restore(store, tmp_path):
    good = make_db(str(tmp_path / "good.db"))
    assert state.snapshot_daily(store, good)
    assert state.snapshot_daily(store, good) is None  # once per day
    path = str(tmp_path / "run.db")
    assert state.fetch_db(store, path, "atlas") > 0  # no live object at all: the snapshot
    assert state.db_problem(path) is None


def test_nothing_sound_fails_closed_unless_the_owner_allows_an_empty_start(store, tmp_path):
    with pytest.raises(state.StateError):
        state.fetch_db(store, str(tmp_path / "run.db"), "atlas")
    store.put_bytes(state.FLAG_PREFIX + "allow-empty-start", b"")
    assert state.fetch_db(store, str(tmp_path / "run.db"), "atlas") == 0


def test_an_unusable_store_is_never_uploaded(store, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"nope")
    with pytest.raises(state.StateError):
        state.save_db(store, str(bad), 0)
    empty = make_db(str(tmp_path / "empty.db"), entries=0)
    with pytest.raises(state.StateError, match="no entries"):
        state.save_db(store, empty, 0)


# ---- the publish guard ----------------------------------------------------------------

def feed(ids, reports=100, withdrawn=0, merged=0, as_of="2026-11-27T10:00:00Z"):
    return json.dumps({"data_as_of": as_of, "entries": [{"id": i} for i in ids],
                       "counts": {"reports": reports, "withdrawn": withdrawn, "merged": merged,
                                  "patterns": len(ids)}}).encode()


def pages(tmp_path, ids, name="pages", **kw):
    d = tmp_path / name
    d.mkdir()
    (d / "feed.json").write_bytes(feed(ids, **kw))
    (d / "index.html").write_text("<h1>Atlas</h1>")
    for i in ids:
        (d / i).mkdir()
        (d / i / "index.html").write_text(f"<h1>{i}</h1>")
    return str(d)


IDS = [f"AT-{i:03d}" for i in range(20)]


def test_guard_passes_growth_and_refuses_a_quiet_shrink(tmp_path):
    old = guard.manifest(pages(tmp_path, IDS))
    assert guard.check(guard.manifest(pages(tmp_path, IDS + ["AT-999"], "grew")), old) == (True, None, [])
    ok, kind, why = guard.check(guard.manifest(pages(tmp_path, IDS[:15], "shrunk")), old)
    assert not ok and kind == guard.SHRINK and "5 published entries would disappear" in why[0]


def test_withdrawals_and_merges_explain_removed_entries(tmp_path):
    old = guard.manifest(pages(tmp_path, IDS))
    new = guard.manifest(pages(tmp_path, IDS[:15], "w", withdrawn=3, merged=2))
    assert guard.check(new, old)[0]


def test_a_falling_report_count_is_a_shrink(tmp_path):
    old = guard.manifest(pages(tmp_path, IDS, reports=1000))
    ok, kind, _ = guard.check(guard.manifest(pages(tmp_path, IDS, "r", reports=800)), old)
    assert (ok, kind) == (False, guard.SHRINK)


def test_allow_shrink_lets_one_deliberate_shrink_through(tmp_path):
    old = guard.manifest(pages(tmp_path, IDS))
    assert guard.check(guard.manifest(pages(tmp_path, IDS[:10], "s")), old, allow_shrink=True)[0] is False
    new = guard.manifest(pages(tmp_path, IDS[:19], "t"))
    assert guard.check(new, old, allow_shrink=True)[0]


def test_broken_pages_are_a_structure_refusal(tmp_path):
    old = guard.manifest(pages(tmp_path, IDS))
    d = pages(tmp_path, IDS, "nofeed")
    os.remove(os.path.join(d, "feed.json"))
    ok, kind, why = guard.check(guard.manifest(d), old)
    assert (ok, kind) == (False, guard.STRUCTURE) and "feed.json" in why[0]
    ok, kind, why = guard.check(guard.manifest(pages(tmp_path, IDS, "pf")), old,
                                preflight_problems=["atlas/x/index.html: no <h1>"])
    assert (ok, kind) == (False, guard.STRUCTURE)


def test_first_publish_compares_with_the_live_feed(tmp_path):
    base = guard.baseline(None, feed(IDS))
    assert base["source"] == "live" and base["pages"] == len(IDS) + 2
    assert guard.check(guard.manifest(pages(tmp_path, IDS[:5])), base)[1] == guard.SHRINK
    assert guard.baseline(None, None) is None


def test_stylesheet_missing_from_the_site_is_caught(tmp_path):
    d = tmp_path / "site" / "atlas"
    d.mkdir(parents=True)
    (d / "index.html").write_text('<link rel="stylesheet" href="/assets/hotato.0123456789.css">')
    assert guard.stylesheet_problems(str(d), str(tmp_path / "site"))
    (tmp_path / "site" / "assets").mkdir()
    (tmp_path / "site" / "assets" / "hotato.0123456789.css").write_text("x")
    assert guard.stylesheet_problems(str(d), str(tmp_path / "site")) == []


def test_needs_deploy(tmp_path):
    new = guard.manifest(pages(tmp_path, IDS))
    assert guard.needs_deploy(new, None, "")[0]
    last = dict(new)
    assert guard.needs_deploy(new, last, new["feed_sha256"]) == (False, "live site already serves this render")
    assert guard.needs_deploy(new, last, "other")[0]  # a site deploy replaced our pages: redeploy


# ---- collector versions ------------------------------------------------------------------

def test_choices_prefer_the_candidate_and_skip_bad_versions():
    doc = {"candidate": "0.3.0", "current": "0.2.0", "bad": ["0.3.0"]}
    assert collector.choices(doc, "0.1.0") == [("0.2.0", "current"), ("0.1.0", "baked")]
    assert collector.choices({"candidate": "0.3.0", "current": "0.2.0"}, "0.2.0") == [
        ("0.3.0", "candidate"), ("0.2.0", "current")]
    assert collector.choices({"candidate": "../x"}, None) == []


def test_a_candidate_that_will_not_install_is_rejected_and_the_run_falls_back(store, monkeypatch, capsys):
    store.put_json(collector.KEY, {"candidate": "9.9.9", "current": None})
    monkeypatch.setattr(collector, "baked", lambda: "0.2.0")
    monkeypatch.setattr(collector, "loaded_version", lambda: "0.2.0")

    def boom(version, target):
        raise RuntimeError("no such version")
    assert collector.select(store, "atlas", install=boom) == {"version": "0.2.0", "role": "baked"}
    doc = store.get_json(collector.KEY)
    assert doc["candidate"] is None and doc["bad"] == ["9.9.9"]
    assert "atlas_collector_rolled_back" in capsys.readouterr().out


def test_settle_promotes_a_clean_candidate_and_rejects_a_crashed_one(store):
    store.put_json(collector.KEY, {"candidate": "0.3.0", "current": "0.2.0"})
    assert collector.settle(store, {"version": "0.3.0", "role": "candidate"}, ok=True) == "promoted"
    assert store.get_json(collector.KEY)["current"] == "0.3.0"
    collector.offer(store, "0.4.0")
    assert collector.settle(store, {"version": "0.4.0", "role": "candidate"}, ok=False) == "rejected"
    doc = store.get_json(collector.KEY)
    assert doc["current"] == "0.3.0" and "0.4.0" in doc["bad"]
    collector.offer(store, "0.4.0")  # a rejected version is never offered again
    assert store.get_json(collector.KEY)["candidate"] is None
    assert collector.settle(store, {"version": "0.3.0", "role": "current"}, ok=False) is None


def test_runtime_install_never_puts_a_credential_on_the_command_line(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv

        class CP:
            returncode = 0
            stderr = ""
        return CP
    monkeypatch.setenv("GCP_PROJECT", "proj")
    collector.pip_install("0.2.0", "/tmp/x", run=run)
    argv = " ".join(seen["argv"])
    assert "--keyring-provider import" in argv and "@" not in argv.split("--index-url")[1].split()[0]


# ---- the site: follow the live release --------------------------------------------------

def test_live_source_reads_the_production_deployment(monkeypatch):
    project = {"production_branch": "main", "canonical_deployment": {
        "id": "d1", "created_on": "2026-11-26T10:00:00Z",
        "deployment_trigger": {"metadata": {"commit_hash": "a" * 40, "branch": "main"}}}}
    monkeypatch.setattr(site, "cf_get", lambda path, token: project)
    live = site.live_source("t", "acct", "hotato-site")
    assert live == {"production_branch": "main", "commit": "a" * 40, "deployment_id": "d1",
                    "created_on": "2026-11-26T10:00:00Z"}


def test_checkout_refuses_a_deployment_without_a_full_commit(tmp_path):
    with pytest.raises(site.SiteError, match="no full source commit"):
        site.checkout("o/site", "", str(tmp_path / "x"), "tok")


def test_git_credential_travels_in_the_environment_only():
    env = site.git_env("secret-token-value")
    assert all("secret-token-value" not in v for v in env.values())  # base64, in a header value
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_live_mismatch_is_detected(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "llms.txt").write_bytes(b"llms")
    (d / "index.html").write_text('<link rel="stylesheet" href="/assets/hotato.abcdef1234.css">')
    live = {"https://h/llms.txt": b"llms", "https://h/": b'href="/assets/hotato.abcdef1234.css"'}
    assert site.live_mismatches(str(d), "https://h", live.get) == []
    live["https://h/"] = b'href="/assets/hotato.9999999999.css"'
    assert site.live_mismatches(str(d), "https://h", live.get) == ["the live home page links a different stylesheet"]


STAGE = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
STAGE="$(mktemp -d)"
cp -r atlas index.html "$STAGE/"
{refuse}
echo "stage: $(find "$STAGE" -type f | wc -l) files"
npx wrangler pages deploy "$STAGE" --project-name {project} --commit-dirty=true "$@"
"""


def site_repo(tmp_path, refuse="", project="hotato-site"):
    d = tmp_path / "site"
    (d / "tools").mkdir(parents=True)
    (d / "atlas").mkdir()
    (d / "atlas" / "feed.json").write_text("{}")
    (d / "index.html").write_text("home")
    (d / "tools" / "stage-deploy.sh").write_text(STAGE.format(refuse=refuse, project=project))
    fake = tmp_path / "wrangler"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path}/wrangler.args\n"
                    "env | grep -c CLOUDFLARE_API_TOKEN >> " + f"{tmp_path}/wrangler.args\n"
                    "echo 'https://abc123.hotato-site.pages.dev'\n")
    fake.chmod(0o755)
    return str(d), str(fake)


DEPLOY = dict(token="cf-token-value", account="acct", project="hotato-site", branch="main",
              commit="b" * 40, message="atlas data as of x")


def test_deploy_runs_the_sites_own_staging_with_the_live_branch_and_commit(tmp_path):
    d, fake = site_repo(tmp_path)
    res = site.deploy(d, wrangler=fake, sleep=lambda s: None, **DEPLOY)
    args = (tmp_path / "wrangler.args").read_text()
    assert res["url"] == "https://abc123.hotato-site.pages.dev"
    assert "--project-name hotato-site" in args and f"--branch main --commit-hash {'b' * 40}" in args
    assert "cf-token-value" not in args  # the token is in the environment, never an argument


def test_deploy_shim_refuses_another_project(tmp_path):
    d, fake = site_repo(tmp_path, project="someone-else")
    with pytest.raises(site.DeployRefused):
        site.deploy(d, wrangler=fake, sleep=lambda s: None, **DEPLOY)
    assert not (tmp_path / "wrangler.args").exists()


def test_a_staging_refusal_is_not_retried(tmp_path):
    d, fake = site_repo(tmp_path, refuse='echo "stage: files not tracked by git" >&2; exit 1')
    slept = []
    with pytest.raises(site.DeployRefused):
        site.deploy(d, wrangler=fake, sleep=slept.append, **DEPLOY)
    assert slept == []


# ---- the hourly job's publish step -----------------------------------------------------------

@pytest.fixture
def fake_atlas(tmp_path, monkeypatch):
    pkg = tmp_path / "fakepkg" / "atlas"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("__version__ = '0.2.0'\n")
    (pkg / "distill.py").write_text("class EngineUnavailable(RuntimeError):\n    pass\nENGINES = {'claude': object}\n")
    (pkg / "proposals.py").write_text("class StubProposer:\n    name='stub-proposer'\n"
                                      "class StubReviewer:\n    name='stub-reviewer'\n"
                                      "def resolve_agents(name='auto'):\n    return None, None\n")
    (pkg / "deploy.py").write_text("def preflight(pages, site):\n    return {'ok': True, 'problems': []}\n")
    saved = {k: v for k, v in sys.modules.items() if k == "atlas" or k.startswith("atlas.")}
    for k in saved:
        del sys.modules[k]
    monkeypatch.syspath_prepend(str(tmp_path / "fakepkg"))
    importlib.invalidate_caches()
    yield
    for k in [k for k in sys.modules if k == "atlas" or k.startswith("atlas.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def test_gemini_retries_transient_errors_then_stops_cleanly(fake_atlas):
    import atlas_job as job

    class Busy(Exception):
        code = 429

    class Denied(Exception):
        code = 403
    calls = []

    class R:
        text = "{}"

        class usage_metadata:
            prompt_token_count, candidates_token_count, thoughts_token_count = 1000, 100, 50

    class C:
        class models:
            @staticmethod
            def generate_content(**kw):
                calls.append(kw)
                if len(calls) == 1:
                    raise Busy()
                if kw["contents"] == "deny":
                    raise Denied()
                return R
    e = job.GeminiEngine(client=C, model="gemini-x-flash", sleep=lambda s: None)
    assert e.run("p", {}) == "{}" and job.USAGE["retries"] >= 1
    assert calls[-1]["config"]["thinking_config"] == {"thinking_budget": 0}
    from atlas.distill import EngineUnavailable
    with pytest.raises(EngineUnavailable):
        e.run("deny", {})


def test_proposer_and_reviewer_are_separate_gemini_engines(fake_atlas):
    import atlas_job as job
    job.install_engine()
    from atlas import distill, proposals
    p, r = proposals.resolve_agents("claude")
    assert distill.ENGINES["claude"] is job.GeminiEngine
    assert isinstance(p, job.GeminiEngine) and isinstance(r, job.GeminiEngine)
    assert p is not r and p.name != r.name and "claude" not in p.name + r.name
    assert type(proposals.resolve_agents("stub")[0]).__name__ == "StubProposer"


def publish_setup(tmp_path, monkeypatch, store, live_feed):
    import atlas_job as job
    tree = tmp_path / "tree"
    tree.mkdir()
    shutil_pages = pages(tree, IDS, "atlas")
    calls = {"deploy": 0, "heartbeats": []}
    served = {"feed": live_feed}

    def fetch(url):
        return served["feed"]
    monkeypatch.setattr(job.site, "fetch_url", fetch)

    def deploy(tree_dir, **kw):
        calls["deploy"] += 1
        calls["kw"] = kw
        served["feed"] = open(os.path.join(shutil_pages, "feed.json"), "rb").read()
        return {"url": "https://x.hotato-site.pages.dev"}
    monkeypatch.setattr(job.site, "deploy", deploy)
    monkeypatch.setattr(job, "heartbeat", lambda s, j, *a: calls["heartbeats"].append(j))
    live = {"commit": "c" * 40, "production_branch": "main", "token": "t", "account": "a"}
    return job, str(tree), live, calls


def test_publish_deploys_confirms_and_records(tmp_path, monkeypatch, store, fake_atlas):
    monkeypatch.setenv("DRY_RUN", "0")
    job, tree, live, calls = publish_setup(tmp_path, monkeypatch, store, feed(IDS[:18]))
    rec = {}
    assert job.publish(store, rec, None, tree, live, "atlas", sleep=lambda s: None) == "deployed"
    assert calls["deploy"] == 1 and calls["kw"]["branch"] == "main" and calls["kw"]["commit"] == "c" * 40
    assert calls["heartbeats"] == ["atlas-publish"]
    m = store.get_json(job.MANIFEST_KEY)
    assert m["verified"] and m["live_commit"] == "c" * 40
    assert store.get_json(job.NEWEST_KEY)["ts"] == "2026-11-27T10:00:00Z"  # freshness.py reads it
    # the next hour, nothing changed: no deploy, still a publish heartbeat
    assert job.publish(store, {}, None, tree, live, "atlas", sleep=lambda s: None) == "current"
    assert calls["deploy"] == 1 and calls["heartbeats"] == ["atlas-publish", "atlas-publish"]


def test_publish_in_shadow_deploys_nothing(tmp_path, monkeypatch, store, fake_atlas):
    monkeypatch.setenv("DRY_RUN", "1")
    job, tree, live, calls = publish_setup(tmp_path, monkeypatch, store, feed(IDS))
    rec = {}
    assert job.publish(store, rec, None, tree, live, "atlas") == "shadow"
    assert calls["deploy"] == 0 and rec["would_deploy"]["entries"] == len(IDS)


def test_publish_refuses_broken_pages(tmp_path, monkeypatch, store, fake_atlas, capsys):
    monkeypatch.setenv("DRY_RUN", "0")
    job, tree, live, calls = publish_setup(tmp_path, monkeypatch, store, feed(IDS))
    os.remove(os.path.join(tree, "atlas", "feed.json"))
    assert job.publish(store, {}, None, tree, live, "atlas") == "refused"
    assert calls["deploy"] == 0 and "atlas_guard_structure" in capsys.readouterr().out
    assert store.get_json(job.STATUS_KEY)["publish"] == "refused"


def test_a_deploy_that_does_not_land_is_retried_next_run(tmp_path, monkeypatch, store, fake_atlas):
    monkeypatch.setenv("DRY_RUN", "0")
    job, tree, live, calls = publish_setup(tmp_path, monkeypatch, store, feed(IDS[:18]))
    monkeypatch.setattr(job.site, "deploy", lambda d, **kw: {"url": ""})  # live feed never changes
    assert job.publish(store, {}, None, tree, live, "atlas", sleep=lambda s: None) == "unverified"
    assert not store.get_json(job.MANIFEST_KEY)["verified"]
    rec = {}
    job.publish(store, rec, None, tree, live, "atlas", sleep=lambda s: None)
    assert rec["deploy_reason"] == "live feed differs from this render"


# ---- the watch ------------------------------------------------------------------------------

AT = dt.datetime(2026, 11, 27, 12, tzinfo=dt.timezone.utc)


def test_watch_assessment():
    f = feed(IDS, as_of="2026-11-27T10:00:00Z")
    m = {"feed_sha256": hashlib.sha256(f).hexdigest()}
    fresh = {"last_run": "2026-11-27T11:05:00+00:00"}
    assert watch.assess(fresh, m, f, AT) == []
    kinds = [k for k, *_ in watch.assess({"last_run": "2026-11-27T05:00:00+00:00"}, {"feed_sha256": "x"},
                                         feed(IDS, as_of="2026-11-25T00:00:00Z"), AT)]
    assert kinds == ["atlas_live_mismatch", "atlas_stale", "atlas_data_stale"]
    assert watch.assess(fresh, m, None, AT)[0][0] == "atlas_live_unreachable"


def test_watch_reruns_once_then_waits_out_the_cooldown(store, monkeypatch):
    monkeypatch.setattr(watch, "now", lambda: AT)
    started = []
    stale = feed(IDS, as_of="2026-11-20T00:00:00Z")
    rec = {}
    assert watch.work(store, rec, start=started.append, fetch=lambda u: stale) == "rerun"
    assert started == ["hotato-atlas"]
    assert watch.work(store, {}, start=started.append, fetch=lambda u: stale) == "cooldown"
    assert started == ["hotato-atlas"]


def test_watch_never_starts_a_run_while_one_holds_the_store(store, monkeypatch):
    monkeypatch.setattr(watch, "now", lambda: AT)
    state.Objects(store).write(state.LOCK_KEY, json.dumps(
        {"holder": "atlas", "expires": "2026-11-27T13:00:00Z"}).encode(), 0)
    started = []
    assert watch.work(store, {}, start=started.append, fetch=lambda u: None) == "alerted"
    assert watch.work(store, {}, start=started.append,
                      fetch=lambda u: feed(IDS, as_of="2026-11-20T00:00:00Z")) == "busy"
    assert started == []


# ---- the collector release job --------------------------------------------------------------

def test_wheel_contents_and_versions(tmp_path):
    import zipfile
    w = tmp_path / "hotato_atlas-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(w, "w") as z:
        for n in ("atlas/cli.py", "atlas/distill.py", "atlas/sitepages.py", "hotato_atlas-0.3.0.dist-info/METADATA"):
            z.writestr(n, "")
    assert wheel.wheel_problems(str(w), "0.3.0") == []
    with zipfile.ZipFile(w, "a") as z:
        z.writestr("tests/secret.txt", "")
    assert wheel.wheel_problems(str(w), "0.3.0") == ["tests/secret.txt"]
    index = b'<a href="hotato_atlas-0.2.0-py3-none-any.whl#sha256=1">x</a>'
    assert wheel.published_versions("t", fetch=lambda u: index) == {"0.2.0"}
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "hotato-atlas"\nversion = "0.3.0"\n')
    assert wheel.source_version(str(tmp_path)) == "0.3.0"


def test_wheel_job_does_nothing_without_a_configured_repository(store, monkeypatch):
    monkeypatch.delenv("ATLAS_COLLECTOR_REPO", raising=False)
    rec = {}
    assert "no collector repository" in wheel.work(store, rec)


# ---- upkeep -------------------------------------------------------------------------------------

def test_upkeep_asks_for_the_baked_collector_to_catch_up():
    import upkeep
    assert upkeep.atlas_pin('substitutions:\n  _ATLAS_VERSION: "0.2.0"\n') == "0.2.0"
    assert upkeep.atlas_pin_behind("0.2.0", {"current": "0.10.0"}) == "0.10.0"
    assert upkeep.atlas_pin_behind("0.2.0", {"current": "0.2.0"}) is None
    assert upkeep.atlas_pin_behind("0.2.0", None) is None


# ---- infrastructure ------------------------------------------------------------------------------

def tf_jobs():
    main = read("infra", "main.tf")
    block = main[main.index("job_specs = {"):main.index("job_defaults")]
    return re.findall(r"^\s{4}([a-z-]+)\s+=\s+\{ script", block, re.M), block


def test_every_job_is_scheduled_deployed_and_watched():
    jobs, _ = tf_jobs()
    assert {"atlas", "atlas-recheck", "atlas-audit", "atlas-watch", "atlas-wheel"} <= set(jobs)
    sched = read("infra", "schedule.tf")
    for j in jobs:
        assert re.search(rf'jobs = \["{j}"', sched) or j == "reviewer", j
    loop = re.search(r"for j in ([a-z -]+); do", read("build", "cloudbuild.yaml")).group(1).split()
    assert set(loop) == set(jobs)


def test_new_schedules_start_paused_and_stay_owner_controlled():
    sched = read("infra", "schedule.tf")
    assert 'paused_at_create = toset(["atlas-recheck", "atlas-audit", "atlas-watch", "atlas-wheel"])' in sched
    assert "ignore_changes = [paused]" in sched


def test_workflow_waits_longer_than_the_longest_job():
    _, block = tf_jobs()
    longest = max(int(t) for t in re.findall(r"timeout = (\d+)", block))
    wait = int(re.search(r"timeout: (\d+)", read("infra", "workflow.yaml")).group(1))
    assert wait >= longest * (1 + 1) and wait < 86400  # one retry of the longest job fits


def test_lock_outlives_the_hourly_job():
    _, block = tf_jobs()
    atlas_timeout = int(re.search(r'atlas\s+= \{[^}]*timeout = (\d+)', block).group(1))
    ttl = int(re.search(r'ATLAS_LOCK_TTL_S\s+= "(\d+)"', read("infra", "main.tf")).group(1))
    assert ttl > atlas_timeout


def test_image_pins_wrangler_and_carries_rsync():
    docker = read("build", "Dockerfile.jobs")
    assert "wrangler@3.99.0" in docker and " rsync " in docker


def test_private_names_stay_out_of_the_repository():
    vars_tf = read("infra", "variables.tf")
    for v in ("atlas_site_repo", "atlas_collector_repo"):
        assert re.search(rf'variable "{v}" \{{\n  type        = string\n  default     = ""', vars_tf), v
    for p in ("jobs/atlas_job.py", "jobs/atlas_site.py", "jobs/atlas_wheel.py", "infra/main.tf", "RUNBOOK.md"):
        body = read(*p.split("/"))
        assert "/home/" not in body and not re.search(r"\b[0-9a-f]{32}\b", body), p


def test_only_the_wheel_job_can_upload_collector_versions():
    iam = read("infra", "iam.tf")
    writers = re.findall(r'role\s+= "roles/artifactregistry.writer"\n\s+member\s+= "serviceAccount:\$\{google_service_account\.([a-z_]+)(\["[a-z-]+"\])?', iam)
    assert sorted(writers) == sorted([("build", ""), ("job", '["atlas-wheel"]')])


def test_only_the_hourly_job_reads_the_pages_token():
    main = read("infra", "main.tf")
    _, block = tf_jobs()
    assert block.count("atlas_publish_secrets") == 1 and re.search(r"atlas\s+= \{[^}]*atlas_publish_secrets", block)
    assert '"cloudflare-pages-token"' not in main.split("atlas_repo_read_secrets =")[1].split("\n")[0]
