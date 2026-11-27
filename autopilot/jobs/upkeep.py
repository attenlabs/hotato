"""upkeep job: vendor-API drift, dependency bumps, red CI -> `autopilot` issues.
The builder picks the issues up. This job never edits code."""
import hashlib
import re

from common import get_json, http, main_guard, open_issue, repo


# ---- pure logic -------------------------------------------------------------
def parse_pins(text):
    """requirements-style `name==x.y.z` lines -> {name: version}."""
    out = {}
    for ln in text.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-]+)==([0-9][^\s;#]*)", ln)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def vkey(v):
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v))


def outdated(pins, latest):
    """{name: (pinned, latest)} where latest > pinned."""
    return {n: (v, latest[n]) for n, v in pins.items() if n in latest and vkey(latest[n]) > vkey(v)}


def drifted(old_hash, new_hash):
    """First sighting (no old hash) is a baseline, not drift."""
    return old_hash is not None and old_hash != new_hash


def normalize_doc(text):
    return re.sub(r"\s+", " ", text).strip()


def atlas_pin(cloudbuild_text):
    m = re.search(r'^  _ATLAS_VERSION: "([0-9.]+)"$', cloudbuild_text, re.M)
    return m.group(1) if m else None


def atlas_pin_behind(pin, collector_doc):
    """The version the hourly job runs, when it is newer than the version baked into the image.
    The image still works (the job installs the newer version at run time); the bump keeps a
    fresh image from starting on an old collector."""
    cur = (collector_doc or {}).get("current")
    if pin and cur and vkey(cur) > vkey(pin):
        return cur
    return None


def failing_ci(check_runs):
    return sorted({c["name"] for c in check_runs if c.get("conclusion") in ("failure", "timed_out")})


# ---- runner -----------------------------------------------------------------
def work(store, rec):
    r = repo()
    made = {}
    # 1. vendor-API drift: upkeep/vendors.json = [{"name":..., "url": public spec or changelog}]
    vendors = store.get_json("upkeep/vendors.json", []) or load_default_vendors()
    hashes = store.get_json("upkeep/vendor-hashes.json", {})
    for v in vendors:
        try:
            body = normalize_doc(http(v["url"])[1].decode("utf-8", "replace"))
        except OSError:
            made[f"vendor {v['name']}"] = "fetch failed"
            continue
        h = hashlib.sha256(body.encode()).hexdigest()
        if drifted(hashes.get(v["name"]), h):
            made[f"vendor {v['name']}"] = open_issue(
                f"upkeep: {v['name']} API docs changed",
                f"The public page {v['url']} changed since the last check. Review the adapter.")
        hashes[v["name"]] = h
    store.put_json("upkeep/vendor-hashes.json", hashes)
    # 2. dependency bumps
    req = http(f"https://raw.githubusercontent.com/{r}/main/autopilot/requirements.txt")[1].decode()
    pins = parse_pins(req)
    latest = {n: get_json(f"https://pypi.org/pypi/{n}/json")["info"]["version"] for n in pins}
    for n, (old, new) in outdated(pins, latest).items():
        made[f"bump {n}"] = open_issue(f"upkeep: bump {n} {old} to {new}", f"{n} {old} is behind {new}.")
    # 3. red CI
    runs = get_json(f"https://api.github.com/repos/{r}/commits/main/check-runs")["check_runs"]
    for name in failing_ci(runs):
        made[f"ci {name}"] = open_issue(f"upkeep: CI check {name} is red on main", "Fix on main.")
    # 4. the Atlas collector baked into the image is behind the version the hourly job settled on
    cb = http(f"https://raw.githubusercontent.com/{r}/main/autopilot/build/cloudbuild.yaml")[1].decode()
    pin = atlas_pin(cb)
    newer = atlas_pin_behind(pin, store.get_json("atlas/collector.json"))
    if newer:
        made["atlas collector"] = open_issue(
            f"upkeep: bake Atlas collector {newer} into the jobs image",
            f"The hourly Atlas job runs hotato-atlas {newer} (tested, uploaded and promoted by "
            f"atlas-wheel), but `_ATLAS_VERSION` in autopilot/build/cloudbuild.yaml is {pin}. "
            f"Set it to {newer}. Nothing is broken meanwhile: the job installs {newer} at run time.")
    return made


def load_default_vendors():
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendors.json")
    return json.load(open(p)) if os.path.exists(p) else []


if __name__ == "__main__":
    main_guard("upkeep", work)
