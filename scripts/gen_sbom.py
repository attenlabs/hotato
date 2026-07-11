#!/usr/bin/env python3
"""Generate a minimal CycloneDX (JSON) SBOM for the hotato core wheel.

Offline and stdlib-only: it reads ``pyproject.toml`` (the [project] table plus
every [project.optional-dependencies] group) and emits a CycloneDX 1.5 bill of
materials with one component for the package itself and one for each declared
dependency. No network, no pip, no third-party import -- it never resolves,
downloads, or introspects installed packages, so the same tree always produces
the same document regardless of what happens to be installed.

Usage
-----
  python3 scripts/gen_sbom.py                       # -> dist/hotato.sbom.cdx.json
  python3 scripts/gen_sbom.py --out /tmp/sbom.json  # custom output path
  python3 scripts/gen_sbom.py --check dist/hotato.sbom.cdx.json  # validate one
  python3 scripts/gen_sbom.py --profile core        # -> dist/hotato.sbom.core.cdx.json
  python3 scripts/gen_sbom.py --profile neural      # -> dist/hotato.sbom.neural.cdx.json
  python3 scripts/gen_sbom.py --list-profiles       # core + each declared extra

Profiles scope the dependency surface: with no ``--profile`` the SBOM covers the
whole surface (core plus every extra); ``core`` is the zero-dependency core
alone; any extra name is core plus that one ``[project.optional-dependencies]``
group. Each profile gets a deterministic serialNumber, so re-running on the same
tree+profile always yields the same document. ``--list-profiles`` prints ``core``
then every declared extra, one per line, so CI can emit one SBOM per profile.

Exit codes: 0 on success, 1 on a validation failure (in --check mode) or a
parse error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

# stdlib TOML parser (Python 3.11+). hotato targets >=3.9, and this is a
# release-tooling script (run on a maintainer/CI machine), so 3.11+ is a fair
# floor; we fail with a clear message rather than silently mis-parsing.
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - <3.11
    tomllib = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "dist", "hotato.sbom.cdx.json")

# CycloneDX requirements this SBOM commits to (checked by --check).
REQUIRED_TOP_LEVEL = ("bomFormat", "specVersion", "version", "metadata", "components")
REQUIRED_COMPONENT_FIELDS = ("type", "name", "version", "bom-ref", "purl")

# PEP 508 requirement -> package name. Splits off any version/marker/extras so
# "mcp>=1.2.0", "pyannote.audio>=4.0", "torch>=2.8; python_version>='3.10'" all
# reduce to the bare distribution name.
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _load_pyproject(path):
    if tomllib is None:
        raise SystemExit(
            "gen_sbom.py needs Python 3.11+ (tomllib) to parse pyproject.toml; "
            f"running under {sys.version.split()[0]}."
        )
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _req_name(requirement):
    """Bare distribution name from a PEP 508 requirement string, or None."""
    m = _REQ_NAME.match(requirement)
    return m.group(1) if m else None


def _purl(name, version=None):
    """Package URL for a PyPI distribution (spec: pkg:pypi/<name>@<version>)."""
    base = f"pkg:pypi/{name}"
    return f"{base}@{version}" if version else base


def build_sbom(pyproject, profile=None):
    """Build a CycloneDX document for the given dependency ``profile``.

    ``profile`` selects which optional-dependency groups are included:
      * ``None``     -- the whole surface (core deps plus every extra); default.
      * ``"core"``   -- none (the zero-dependency core alone).
      * ``"<extra>"``-- core deps plus that one declared extra group.
    Core (required) ``[project].dependencies`` are always included.
    """
    project = pyproject.get("project")
    if not project:
        raise SystemExit("pyproject.toml has no [project] table")
    name = project.get("name")
    version = project.get("version")
    if not name or not version:
        raise SystemExit("pyproject.toml [project] is missing name or version")

    optional = project.get("optional-dependencies") or {}
    if profile is None:
        groups = list(optional.keys())
    elif profile == "core":
        groups = []
    elif profile in optional:
        groups = [profile]
    else:
        raise SystemExit(
            f"unknown profile {profile!r}; expected 'core' or one of the "
            f"declared extras: {', '.join(sorted(optional)) or '(none)'}"
        )

    components = []
    seen = set()  # dedupe by (name) -- a dep can appear in several extra groups

    # Root package as the first component.
    components.append(
        {
            "type": "library",
            "name": name,
            "version": version,
            "bom-ref": _purl(name, version),
            "purl": _purl(name, version),
            "licenses": [{"license": {"id": project.get("license", "MIT")}}]
            if isinstance(project.get("license"), str)
            else [{"license": {"id": "MIT"}}],
            "description": project.get("description", ""),
            "properties": [{"name": "hotato:scope", "value": "package"}],
        }
    )

    def add_dep(req, scope):
        dep_name = _req_name(req)
        if not dep_name:
            return
        key = dep_name.lower()
        if key in seen:
            return
        seen.add(key)
        components.append(
            {
                "type": "library",
                "name": dep_name,
                "version": "",  # unresolved by design: declared range, not a pin
                "bom-ref": _purl(dep_name),
                "purl": _purl(dep_name),
                "properties": [
                    {"name": "hotato:scope", "value": scope},
                    {"name": "hotato:requirement", "value": req.strip()},
                ],
            }
        )

    # Core (required) dependencies -- empty for hotato by design, but handled.
    for req in project.get("dependencies", []) or []:
        add_dep(req, "runtime")

    # The selected optional-dependencies groups, scope-tagged by group name.
    for group in groups:
        for req in optional.get(group) or []:
            add_dep(req, f"optional:{group}")

    profile_label = profile or "all"
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{_uuid_from(name, version, profile_label)}",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "tools": [{"name": "gen_sbom.py", "vendor": "hotato"}],
            "component": {
                "type": "application",
                "name": name,
                "version": version,
                "bom-ref": _purl(name, version),
                "purl": _purl(name, version),
                "properties": [{"name": "hotato:profile", "value": profile_label}],
            },
        },
        "components": components,
    }


def _uuid_from(name, version, profile="all"):
    """Deterministic UUIDv5 in the URL namespace, so re-running on the same
    tree+profile yields the same serialNumber (no random reruns churning the
    file). The whole-surface default keeps its historical seed; a scoped profile
    folds its name in so each profile gets its own stable serialNumber."""
    import uuid

    seed = f"hotato-sbom:{name}:{version}"
    if profile and profile != "all":
        seed += f":{profile}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def check_sbom(path):
    """Validate a generated SBOM is well-formed and carries the required
    CycloneDX fields. Returns a list of problem strings (empty == valid)."""
    problems = []
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"not readable well-formed JSON: {exc}"]

    if not isinstance(doc, dict):
        return ["top-level JSON is not an object"]

    for field in REQUIRED_TOP_LEVEL:
        if field not in doc:
            problems.append(f"missing top-level field: {field!r}")

    if doc.get("bomFormat") != "CycloneDX":
        problems.append(f"bomFormat is {doc.get('bomFormat')!r}, expected 'CycloneDX'")
    if not str(doc.get("specVersion", "")):
        problems.append("specVersion is empty")

    components = doc.get("components")
    if not isinstance(components, list) or not components:
        problems.append("components is missing, not a list, or empty")
    else:
        for i, comp in enumerate(components):
            if not isinstance(comp, dict):
                problems.append(f"component[{i}] is not an object")
                continue
            for field in REQUIRED_COMPONENT_FIELDS:
                if field not in comp:
                    problems.append(f"component[{i}] ({comp.get('name', '?')}) missing {field!r}")

    meta = doc.get("metadata")
    if not isinstance(meta, dict) or "component" not in meta:
        problems.append("metadata.component is missing")

    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate or validate a minimal CycloneDX SBOM for hotato."
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"output path for the generated SBOM (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--check",
        metavar="SBOM_JSON",
        help="validate an existing SBOM file instead of generating one",
    )
    parser.add_argument(
        "--pyproject",
        default=os.path.join(ROOT, "pyproject.toml"),
        help="path to pyproject.toml (default: repo root)",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="scope the SBOM to a dependency profile: 'core' (zero-dependency "
        "core only), an extra name (core plus that optional-dependencies "
        "group), or omit for the whole surface (core plus every extra). "
        "With a profile and no --out, writes dist/hotato.sbom.<profile>.cdx.json. "
        "See --list-profiles.",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="print the available profiles ('core' plus each declared extra), "
        "one per line, and exit -- lets CI emit one SBOM per profile.",
    )
    args = parser.parse_args(argv)

    if args.list_profiles:
        project = _load_pyproject(args.pyproject).get("project") or {}
        print("core")
        for group in sorted((project.get("optional-dependencies") or {})):
            print(group)
        return 0

    if args.check:
        problems = check_sbom(args.check)
        if problems:
            print(f"SBOM INVALID: {args.check}", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(f"SBOM OK: {args.check}")
        return 0

    sbom = build_sbom(_load_pyproject(args.pyproject), profile=args.profile)
    out = args.out
    # A profile with the default --out gets a per-profile filename so the
    # profiles never clobber each other or the whole-surface SBOM.
    if args.profile and out == DEFAULT_OUT:
        out = os.path.join(ROOT, "dist", f"hotato.sbom.{args.profile}.cdx.json")
    out_dir = os.path.dirname(os.path.abspath(out))
    os.makedirs(out_dir, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(sbom, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(
        f"wrote {out}: {len(sbom['components'])} components "
        f"({sbom['metadata']['component']['name']} "
        f"{sbom['metadata']['component']['version']}, "
        f"profile={args.profile or 'all'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
