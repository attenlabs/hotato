"""scorecard job: weekly product-market-fit measures from PUBLIC sources only
(pypistats, GitHub API, competitors' public repos and changelog pages), then a
proposed reorder of BACKLOG.md delivered as a PR. Hosted-service numbers
(activation, retention, value) come from the bucket file metrics/hosted.json
written by the hosted service itself; nothing private is read."""
import json
import os
import re

from common import bot_token, dry_run, get_json, gh, http, main_guard, now, repo

MEASURES = ["reach", "activation", "retention", "pull", "value"]


# ---- pure logic -------------------------------------------------------------
def pct_change(new, old):
    if old in (None, 0):
        return None
    return (new - old) / old


def falling_two_weeks(series):
    """series oldest->newest; True when the last 3 points strictly fall twice running."""
    return len(series) >= 3 and series[-3] > series[-2] > series[-1]


def release_pace(release_dates_iso, at, days=90):
    """Releases per 30 days over the window."""
    import datetime as dt
    n = 0
    for d in release_dates_iso:
        t = dt.datetime.fromisoformat(d.replace("Z", "+00:00"))
        if 0 <= (at - t).days <= days:
            n += 1
    return n * 30 / days


def proven_fit(retention_weekly, threshold, weeks=8):
    """Declared only on `weeks` consecutive weekly retention values >= threshold."""
    return len(retention_weekly) >= weeks and all(x >= threshold for x in retention_weekly[-weeks:])


def new_competitor_features(old_titles, new_titles):
    return [t for t in new_titles if t not in set(old_titles)]


def pull_score(stars_delta, outside_issues, outside_prs):
    return stars_delta + 2 * outside_issues + 5 * outside_prs


def parse_backlog(md):
    """-> (header_lines, items) where item = (text_line, title). Items are `N. title` lines."""
    head, items = [], []
    for ln in md.splitlines():
        m = re.match(r"^\s*\d+\.\s+(.*)$", ln)
        if m:
            items.append(m.group(1).strip())
        elif not items:
            head.append(ln)
    return head, items


def render_backlog(head, items):
    return "\n".join(head + [f"{i}. {t}" for i, t in enumerate(items, 1)]) + "\n"


def reorder(items, top=None, boost=(), keywords=None):
    """Move `top` (a measure-falling item) first, then boosted items, rest keep order.
    keywords: {measure: substring of an item title} to map a measure to an item."""
    def find(sub):
        for t in items:
            if sub.lower() in t.lower():
                return t
        return None
    order = []
    for sub in ([top] if top else []) + list(boost):
        t = find(sub) if sub else None
        if t and t not in order:
            order.append(t)
    return order + [t for t in items if t not in order]


# ---- runner -----------------------------------------------------------------
def collect(store):
    r = repo()
    pkg = os.environ.get("PYPI_NAME", "hotato")
    out = {"ts": now().isoformat()}
    recent = get_json(f"https://pypistats.org/api/packages/{pkg}/recent")["data"]
    out["reach"] = recent["last_week"]
    repo_j = get_json(f"https://api.github.com/repos/{r}")
    out["stars"] = repo_j["stargazers_count"]
    hist = store.get_json("scorecard/history.json", [])
    prev_stars = hist[-1]["stars"] if hist else None
    out["pull"] = pull_score(out["stars"] - (prev_stars or out["stars"]), 0, 0)
    hosted = store.get_json("metrics/hosted.json", {})
    for m in ("activation", "retention", "value"):
        out[m] = hosted.get(m, 0)
    return out, hist


def competitors(store):
    """Public baseline. config: scorecard/competitors.json
    [{"name":..., "repo":"org/name"|null, "changelog_url":...|null}]"""
    cfg = store.get_json("scorecard/competitors.json", []) or []
    base = {}
    for c in cfg:
        row = {}
        if c.get("repo"):
            j = get_json(f"https://api.github.com/repos/{c['repo']}")
            row["stars"] = j["stargazers_count"]
            rel = get_json(f"https://api.github.com/repos/{c['repo']}/releases?per_page=30")
            row["pace_30d"] = release_pace([x["published_at"] for x in rel if x.get("published_at")], now())
        if c.get("changelog_url"):
            _, b = http(c["changelog_url"])
            row["headings"] = re.findall(r"^#{2,3}\s+(.+)$", b.decode("utf-8", "replace"), re.M)[:60]
        base[c["name"]] = row
    return base


def work(store, rec):
    cur, hist = collect(store)
    series = {m: [h.get(m, 0) for h in hist] + [cur[m]] for m in MEASURES}
    falling = [m for m in MEASURES if falling_two_weeks(series[m])]
    base = competitors(store)
    prev_heads = store.get_json("scorecard/competitor-headings.json", {})
    new_feats = {n: new_competitor_features(prev_heads.get(n, []), b.get("headings", [])) for n, b in base.items()}
    store.put_json("scorecard/competitor-headings.json", {n: b.get("headings", []) for n, b in base.items()})
    hist.append(cur)
    store.put_json("scorecard/history.json", hist[-104:])
    fit = proven_fit(series["retention"], float(os.environ.get("FIT_RETENTION", "0.3")))
    # map a falling measure to the backlog item that serves it
    serves = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "measure_items.json")))
    md = open(os.environ.get("BACKLOG_PATH", "BACKLOG.md")).read() if os.path.exists(
        os.environ.get("BACKLOG_PATH", "BACKLOG.md")) else (store.get_bytes("backlog/BACKLOG.md") or b"").decode()
    head, items = parse_backlog(md)
    new_items = reorder(items, top=serves.get(falling[0]) if falling else None)
    changed = new_items != items
    summary = {"falling": falling, "proven_fit": fit, "backlog_changed": changed,
               "competitor_new_features": {k: v[:5] for k, v in new_feats.items() if v}}
    store.put_json(f"scorecard/weekly/{cur['ts'][:10]}.json", {"measures": cur, **summary})
    if changed and not dry_run():
        pr_backlog(render_backlog(head, new_items))
    return summary


def pr_backlog(text):
    import base64
    tok, r = bot_token(), repo()
    base = gh(f"/repos/{r}/git/ref/heads/main", tok)["object"]["sha"]
    branch = f"autopilot/backlog-{now().strftime('%Y%m%d')}"
    gh(f"/repos/{r}/git/refs", tok, "POST", {"ref": f"refs/heads/{branch}", "sha": base})
    cur = gh(f"/repos/{r}/contents/autopilot/BACKLOG.md?ref=main", tok)
    gh(f"/repos/{r}/contents/autopilot/BACKLOG.md", tok, "PUT", {
        "message": "Reorder backlog from weekly scorecard", "branch": branch, "sha": cur["sha"],
        "content": base64.b64encode(text.encode()).decode()})
    gh(f"/repos/{r}/pulls", tok, "POST", {"title": "Reorder backlog from weekly scorecard",
                                         "head": branch, "base": "main",
                                         "body": "Weekly scorecard reorder. Docs class."})


if __name__ == "__main__":
    main_guard("scorecard", work)
