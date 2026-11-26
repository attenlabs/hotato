"""Merge policy. Pure functions plus a small CLI.

  python3 gates/policy.py --files a b c --checks test=success ... --labels x --approved
prints JSON {"decision": "auto_merge"|"owner"|"blocked", "class": ..., "reasons": [...]}.
Config: gates/policy.json (owner-only via CODEOWNERS).
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# strictness order: later = stricter
ORDER = ["docs", "tests", "atlas_content", "deps", "vendor_adapter", "feature", "public_claim", "pricing", "owner_only"]


def load_config(path=None):
    with open(path or os.path.join(HERE, "policy.json"), encoding="utf-8") as fh:
        return json.load(fh)


def glob_re(glob):
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile("^" + out + "$")


def classify_file(path, cfg):
    while path.startswith("./"):  # strip "./" prefixes only; lstrip would eat the dot of ".github"
        path = path[2:]
    path = path.lstrip("/")
    # owner_only always wins when matched
    for name in ["owner_only"] + [c for c in cfg["classes"] if c != "owner_only"]:
        for g in cfg["classes"][name]["globs"]:
            if glob_re(g).match(path):
                return name
    return cfg["default_class"]


def strictness(cls):
    return ORDER.index(cls) if cls in ORDER else ORDER.index("feature")


def classify_change(files, labels, cfg):
    """The class of a whole PR = the strictest class among its files and labels."""
    classes = [classify_file(f, cfg) for f in files] or [cfg["default_class"]]
    for lab in labels:
        if lab in cfg["labels_force_owner"]:
            classes.append(lab.replace("-", "_") if lab != "feature" else "feature")
    if cfg["patch_release_label"] in labels and not any(
            strictness(c) >= strictness("feature") for c in classes):
        classes.append("docs")
    return max(classes, key=strictness)


def decide(files, checks, labels=(), approved=False, cfg=None, shadow=None):
    """checks: {name: conclusion}. Returns dict(decision, class, reasons)."""
    cfg = cfg or load_config()
    shadow = cfg["shadow_mode"] if shadow is None else shadow
    labels = list(labels)
    cls = classify_change(files, labels, cfg)
    reasons = []
    for req in cfg["required_checks"]:
        if checks.get(req) != "success":
            reasons.append(f"check {req} is {checks.get(req, 'missing')}")
    if cfg["require_reviewer_approval"] and not approved:
        reasons.append("independent reviewer has not approved")
    if len(files) > cfg["max_changed_files"]:
        reasons.append("too many changed files")
    if reasons:
        return {"decision": "blocked", "class": cls, "reasons": reasons}
    if cls in cfg["needs_owner"] or not cfg["classes"].get(cls, {}).get("auto_merge", False):
        return {"decision": "owner", "class": cls, "reasons": ["class needs the owner's approval"]}
    if shadow:
        return {"decision": "owner", "class": cls, "reasons": ["shadow mode: no auto-merge"]}
    return {"decision": "auto_merge", "class": cls, "reasons": []}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*", default=[])
    ap.add_argument("--checks", nargs="*", default=[], help="name=conclusion")
    ap.add_argument("--labels", nargs="*", default=[])
    ap.add_argument("--approved", action="store_true")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    checks = dict(c.split("=", 1) for c in a.checks)
    res = decide(a.files, checks, a.labels, a.approved, load_config(a.config))
    print(json.dumps(res))
    return 0 if res["decision"] == "auto_merge" else 3


if __name__ == "__main__":
    sys.exit(main())
