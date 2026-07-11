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

import os
import re
import subprocess
import sys

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
def test_every_uses_is_sha_pinned():
    lines = _uses_lines()
    if not lines:
        pytest.skip("no workflow files present (minimal sdist?)")
    unpinned = [
        f"{os.path.basename(p)}:{n}: {line.strip()}"
        for p, n, line in lines
        if not _SHA_PIN.search(line)
    ]
    assert not unpinned, "un-SHA-pinned `uses:` (need @<40-hex>):\n" + "\n".join(unpinned)


def test_every_uses_has_version_comment():
    lines = _uses_lines()
    if not lines:
        pytest.skip("no workflow files present (minimal sdist?)")
    missing = [
        f"{os.path.basename(p)}:{n}: {line.strip()}"
        for p, n, line in lines
        if not _SHA_PIN_WITH_COMMENT.search(line)
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
