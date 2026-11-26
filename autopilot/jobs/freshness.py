"""freshness job: nightly consistency checks. Pure comparison helpers + a runner.
A failed check opens an `autopilot` upkeep issue (deduplicated by title)."""
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import xml.etree.ElementTree as ET

from common import dry_run, get_json, http, main_guard, now, open_issue, repo

VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


# ---- pure logic -------------------------------------------------------------
def check_version(text, latest):
    """Every x.y.z that follows the word 'version' or 'v' (or pip pin ==) must equal latest."""
    found = re.findall(r"(?:version|\bv|==)\s*:?\s*(\d+\.\d+\.\d+)", text, re.I)
    bad = sorted({v for v in found if v != latest})
    return (not bad and bool(found)), {"found": sorted(set(found)), "latest": latest, "bad": bad}


def check_figures(site, release, tol=0.0):
    """site/release: {name: number}. Returns names that disagree (missing counts as disagree)."""
    bad = []
    for k, v in site.items():
        r = release.get(k)
        if r is None or abs(r - v) > tol * max(abs(r), 1):
            bad.append(k)
    return sorted(bad)


def hours_old(ts_iso, at):
    t = dt.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return (at - t).total_seconds() / 3600


def atlas_fresh(newest_iso, at, max_hours=48):
    return hours_old(newest_iso, at) < max_hours


def stale_issues(issues, at, days=7):
    """Open issues, not PRs, with zero comments, older than `days`."""
    out = []
    for i in issues:
        if "pull_request" in i or i.get("state", "open") != "open":
            continue
        age = hours_old(i["created_at"], at) / 24
        if age > days and i.get("comments", 0) == 0:
            out.append(i["number"])
    return out


def red_main(check_runs):
    return sorted(c["name"] for c in check_runs if c.get("conclusion") in ("failure", "timed_out", "cancelled"))


def sitemap_urls(xml_text):
    root = ET.fromstring(xml_text)
    return [e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]


def sitemap_current(xml_text, required_urls):
    have = set(sitemap_urls(xml_text))
    return sorted(set(required_urls) - have)


def readme_commands(md):
    """Lines starting with `hotato ` inside fenced blocks."""
    cmds = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", md, re.S):
        cmds += [ln.strip().lstrip("$ ") for ln in block.splitlines() if ln.strip().lstrip("$ ").startswith("hotato ")]
    return cmds


def links(md):
    return sorted(set(re.findall(r"\]\((https?://[^)\s]+)\)", md)))


# ---- runner -----------------------------------------------------------------
def fetch(url):
    try:
        return http(url)[1].decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None


def link_ok(url):
    try:
        return http(url, "HEAD")[0] < 400
    except (urllib.error.URLError, OSError):
        return False


def fresh_install_commands(cmds, version):
    """Run README commands in a throwaway venv. Only `--help`/`--version` style commands
    are run; anything else is skipped (no network side effects)."""
    failed = []
    with tempfile.TemporaryDirectory() as t:
        subprocess.run([sys.executable, "-m", "venv", t], check=True)
        subprocess.run([f"{t}/bin/pip", "install", "-q", f"hotato=={version}"], check=True)
        for c in cmds:
            if not re.search(r"--(help|version)\b", c):
                continue
            argv = c.split()
            argv[0] = f"{t}/bin/hotato"
            if subprocess.run(argv, capture_output=True, timeout=60).returncode:
                failed.append(c)
    return failed


def work(store, rec):
    site = os.environ.get("SITE_URL", "https://hotato.dev")
    r = repo()
    problems = {}
    latest = get_json("https://pypi.org/pypi/hotato/json")["info"]["version"]
    home = fetch(site) or ""
    ok, d = check_version(home, latest)
    if not ok:
        problems["site_version"] = d
    readme = fetch(f"https://raw.githubusercontent.com/{r}/main/README.md") or ""
    ok, d = check_version(readme, latest)
    if not ok:
        problems["readme_version"] = d
    figs = store.get_json("freshness/figures.json")  # {"site": {...}, "release": {...}} written by the build
    if figs:
        bad = check_figures(figs["site"], figs["release"])
        if bad:
            problems["figures"] = bad
    dead = [u for u in links(readme) if not link_ok(u)]
    if dead:
        problems["dead_links"] = dead
    sm = fetch(site + "/sitemap.xml")
    if sm is None or sitemap_current(sm, [site + "/"]):
        problems["sitemap"] = "missing or stale"
    newest = store.get_json("atlas/newest.json")
    if newest and not atlas_fresh(newest["ts"], now()):
        problems["atlas_age"] = newest["ts"]
    issues = get_json(f"https://api.github.com/repos/{r}/issues?state=open&per_page=100")
    st = stale_issues(issues, now())
    if st:
        problems["stale_issues"] = st
    runs = get_json(f"https://api.github.com/repos/{r}/commits/main/check-runs")["check_runs"]
    if red_main(runs):
        problems["red_main"] = red_main(runs)
    if not dry_run():
        bad = fresh_install_commands(readme_commands(readme), latest)
        if bad:
            problems["readme_commands"] = bad
    rec["problems"] = problems
    for key, detail in problems.items():
        rec.setdefault("issues", {})[key] = open_issue(
            f"freshness: {key}", f"Nightly freshness check failed.\n\n{detail}")
    return {"failed": sorted(problems), "latest": latest}


if __name__ == "__main__":
    main_guard("freshness", work)
