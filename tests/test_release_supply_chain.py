"""Release supply-chain wiring guards (§4 packaging & supply chain).

Pure Python, no network and no third-party dependency (PyYAML is used only if
present; otherwise the workflows are parsed as text with regex). These tests
assert that the CI wiring for the hardening tranche stays in place:

  (a) every GitHub Actions ``uses:`` is pinned to a full 40-hex commit SHA
      (with a ``# vX.Y.Z`` comment), never a mutable tag/branch;
  (b) the release path invokes ``scripts/gen_sbom.py`` (SBOM generation);
  (c) a build-provenance attestation step exists
      (``actions/attest-build-provenance``);
  (d) a reproducible / second-build ``sha256sum`` comparison step exists;
  (e) ``scripts/gen_sbom.py --check`` passes on a freshly generated SBOM
      (generated into a temp dir), for the whole-surface and a scoped profile.

The tests skip cleanly if the workflow files or ``scripts/gen_sbom.py`` are not
present (e.g. a minimal sdist without them), rather than hard-failing.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")
GEN_SBOM = os.path.join(ROOT, "scripts", "gen_sbom.py")

# A ``uses:`` line: an optional leading "- ", the key, then owner/repo@ref and an
# optional trailing "# comment".
_USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)")
# The pin we require: @<40 hex> terminated by whitespace or end-of-string, so a
# trailing "# vX.Y.Z" comment does not fool the check.
_SHA_PIN = re.compile(r"@[0-9a-f]{40}(?:\s|$)")
# Same, but also demanding the "# vX" provenance comment (fleet SHA-pin law).
_SHA_PIN_WITH_COMMENT = re.compile(r"@[0-9a-f]{40}\s+#\s*v")


def _workflow_files():
    if not os.path.isdir(WORKFLOWS_DIR):
        return []
    return [
        os.path.join(WORKFLOWS_DIR, f)
        for f in sorted(os.listdir(WORKFLOWS_DIR))
        if f.endswith((".yml", ".yaml"))
    ]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _uses_lines():
    """(workflow_path, line_number, line_text) for every ``uses:`` line."""
    out = []
    for path in _workflow_files():
        for i, line in enumerate(_read(path).splitlines(), start=1):
            if _USES_LINE.match(line) and "uses:" in line:
                out.append((path, i, line))
    return out


def _all_workflow_text():
    return "\n".join(_read(p) for p in _workflow_files())


# ---------------------------------------------------------------------------
# (a) every `uses:` is pinned to a 40-hex commit SHA (+ a # vX comment)
# ---------------------------------------------------------------------------
def _is_local_action(line):
    """A repository-local ``uses: ./...`` (the root ``action.yml`` via ``./`` or a
    composite action under ``./.github/actions/...``) runs the exact checked-out
    commit, so it is immutable by construction and carries no remote ref to pin.
    Every local ``./``-prefixed form is exempt; a remote ``owner/repo@ref`` still
    needs the 40-hex SHA."""
    m = _USES_LINE.match(line)
    return bool(m) and m.group(1).startswith("./")


def test_every_uses_is_sha_pinned():
    lines = _uses_lines()
    if not lines:
        pytest.skip("no workflow files present (minimal sdist?)")
    unpinned = [
        f"{os.path.basename(p)}:{n}: {line.strip()}"
        for p, n, line in lines
        if not _SHA_PIN.search(line) and not _is_local_action(line)
    ]
    assert not unpinned, "un-SHA-pinned `uses:` (need @<40-hex>):\n" + "\n".join(unpinned)


def test_every_uses_has_version_comment():
    lines = _uses_lines()
    if not lines:
        pytest.skip("no workflow files present (minimal sdist?)")
    missing = [
        f"{os.path.basename(p)}:{n}: {line.strip()}"
        for p, n, line in lines
        if not _SHA_PIN_WITH_COMMENT.search(line) and not _is_local_action(line)
    ]
    assert not missing, "SHA pin without a `# vX.Y.Z` comment:\n" + "\n".join(missing)


# ---------------------------------------------------------------------------
# (b) the release path references scripts/gen_sbom.py
# ---------------------------------------------------------------------------
def test_release_path_generates_sbom():
    files = _workflow_files()
    if not files:
        pytest.skip("no workflow files present (minimal sdist?)")
    referencing = [
        os.path.basename(p) for p in files if "gen_sbom.py" in _read(p)
    ]
    assert referencing, "no workflow invokes scripts/gen_sbom.py"
    # The SBOM must be validated, not just generated.
    assert "gen_sbom.py --check" in _all_workflow_text(), (
        "gen_sbom.py is generated but never validated with --check in CI"
    )


# ---------------------------------------------------------------------------
# (c) a build-provenance attestation step exists
# ---------------------------------------------------------------------------
def test_build_provenance_attestation_step_exists():
    files = _workflow_files()
    if not files:
        pytest.skip("no workflow files present (minimal sdist?)")
    text = _all_workflow_text()
    assert "actions/attest-build-provenance" in text, (
        "no actions/attest-build-provenance step found in any workflow"
    )
    # It must be a real, SHA-pinned action reference.
    assert re.search(
        r"uses:\s*actions/attest-build-provenance@[0-9a-f]{40}", text
    ), "attest-build-provenance is referenced but not SHA-pinned as a `uses:`"


def test_attestation_job_grants_write_permission():
    files = _workflow_files()
    if not files:
        pytest.skip("no workflow files present (minimal sdist?)")
    # The job carrying the attestation needs both id-token: write and
    # attestations: write for the signing to work.
    text = _all_workflow_text()
    assert "attestations: write" in text, (
        "attest-build-provenance requires `attestations: write`, not granted anywhere"
    )
    assert "id-token: write" in text, "id-token: write is required for attestation/OIDC"


# ---------------------------------------------------------------------------
# (d) a reproducible / second-build hash-comparison step exists
# ---------------------------------------------------------------------------
def test_reproducible_second_build_hash_check_exists():
    files = _workflow_files()
    if not files:
        pytest.skip("no workflow files present (minimal sdist?)")
    text = _all_workflow_text()
    assert "sha256sum" in text, "no sha256sum hash step found (reproducible-build check)"
    # A *second* build compared against the first: the check builds into a
    # separate output dir and pins the backend + SOURCE_DATE_EPOCH.
    assert "dist-verify" in text, (
        "no second-build output dir (dist-verify) found; the reproducibility "
        "check must build a SECOND time and compare hashes"
    )
    assert "SOURCE_DATE_EPOCH" in text, (
        "reproducible build must set SOURCE_DATE_EPOCH for deterministic timestamps"
    )


def test_trusted_publish_uses_the_canonical_build_action():
    workflow = os.path.join(WORKFLOWS_DIR, "publish-pypi-oidc.yml")
    action = os.path.join(ROOT, ".github", "actions", "build-python-dist", "action.yml")
    if not os.path.exists(workflow) or not os.path.exists(action):
        pytest.skip("canonical build action or trusted-publish workflow is absent")
    assert re.search(
        r"(?m)^\s*uses:\s+\./\.github/actions/build-python-dist\s*$",
        _read(workflow),
    ), (
        "trusted publishing must use the same repository-owned pinned build "
        "action as release validation"
    )
    action_text = _read(action)
    assert 'echo "SOURCE_DATE_EPOCH=$source_date_epoch" >> "$GITHUB_ENV"' in action_text, (
        "the canonical action must export its commit-derived epoch for the "
        "trusted workflow's second build"
    )


# ---------------------------------------------------------------------------
# (e) gen_sbom.py --check passes on a freshly generated SBOM
# ---------------------------------------------------------------------------
def _run(args, **kw):
    return subprocess.run(
        [sys.executable, GEN_SBOM, *args],
        capture_output=True,
        text=True,
        **kw,
    )


@pytest.mark.skipif(
    not os.path.exists(GEN_SBOM) or sys.version_info < (3, 11),
    reason="gen_sbom.py is release tooling that needs Python 3.11+ (tomllib)",
)
def test_gen_sbom_generate_then_check_passes(tmp_path):
    out = str(tmp_path / "hotato.sbom.cdx.json")
    gen = _run(["--out", out])
    assert gen.returncode == 0, f"gen_sbom failed: {gen.stderr or gen.stdout}"
    assert os.path.exists(out)
    chk = _run(["--check", out])
    assert chk.returncode == 0, f"--check failed: {chk.stderr or chk.stdout}"
    assert "SBOM OK" in chk.stdout


@pytest.mark.skipif(
    not os.path.exists(GEN_SBOM) or sys.version_info < (3, 11),
    reason="gen_sbom.py is release tooling that needs Python 3.11+ (tomllib)",
)
def test_gen_sbom_profile_generate_then_check_passes(tmp_path):
    # A scoped profile ("core") must also produce a valid, --check-clean SBOM.
    out = str(tmp_path / "hotato.sbom.core.cdx.json")
    gen = _run(["--profile", "core", "--out", out])
    assert gen.returncode == 0, f"gen_sbom --profile core failed: {gen.stderr or gen.stdout}"
    chk = _run(["--check", out])
    assert chk.returncode == 0, f"--check failed: {chk.stderr or chk.stdout}"
    assert "SBOM OK" in chk.stdout


@pytest.mark.skipif(
    not os.path.exists(GEN_SBOM) or sys.version_info < (3, 11),
    reason="gen_sbom.py is release tooling that needs Python 3.11+ (tomllib)",
)
def test_gen_sbom_list_profiles_includes_core():
    res = _run(["--list-profiles"])
    assert res.returncode == 0, f"--list-profiles failed: {res.stderr or res.stdout}"
    profiles = res.stdout.split()
    assert "core" in profiles, f"--list-profiles must include 'core'; got {profiles}"


# ---------------------------------------------------------------------------
# (f) the SBOM MACHINE-DECLARES its scope: declared-direct deps, NOT a resolved
#     transitive closure. gen_sbom is offline/stdlib-only and never resolves,
#     downloads, or introspects installed packages, so the document must SAY SO
#     in machine-readable form -- a consumer parsing the CycloneDX JSON must be
#     able to tell it is the manifest-level surface, not the full resolved graph,
#     and must never mistake it for a pinned closure.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.path.exists(GEN_SBOM) or sys.version_info < (3, 11),
    reason="gen_sbom.py is release tooling that needs Python 3.11+ (tomllib)",
)
def test_gen_sbom_declares_declared_direct_scope(tmp_path):
    out = str(tmp_path / "hotato.sbom.cdx.json")
    gen = _run(["--out", out])
    assert gen.returncode == 0, f"gen_sbom failed: {gen.stderr or gen.stdout}"
    with open(out, encoding="utf-8") as fh:
        doc = json.load(fh)

    props = {p["name"]: p["value"] for p in doc["metadata"].get("properties", [])}
    assert props.get("hotato:dependency-scope") == "declared-direct", (
        "the SBOM must machine-declare metadata.properties "
        "hotato:dependency-scope=declared-direct so consumers know it lists the "
        "declared direct surface, not the resolved transitive graph"
    )
    assert props.get("hotato:transitive-resolved") == "false", (
        "the SBOM must machine-declare hotato:transitive-resolved=false: it "
        "never resolves, pins, or introspects the transitive closure"
    )

    # Every dependency component is UNRESOLVED (empty version == declared range,
    # not a pin), so the document can never be read as a resolved closure.
    deps = [c for c in doc["components"] if c.get("name") != "hotato"]
    assert deps, "expected at least one dependency component in the whole-surface SBOM"
    unpinned = [c["name"] for c in deps if c.get("version") != ""]
    assert not unpinned, (
        "dependency components must carry an empty (unresolved) version -- a "
        f"non-empty version implies a resolved pin the tool never produces: {unpinned}"
    )


# ---------------------------------------------------------------------------
# (g) every publish path builds byte-reproducibly. The OIDC workflow already
#     sets SOURCE_DATE_EPOCH + a pinned backend; the manual twine FALLBACK in
#     the release checklist must do the same, or a hand-published wheel's ZIP
#     timestamps/modes drift from a rebuild and the byte-reproducibility claim
#     would not hold for that artifact.
# ---------------------------------------------------------------------------
def test_fallback_publish_path_builds_reproducibly():
    checklist = os.path.join(ROOT, "docs", "RELEASE-CHECKLIST.md")
    if not os.path.exists(checklist):
        pytest.skip("RELEASE-CHECKLIST.md not present (minimal sdist?)")
    text = _read(checklist)
    idx = text.find("Fallback: manual token upload")
    assert idx != -1, "no 'Fallback: manual token upload' section in the checklist"
    fallback = text[idx:]
    assert "SOURCE_DATE_EPOCH" in fallback, (
        "the manual twine fallback must build with SOURCE_DATE_EPOCH (and the "
        "pinned backend) so a hand-published wheel is byte-reproducible; "
        "otherwise its ZIP timestamps/modes drift from a rebuild"
    )


def test_fallback_publish_path_mirrors_canonical_toolchain():
    checklist = os.path.join(ROOT, "docs", "RELEASE-CHECKLIST.md")
    action = os.path.join(ROOT, ".github", "actions", "build-python-dist", "action.yml")
    if not os.path.exists(checklist) or not os.path.exists(action):
        pytest.skip("release checklist or canonical build action is absent")
    fallback = _read(checklist).split("Fallback: manual token upload", 1)[-1]
    canonical = _read(action)
    pin_pattern = r'"((?:pip|build|setuptools|wheel|twine)==[^"\s]+)"'
    canonical_pins = re.findall(pin_pattern, canonical)
    fallback_pins = re.findall(pin_pattern, fallback)
    assert len(canonical_pins) == 5, (
        f"expected five exact canonical tool pins, got {canonical_pins}"
    )
    assert fallback_pins == canonical_pins, (
        "manual publish toolchain must exactly mirror canonical pins; "
        f"canonical={canonical_pins}, fallback={fallback_pins}"
    )
    assert "twine check --strict" in fallback, "manual fallback must retain strict metadata checks"


# ---------------------------------------------------------------------------
# (h) tag-faithfulness: an sdist built from `git archive HEAD` contains ONLY
#     git-tracked paths (modulo the build-generated PKG-INFO / setup.cfg /
#     *.egg-info members). The published 1.6.1 and 1.6.2 sdists carried ~1,300
#     generated working-tree files (1,151 under examples/reference-agent/.out/
#     plus untracked corpus renders) because the release was built in place;
#     generated corpus files land NEXT TO tracked ones, so MANIFEST.in patterns
#     cannot separate them -- this member-level check is the enforcement, and
#     scripts/build_release.py is the build path that satisfies it by
#     construction. Builds one sdist with the declared PEP 517 backend
#     (setuptools.build_meta, a dev dependency), bounded well under a minute;
#     skips cleanly outside a git checkout (e.g. CI's extracted-sdist run).
# ---------------------------------------------------------------------------
def _git(*args):
    return subprocess.run(
        ["git", "-C", ROOT, *args], capture_output=True, check=False
    )


# Members setuptools generates into every sdist; everything else must be a
# git-tracked path.
_BUILD_GENERATED = ("PKG-INFO", "setup.cfg")


def test_sdist_from_git_archive_ships_only_tracked_paths(tmp_path):
    try:
        head = _git("rev-parse", "--verify", "HEAD")
    except FileNotFoundError:
        pytest.skip("git is not available")
    if head.returncode != 0:
        pytest.skip("not a git checkout (extracted sdist?)")
    setuptools = pytest.importorskip("setuptools")
    if int(setuptools.__version__.split(".")[0]) < 77:
        pytest.skip("building needs setuptools >= 77 (PEP 639 metadata)")

    tracked = set(
        _git("ls-tree", "-r", "--name-only", "HEAD")
        .stdout.decode("utf-8")
        .splitlines()
    )
    assert tracked, "git ls-tree returned no tracked paths"

    src = tmp_path / "src"
    src.mkdir()
    archive = _git("archive", "--format=tar", "HEAD")
    assert archive.returncode == 0, archive.stderr.decode("utf-8", "replace")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(src, filter="data")
        else:  # pragma: no cover -- pre-3.12 interpreters
            tf.extractall(src)

    out = tmp_path / "dist"
    out.mkdir()
    built = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from setuptools import build_meta; "
            "print(build_meta.build_sdist(sys.argv[1]))",
            str(out),
        ],
        cwd=src,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, (
        f"sdist build failed:\n{built.stdout}\n{built.stderr}"
    )
    sdists = sorted(out.glob("*.tar.gz"))
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"

    with tarfile.open(sdists[0]) as tf:
        member_paths = [m.name for m in tf.getmembers() if m.isfile()]
    assert member_paths, "sdist has no file members"

    # The H-01 signature first, by name, so a regression reads instantly.
    out_members = [p for p in member_paths if "/.out/" in p]
    assert not out_members, (
        "sdist carries generated reference-agent output "
        f"(examples/reference-agent/.out/): {out_members[:5]} ..."
    )

    untracked = []
    for path in member_paths:
        rel = path.split("/", 1)[1] if "/" in path else path
        if rel in _BUILD_GENERATED or ".egg-info" in rel:
            continue
        if rel not in tracked:
            untracked.append(rel)
    assert not untracked, (
        f"{len(untracked)} sdist member(s) are not git-tracked paths -- a "
        "release built this way would ship working-tree contamination "
        "(H-01: 1.6.1/1.6.2 shipped generated .out/ and corpus files). "
        "Build releases with scripts/build_release.py. First offenders: "
        + ", ".join(untracked[:10])
    )
