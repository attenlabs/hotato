"""The publish guard: a render reaches hotato.dev only if it does not quietly lose records or
break pages compared with what is already published. Pure functions over files and dicts.

Baseline = the manifest of the last deploy this pipeline made (atlas/published/manifest.json),
or, before the first one, the feed the live site serves now.

Refused as a SHRINK (the store itself is suspect: it is quarantined, not uploaded):
  - entries disappear beyond what new withdrawals and merges explain (plus a slack of 2),
  - the published report count falls by more than 10 %.
Refused as STRUCTURE (the store is fine, the pages are not: the store is kept, nothing ships):
  - fewer than 90 % of the baseline's pages (less the entries new withdrawals and merges remove),
  - the feed is missing, unreadable or has no entries,
  - a page links a stylesheet the site checkout does not carry,
  - the collector's own preflight problems (h1, closing tags, template holes, machine files).
A one-shot owner flag (atlas/flags/allow-shrink) lets a deliberate shrink through once.
"""
import hashlib
import json
import os
import re

SHRINK, STRUCTURE = "shrink", "structure"
SLACK = 2


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def digest_dir(path):
    """sha256 over (relative path, file sha256) pairs in path order."""
    h = hashlib.sha256()
    for rel in sorted(_files(path)):
        h.update(rel.encode() + b"\0" + _sha(open(os.path.join(path, rel), "rb").read()).encode() + b"\n")
    return "sha256:" + h.hexdigest()


def _files(path):
    out = []
    for root, _dirs, names in os.walk(path):
        for n in names:
            out.append(os.path.relpath(os.path.join(root, n), path))
    return out


def feed_summary(feed_bytes):
    """-> {as_of, entries: [ids], counts, generator} from a feed.json body, or None."""
    try:
        doc = json.loads(feed_bytes)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        return None
    counts = doc.get("counts") if isinstance(doc.get("counts"), dict) else {}
    return {"as_of": doc.get("data_as_of", ""), "generator": doc.get("generator", ""),
            "entries": sorted(e.get("id", "") for e in doc["entries"] if isinstance(e, dict)),
            "counts": {k: v for k, v in counts.items() if isinstance(v, int)}}


def manifest(pages_dir):
    """What a rendered atlas directory contains."""
    feed_path = os.path.join(pages_dir, "feed.json")
    feed = open(feed_path, "rb").read() if os.path.exists(feed_path) else b""
    summary = feed_summary(feed) or {"as_of": "", "generator": "", "entries": [], "counts": {}}
    pages = [r for r in _files(pages_dir) if r.endswith("index.html")]
    return dict(summary, digest=digest_dir(pages_dir), feed_sha256=_sha(feed) if feed else "",
                pages=len(pages), feed_ok=bool(feed) and feed_summary(feed) is not None)


def baseline(last_manifest, live_feed):
    """The manifest to compare against: our last deploy, else the live feed, else nothing."""
    if last_manifest:
        return last_manifest
    s = feed_summary(live_feed) if live_feed else None
    if s:
        # The live pages are one per entry plus the index and the removal page.
        return dict(s, pages=len(s["entries"]) + 2, feed_sha256=_sha(live_feed), digest="", source="live")
    return None


def stylesheet_problems(pages_dir, site_root):
    linked = set()
    for rel in _files(pages_dir):
        if rel.endswith(".html"):
            html = open(os.path.join(pages_dir, rel), encoding="utf-8", errors="replace").read()
            linked.update(re.findall(r'href="/(assets/[^"?#]+\.css)', html))
    return [f"/{r}: linked by the atlas pages but missing from the site" for r in sorted(linked)
            if not os.path.isfile(os.path.join(site_root, r))]


def check(new, old, *, preflight_problems=(), css_problems=(), allow_shrink=False):
    """-> (ok, kind or None, problems). `kind` is SHRINK or STRUCTURE for a refusal."""
    structure = list(preflight_problems) + list(css_problems)
    if not new.get("feed_ok") or not new.get("entries"):
        structure.append("feed.json is missing, unreadable or has no entries")
    shrink = []
    # Without a readable feed there is nothing to compare: a render fault, not a smaller store.
    if old and new.get("feed_ok"):
        dropped = sorted(set(old.get("entries", [])) - set(new.get("entries", [])))
        oc, nc = old.get("counts", {}), new.get("counts", {})
        explained = max(0, nc.get("withdrawn", 0) - oc.get("withdrawn", 0)) \
            + max(0, nc.get("merged", 0) - oc.get("merged", 0))
        if old.get("pages") and new.get("pages", 0) < 0.9 * (old["pages"] - explained):
            structure.append(f"{new.get('pages', 0)} pages, the last publish had {old['pages']}")
        if len(dropped) > explained + SLACK:
            shrink.append(f"{len(dropped)} published entries would disappear "
                          f"({explained} explained by new withdrawals or merges): {', '.join(dropped[:5])}")
        if oc.get("reports") and nc.get("reports", 0) < 0.9 * oc["reports"]:
            shrink.append(f"published reports would fall from {oc['reports']} to {nc.get('reports', 0)}")
    if shrink and allow_shrink:
        shrink = []
    if shrink:
        return False, SHRINK, shrink + structure
    if structure:
        return False, STRUCTURE, structure
    return True, None, []


def needs_deploy(new, last_manifest, live_feed_sha):
    """Deploy when the render changed since our last deploy, or the live feed is not ours
    (another deploy replaced the site, or our last one did not land)."""
    if not last_manifest or last_manifest.get("digest") != new["digest"]:
        return True, "render changed"
    if live_feed_sha != new["feed_sha256"]:
        return True, "live feed differs from this render"
    return False, "live site already serves this render"
