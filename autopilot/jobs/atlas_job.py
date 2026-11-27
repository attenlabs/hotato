"""The Atlas jobs: keep hotato.dev/atlas current with no person in the loop.

One script, four modes (the Cloud Run job passes the mode as its first argument):

  run      hotato-atlas, hourly. Collect, classify new reports on a time budget, run the
           propose/review loop, render the pages into a checkout of the exact site release that
           is live, check them against what is published, deploy, and confirm the live feed.
  recheck  hotato-atlas-recheck, daily. Re-checks the evidence behind every entry (store only).
  audit    hotato-atlas-audit, weekly. The adversarial audit over what is published (store only).
  cli      one-off owner commands against the cloud store (withdraw, merge, dispute, ...):
           `gcloud run jobs execute hotato-atlas-audit --args=cli,withdraw,<id>,--reason,<why>`.
           The next hourly run publishes the result.

Only `run` publishes, and it holds the writer lock (atlas_state.Lock) from the store read to the
confirmed deploy. Every mode that writes the store holds that lock, uploads only over the
generation it read, and passes the same shrink guard first, so two runs can never both write it
and an older render can never be deployed over a newer one (the publish manifest is written only
over the generation this run read, and records the store generation it shows).

The collector (the private hotato-atlas package) is used through its CLI and modules. Gemini on
Vertex AI is swapped in as its classifier and as its proposer and reviewer: two separate engine
instances with different names, and the store refuses a review by the proposer's own name.
"""
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time

from common import alert, debit, dry_run, env_float, heartbeat, main_guard, now, read_secret, redact
import atlas_collector as collector
import atlas_guard as guard
import atlas_site as site
import atlas_state as state
import vertex_models as models

RESOLVED = {"model": None}  # set by prepare() from gates/policy.json models.atlas
USAGE = {"calls": 0, "in": 0, "out": 0, "retries": 0}
JOBS = {"run": "atlas", "recheck": "atlas-recheck", "audit": "atlas-audit", "cli": "atlas-cli"}
MANIFEST_KEY = "atlas/published/manifest.json"
STATUS_KEY = "atlas/status.json"
NEWEST_KEY = "atlas/newest.json"  # read by freshness.py
RETRYABLE = {408, 429, 500, 502, 503, 504}


def cfg(name, default):
    return os.environ.get(name, default)


def site_url():
    return cfg("ATLAS_SITE_URL", "https://hotato.dev").rstrip("/")


def _sha(b):
    return hashlib.sha256(b).hexdigest() if b else ""


# ---- Gemini as the collector's engine ------------------------------------------------

class GeminiEngine:
    """Implements atlas.distill.Engine: name + available() + run(prompt, context) -> raw text.
    Transient Vertex errors are retried with jittered backoff; anything else, or the run's
    spend cap, raises the collector's EngineUnavailable so the pass stops cleanly."""
    name = "gemini-vertex"

    def __init__(self, client=None, model=None, attempts=5, sleep=time.sleep):
        self.model = model or RESOLVED["model"] or cfg("ATLAS_GEMINI_MODEL", "gemini-2.5-flash")
        self._client, self.attempts, self.sleep = client, attempts, sleep

    def available(self):
        return True

    def _c(self):
        if self._client is None:
            from google import genai  # google-genai, Vertex mode (ADC of the job's service account)
            self._client = genai.Client(vertexai=True, project=os.environ["GCP_PROJECT"],
                                        location=models.vertex_region())
        return self._client

    def _config(self):
        conf = {"response_mime_type": "application/json", "temperature": 0}
        budget = cfg("ATLAS_THINKING_BUDGET", "0")
        if "flash" in self.model and budget != "":
            conf["thinking_config"] = {"thinking_budget": int(budget)}
        return conf

    def run(self, prompt, context):
        if over_run_cap():
            raise engine_unavailable()("this run reached ATLAS_RUN_MAX_USD")
        resp = None
        for i in range(self.attempts):
            try:
                resp = self._c().models.generate_content(model=self.model, contents=prompt,
                                                         config=self._config())
                break
            except Exception as exc:  # noqa: BLE001 - classify, then retry or stop
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                transient = code in RETRYABLE or isinstance(exc, (TimeoutError, ConnectionError))
                if not transient or i == self.attempts - 1:
                    raise engine_unavailable()(
                        f"gemini call failed: {type(exc).__name__} {code or ''}".strip()) from exc
                USAGE["retries"] += 1
                self.sleep(min(60, 2 ** (i + 1)) + random.random())
        meta = getattr(resp, "usage_metadata", None)
        USAGE["calls"] += 1
        USAGE["in"] += int(getattr(meta, "prompt_token_count", 0) or 0)
        USAGE["out"] += int(getattr(meta, "candidates_token_count", 0) or 0) \
            + int(getattr(meta, "thoughts_token_count", 0) or 0)
        return resp.text or ""


def engine_unavailable():
    """The collector's EngineUnavailable (a distill pass stops on it and keeps what it has)."""
    try:
        from atlas.distill import EngineUnavailable
        return EngineUnavailable
    except ImportError:  # the collector is not installed (this repository's own test run)
        return RuntimeError


def spent_usd():
    return (USAGE["in"] * env_float("ATLAS_USD_PER_MTOK_IN", 0.30)
            + USAGE["out"] * env_float("ATLAS_USD_PER_MTOK_OUT", 2.50)) / 1e6


def over_run_cap():
    return spent_usd() >= env_float("ATLAS_RUN_MAX_USD", 5.0)


def install_engine(cls=GeminiEngine):
    """The CLI's argparse accepts auto|stub|claude; `claude` is re-pointed at Gemini for the
    classifier, and the proposer/reviewer pair is built from two Gemini instances."""
    from atlas import distill
    distill.ENGINES["claude"] = cls
    try:
        from atlas import proposals
    except ImportError:
        return distill

    def resolve_agents(name="auto"):
        if name == "stub":
            return proposals.StubProposer(), proposals.StubReviewer()
        proposer, reviewer = cls(), cls(model=cfg("ATLAS_REVIEWER_MODEL", "") or None)
        proposer.name, reviewer.name = "gemini-proposer", "gemini-reviewer"
        return proposer, reviewer
    proposals.resolve_agents = resolve_agents
    return distill


def cli(argv):
    from atlas import cli as acli
    return acli.main(list(argv))


def charge(store, rec):
    """Book this run's model spend in the shared ledger (category agents)."""
    usd = round(spent_usd(), 4)
    rec["model_usage"] = dict(USAGE, usd=usd)
    if usd and not debit(store, "agents", usd, env_float("AGENTS_CAP_USD", 0)):
        alert("atlas_spend_cap", "WARNING", f"agents cap reached; ${usd} not booked")


# ---- the store under the writer lock ---------------------------------------------------

class CollectorFailed(RuntimeError):
    """The collector itself exited non-zero: a fault of the version that ran."""
    collector_fault = True


class Workdir:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas-run-")
        self.db = os.path.join(self.tmp, "atlas.db")
        self.before = os.path.join(self.tmp, "before.db")  # the store as this run read it
        self.cache = cfg("ATLAS_CACHE_DIR", "") or os.path.join(self.tmp, "cache")
        self.health = os.path.join(self.tmp, "health.json")
        self.feed = os.path.join(self.tmp, "feed")

    def base(self, db=None):
        # ATLAS_OFFLINE=1 (dry runs, tests): the collector reads its cache only, fetches nothing.
        offline = ["--offline"] if cfg("ATLAS_OFFLINE", "") == "1" else []
        return offline + ["--db", db or self.db, "--cache", self.cache, "--health", self.health]

    def keep_before(self):
        if os.path.exists(self.db):
            shutil.copyfile(self.db, self.before)


def locked(store, job, wait_s):
    lock = state.Lock(state.Objects(store), job, int(env_float("ATLAS_LOCK_TTL_S", 6000)))
    return lock, lock.acquire(wait_s=wait_s, poll_s=30)


def prepare(store, rec, job, models_needed=True, candidate=False, github=False):
    rec["collector"] = collector.select(store, job, allow_candidate=candidate)
    if models_needed:
        RESOLVED["model"] = rec["model"] = models.resolve("atlas")
    install_engine()
    if github and not os.environ.get("GITHUB_TOKEN"):
        tok = read_secret("github-bot-token")  # raises GitHub's rate limit for evidence checks
        if tok:
            os.environ["GITHUB_TOKEN"] = tok


def distill_on_budget(w, rec, budget_s, chunk):
    """Classify pending reports in chunks until the queue is empty, the time budget is spent,
    the run spend cap is reached, or the engine stops answering."""
    from atlas import distill as D, store as S
    conn = S.connect(w.db)
    engine = D.resolve_engine("claude")
    start, done, rounds, errors = time.monotonic(), 0, 0, []
    try:
        while time.monotonic() - start < budget_s and not over_run_cap():
            res = D.run_distill(conn, engine, limit=chunk,
                                now=now().replace(microsecond=0).isoformat().replace("+00:00", "Z"))
            rounds += 1
            done += res.classified + res.unclassified
            errors += list(res.errors)[:3]
            if res.considered < chunk or (res.errors and not res.classified):
                break
    finally:
        conn.close()
    rec["distill"] = {"rounds": rounds, "processed": done, "seconds": int(time.monotonic() - start),
                      "errors": [redact(e)[:200] for e in errors[:5]]}
    return done


# ---- the shrink guard, before any store is saved ---------------------------------------

def render_feed(w, db=None, out=None):
    """The feed alone (no site needed): what the shrink check compares, before the store is saved."""
    return cli(w.base(db) + ["publish", "--out", out or w.feed, "--no-pages"])


def baseline_for(store, w):
    """What the new store is compared with -> (manifest or None, source).
    Our last deploy, else the feed the live site serves, else the store as this run read it
    (rendered here). None only when there is no earlier store at all (an owner-allowed empty
    start); every other gap is a refusal, never a skipped check."""
    base = guard.baseline(store.get_json(MANIFEST_KEY), site.fetch_url(f"{site_url()}/atlas/feed.json"))
    if base:
        return base, base.get("source", "published")
    if not os.path.exists(w.before):
        return None, "empty start"
    tmp = os.path.join(w.tmp, "before-copy.db")
    shutil.copyfile(w.before, tmp)  # the collector may migrate the file it reads
    out = os.path.join(w.tmp, "feed-before")
    if render_feed(w, db=tmp, out=out):
        return None, "unavailable"
    prev = guard.manifest(out)
    if not prev.get("feed_ok"):
        return None, "unavailable"
    return dict(prev, source="previous store"), "previous store"


def guard_store(store, rec, w, job):
    """Render the feed of the new store and compare it with the baseline. -> (refusal outcome
    or None, allow_shrink flag consumed)."""
    rc = render_feed(w)
    rec.setdefault("stages", {})["feed"] = rc
    if rc:
        raise CollectorFailed(f"atlas publish --no-pages exited {rc}")
    new = guard.manifest(w.feed)
    base, source = baseline_for(store, w)
    rec["baseline"] = source
    if base is None and source != "empty start":
        alert("atlas_guard_no_baseline", "ERROR",
              "nothing to compare the new store with (no published manifest, the live feed did not "
              "answer, and the previous store did not render); the store is kept as it was", job)
        set_status(store, publish="refused", refused=["no baseline for the shrink check"])
        return "refused", False
    allow = state.flag(store, "allow-shrink")
    ok, kind, why = guard.check(new, base, allow_shrink=allow)
    if not ok and kind == guard.SHRINK:
        key = state.quarantine(store, w.db, "; ".join(why[:3]))
        rec["guard"] = {"kind": kind, "problems": why[:10], "quarantined": key, "baseline": source}
        alert("atlas_guard_shrink", "ERROR",
              f"the new store would drop published records; kept the old one, quarantined {key}: "
              f"{'; '.join(why[:3])}. Deliberate? set atlas/flags/allow-shrink", job)
        set_status(store, publish="refused", refused=why[:5])
        return "shrink refused", allow
    return None, allow


# ---- publishing ----------------------------------------------------------------------

def site_tree(rec, w):
    """Check out the live site release. -> (dir, live) or (None, reason)."""
    repo = cfg("ATLAS_SITE_REPO", "")
    if not repo:
        return None, "no site repository configured (atlas_site_repo)"
    token = read_secret(site.CF_TOKEN_SECRET)
    if not token:
        return None, "no cloudflare-pages-token"
    try:
        acct = site.account_id(token)
        live = site.live_source(token, acct, cfg("ATLAS_PAGES_PROJECT", "hotato-site"))
        gh, kind = site.repo_token(repo)
        rec["site_credential"] = kind
        tree = site.checkout(repo, live["commit"], os.path.join(w.tmp, "site"), gh)
    except site.RepoAccessError as exc:
        alert("atlas_repo_access", "ERROR", f"pages not published: {exc}", rec.get("job_name"))
        return None, str(exc)
    except site.SiteError as exc:
        return None, str(exc)
    bad = site.live_mismatches(tree, site_url(), site.fetch_url)
    if bad:
        return None, f"live site does not match commit {live['commit'][:12]}: " + "; ".join(bad)
    live.update(account=acct, token=token)
    return tree, live


def render_pages(w, tree):
    """The pages, into the site checkout, with that checkout's own chrome."""
    return cli(w.base() + ["publish", "--out", w.feed, "--site-repo", tree,
                           "--pages", os.path.join(tree, "atlas"), "--shell-from-site"])


def preflight(pages, tree):
    from atlas import deploy as DEP
    return DEP.preflight(pages, tree).get("problems", [])


def confirm_live(url, want_sha, attempts=10, sleep=time.sleep):
    got = None
    for i in range(attempts):
        got = _sha(site.fetch_url(url))
        if got == want_sha:
            return True, got
        if i < attempts - 1:
            sleep(30)
    return False, got


def set_status(store, **kw):
    store.update_json(STATUS_KEY, lambda d: dict(d, **kw, updated=now().isoformat()), {})


def read_manifest(store):
    raw, gen = state.Objects(store).read(MANIFEST_KEY)
    return (json.loads(raw) if raw else None), gen


def write_manifest(store, doc, gen):
    return state.Objects(store).write(MANIFEST_KEY, json.dumps(doc, indent=2, sort_keys=True).encode(), gen)


def held_back(store, job, why):
    """A deploy this run must not make, because something newer or different now owns the
    site. Not a fault of the render: the next run starts over."""
    alert("atlas_publish_superseded", "WARNING", f"not deployed: {why}", job)
    set_status(store, publish="superseded", superseded=why[:300])
    return "superseded"


def publish(store, rec, w, tree, live, job, sleep=time.sleep, db_gen=0, lock=None):
    """Structure guard, then deploy. The store is already saved and the writer lock is held
    (run() holds it from the store read to here), so no other run deploys in between. -> outcome."""
    pages = os.path.join(tree, "atlas")
    new = guard.manifest(pages)
    old, mgen = read_manifest(store)
    live_feed = site.fetch_url(f"{site_url()}/atlas/feed.json")
    ok, kind, why = guard.check(new, guard.baseline(old, live_feed),
                                preflight_problems=preflight(pages, tree),
                                css_problems=guard.stylesheet_problems(pages, tree),
                                allow_shrink=True)  # shrink was decided before the store was saved
    try:
        outside = site.changes_outside_atlas(tree, live["commit"])
    except site.SiteError as exc:
        outside = [f"git cannot list the changes: {exc}"]
    if outside:
        ok, kind = False, guard.STRUCTURE
        why = [f"the render changed files outside atlas/: {', '.join(outside[:5])}"] + list(why)
    if not ok:
        rec["guard"] = {"kind": kind, "problems": why[:10]}
        alert("atlas_guard_structure", "ERROR", f"publish refused: {'; '.join(why[:5])}", job)
        set_status(store, publish="refused", refused=why[:5])
        return "refused"
    if old and int(old.get("db_generation") or 0) > int(db_gen or 0):
        return held_back(store, job, f"a newer store (generation {old['db_generation']}) is already "
                                     f"published; this run rendered generation {db_gen}")
    go, reason = guard.needs_deploy(new, old, _sha(live_feed))
    rec["deploy_reason"] = reason
    if not go:
        heartbeat(store, "atlas-publish", "ok", rec.get("mode"))
        set_status(store, publish="current", live_commit=live["commit"], as_of=new["as_of"])
        return "current"
    if dry_run():
        rec["would_deploy"] = {"commit": live["commit"], "entries": len(new["entries"])}
        set_status(store, publish="shadow", live_commit=live["commit"])
        return "shadow"
    project = cfg("ATLAS_PAGES_PROJECT", "hotato-site")
    # Last checks, right before the deploy: the same site release is still live, nobody
    # published since this run read the manifest, and this run still holds the writer lock.
    try:
        now_live = site.live_source(live["token"], live["account"], project)
    except site.SiteError as exc:
        return held_back(store, job, f"the live deployment cannot be re-read: {exc}")
    if not site.same_deployment(live, now_live):
        return held_back(store, job, f"the live site changed since the checkout "
                                     f"({live['commit'][:12]} -> {now_live.get('commit', '')[:12]})")
    if state.Objects(store).generation(MANIFEST_KEY) != mgen:
        return held_back(store, job, "another run published while this one rendered")
    if lock is not None and not lock.still_held():
        return held_back(store, job, "this run no longer holds the writer lock")
    msg = f"atlas data as of {new['as_of'] or 'unknown'} on site {live['commit'][:12]}"
    try:
        res = site.deploy(tree, token=live["token"], account=live["account"], project=project,
                          branch=live["production_branch"], commit=live["commit"], message=msg,
                          wrangler=cfg("ATLAS_WRANGLER", "wrangler"), sleep=sleep)
    except site.SiteError as exc:
        alert("atlas_publish_failed", "ERROR", str(exc), job)
        set_status(store, publish="failed", error=redact(exc)[:300])
        return "failed"
    landed, _ = confirm_live(f"{site_url()}/atlas/feed.json", new["feed_sha256"], sleep=sleep)
    try:
        write_manifest(store, dict(new, deployed=now().isoformat(), live_commit=live["commit"],
                                   deployment=res.get("url", ""), verified=landed, db_generation=db_gen),
                       mgen)
    except state.Conflict:
        alert("atlas_publish_superseded", "ERROR",
              "deployed, but another run wrote the publish manifest meanwhile; the next run re-checks", job)
        return "superseded"
    if not landed:
        alert("atlas_live_mismatch", "ERROR",
              "deployed, but the live feed is not this render after 5 minutes; the next run retries", job)
        set_status(store, publish="unverified", live_commit=live["commit"])
        return "unverified"
    store.put_json(NEWEST_KEY, {"ts": new["as_of"] or now().isoformat(), "deployed": now().isoformat()})
    heartbeat(store, "atlas-publish", "ok", rec.get("mode"))
    set_status(store, publish="deployed", live_commit=live["commit"], as_of=new["as_of"],
               entries=len(new["entries"]), last_deploy=now().isoformat())
    return "deployed"


# ---- modes ---------------------------------------------------------------------------

def run(store, rec, job="atlas"):
    """The hourly run. The writer lock is held from before the store is read until the deploy
    is confirmed, so two runs never interleave a store save and a deploy."""
    rec["job_name"] = job
    lock, held = locked(store, job, wait_s=int(env_float("ATLAS_LOCK_WAIT_S", 120)))
    if held:
        rec["skipped"] = f"store busy: {held.get('holder')} since {held.get('acquired')}"
        return rec["skipped"]
    try:
        prepare(store, rec, job, candidate=True, github=True)
        w = Workdir()
        gen = state.fetch_db(store, w.db, job)
        w.keep_before()
        rec["stages"] = {"collect": cli(w.base() + ["collect", "--limit", cfg("ATLAS_COLLECT_LIMIT", "25")])}
        distill_on_budget(w, rec, env_float("ATLAS_DISTILL_BUDGET_S", 2400),
                          int(env_float("ATLAS_DISTILL_CHUNK", 40)))
        rec["stages"]["coverage"] = cli(w.base() + ["coverage"])
        if not over_run_cap():
            limit = cfg("ATLAS_IMPROVE_LIMIT", "40")
            rec["stages"]["propose"] = cli(w.base() + ["propose", "--agents", "claude", "--limit", limit])
            rec["stages"]["review"] = cli(w.base() + ["review", "--agents", "claude", "--limit", limit])
            cli(w.base() + ["coverage"])
        refused, allow = guard_store(store, rec, w, job)
        if refused:
            return refused
        try:
            gen = state.save_db(store, w.db, gen)
        except state.Conflict:
            alert("atlas_db_conflict", "ERROR", "atlas.db changed under this run; its changes are dropped", job)
            return "conflict"
        if allow:
            state.clear_flag(store, "allow-shrink")
        state.snapshot_daily(store, w.db)
        set_status(store, last_run=now().isoformat(), collector=rec["collector"]["version"])
        tree, live = site_tree(rec, w)
        if not tree:
            rec["site"] = live
            alert("atlas_site_unavailable", "WARNING", f"store updated, pages not published: {live}", job)
            return "store only"
        rec["stages"]["pages"] = render_pages(w, tree)
        if rec["stages"]["pages"]:
            # A site release the generator cannot dress (donor page, stylesheet): the store is fine.
            alert("atlas_guard_structure", "ERROR",
                  f"pages not rendered into site {live['commit'][:12]} (atlas publish exited "
                  f"{rec['stages']['pages']}; see the run log)", job)
            set_status(store, publish="refused", refused=["render into the live site release failed"])
            return "refused"
        rec["publish"] = publish(store, rec, w, tree, live, job, db_gen=gen, lock=lock)
        return rec["publish"]
    finally:
        lock.release()
        charge(store, rec)


def db_only(store, rec, job, argv, wait_s, models_needed=False):
    """recheck, audit and owner commands: the store only, under the same lock and the same
    shrink guard as the hourly run (a deliberate shrink needs atlas/flags/allow-shrink)."""
    lock, held = locked(store, job, wait_s=wait_s)
    if held:
        rec["skipped"] = f"store busy: {held.get('holder')} since {held.get('acquired')}"
        return rec["skipped"]
    try:
        prepare(store, rec, job, models_needed=models_needed)
        w = Workdir()
        gen = state.fetch_db(store, w.db, job)
        w.keep_before()
        rc = cli(w.base() + list(argv))
        rec["exit"] = rc
        if rc:
            raise CollectorFailed(f"atlas {argv[0]} exited {rc}")
        refused, allow = guard_store(store, rec, w, job)
        if refused:
            return refused
        state.save_db(store, w.db, gen)
        if allow:
            state.clear_flag(store, "allow-shrink")
    finally:
        lock.release()
        charge(store, rec)
    return "ok"


CLEAN = {"deployed", "current", "shadow"}   # published, already live, or would publish (shadow)
FAULTS = {"shrink refused", "refused"}       # the render itself was refused


def verdict(outcome):
    """A run outcome -> clean / fault / inconclusive, for settling a candidate collector."""
    if outcome in CLEAN:
        return "clean"
    if outcome in FAULTS:
        return "fault"
    return "inconclusive"


def settle_after(store, rec, fn):
    """Promote, roll back or keep trying a candidate collector on the run's outcome."""
    try:
        out = fn(store, rec)
    except Exception as exc:
        if rec.get("collector"):
            v = collector.verdict_for_exception(exc)
            rec["collector_settled"] = collector.settle(
                store, rec["collector"], v, job="atlas", why=f"the run raised {type(exc).__name__}: {exc}"[:300])
        raise
    if rec.get("collector"):
        rec["collector_settled"] = collector.settle(store, rec["collector"], verdict(out), job="atlas",
                                                    why=f"run outcome: {out}")
    return out


def work_for(mode, extra=()):
    if mode == "run":
        return lambda store, rec: settle_after(store, rec, run)
    if mode in ("recheck", "audit"):
        return lambda store, rec: db_only(store, rec, JOBS[mode], [mode], wait_s=2700)
    if mode == "cli":
        if not extra:
            raise SystemExit("usage: atlas_job.py cli <atlas subcommand> [args]")
        return lambda store, rec: db_only(store, rec, JOBS[mode], extra, wait_s=600, models_needed=True)
    raise SystemExit(f"unknown mode {mode!r}; choose from {sorted(JOBS)}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else cfg("ATLAS_MODE", "run")
    main_guard(JOBS.get(mode, "atlas"), work_for(mode, argv[1:]))


if __name__ == "__main__":
    main()
