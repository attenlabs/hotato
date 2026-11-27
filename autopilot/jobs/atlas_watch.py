"""atlas-watch: an outside check that hotato.dev/atlas is what the pipeline last published, and
recent. It reads only the live site and the state bucket, so it still works when the hourly job
is broken; when the pages are stale or replaced it re-runs hotato-atlas (at most once per
cooldown, never while a run holds the store).

  live feed unreachable                      ERROR  atlas_live_unreachable   (no re-run)
  live feed is not our last deploy           WARN   atlas_live_mismatch      re-run
  no hourly run finished for ATLAS_STALE_H   ERROR  atlas_stale              re-run
  live data older than ATLAS_MAX_DATA_AGE_H  WARN   atlas_data_stale         re-run
"""
import datetime as dt
import hashlib
import json
import os

from common import alert, gcp_token, env_float, http, main_guard, now
import atlas_site as site
import atlas_state as state

WATCH_KEY = "atlas/watch.json"


def _age_h(iso, at):
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at - t).total_seconds() / 3600


def assess(status, manifest, live_feed, at):
    """-> list of (kind, severity, detail, rerun)."""
    out = []
    if live_feed is None:
        return [("atlas_live_unreachable", "ERROR", "hotato.dev/atlas/feed.json did not answer", False)]
    sha = hashlib.sha256(live_feed).hexdigest()
    if manifest and manifest.get("feed_sha256") and sha != manifest["feed_sha256"]:
        out.append(("atlas_live_mismatch", "WARNING",
                    "the live feed is not the last Atlas deploy (a site deploy replaced it, or it did not land)",
                    True))
    run_age = _age_h((status or {}).get("last_run"), at)
    if run_age is None or run_age > env_float("ATLAS_STALE_H", 3):
        out.append(("atlas_stale", "ERROR",
                    f"no hourly Atlas run has finished for {'ever' if run_age is None else f'{run_age:.1f} h'}",
                    True))
    try:
        as_of = json.loads(live_feed).get("data_as_of", "")
    except ValueError:
        as_of = ""
    data_age = _age_h(as_of, at)
    if data_age is None or data_age > env_float("ATLAS_MAX_DATA_AGE_H", 36):
        out.append(("atlas_data_stale", "WARNING",
                    f"the live Atlas data is as of {as_of or 'unknown'}", True))
    return out


def run_busy(store, at):
    raw, _ = state.Objects(store).read(state.LOCK_KEY)
    if not raw:
        return False
    try:
        exp = json.loads(raw).get("expires", "")
        return dt.datetime.fromisoformat(exp.replace("Z", "+00:00")) > at
    except ValueError:
        return False


def start_job(name):
    project, region = os.environ["GCP_PROJECT"], os.environ.get("GCP_REGION", "us-central1")
    url = f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{name}:run"
    http(url, "POST", {"Authorization": f"Bearer {gcp_token()}", "Content-Type": "application/json"}, b"{}")


def work(store, rec, start=start_job, fetch=None):
    at = now()
    fetch = fetch or site.fetch_url
    live = fetch(os.environ.get("ATLAS_SITE_URL", "https://hotato.dev").rstrip("/") + "/atlas/feed.json")
    findings = assess(store.get_json("atlas/status.json"), store.get_json("atlas/published/manifest.json"),
                      live, at)
    for kind, sev, detail, _ in findings:
        alert(kind, sev, detail, "atlas-watch")
    rec["findings"] = [f[0] for f in findings]
    if not any(f[3] for f in findings):
        return "ok" if not findings else "alerted"
    watch = store.get_json(WATCH_KEY, {}) or {}
    since = _age_h(watch.get("last_rerun"), at)
    if since is not None and since < env_float("ATLAS_RERUN_COOLDOWN_H", 3):
        rec["rerun"] = f"cooldown ({since:.1f} h since the last re-run)"
        return "cooldown"
    if run_busy(store, at):
        rec["rerun"] = "a run holds the store"
        return "busy"
    start("hotato-atlas")
    store.put_json(WATCH_KEY, {"last_rerun": at.isoformat(), "why": rec["findings"]})
    rec["rerun"] = "started hotato-atlas"
    return "rerun"


if __name__ == "__main__":
    main_guard("atlas-watch", work)
