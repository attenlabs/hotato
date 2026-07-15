"""R-06 tag-lock + stale-dist regression guards for scripts/build_release.py.

Two integrity properties, each written to FAIL on the pre-fix script (which
built ``git archive HEAD`` unconditionally and did ``DIST.mkdir(exist_ok=True)``
without ever clearing ``dist/``) and PASS on the fix:

  (1) TAG-LOCK: a default build REFUSES (exit 2) when HEAD is not exactly at the
      ``vX.Y.Z`` tag matching pyproject -- drift past the tag, or no tag at all
      -- so a "release" can never be cut from an untagged / post-tag commit. The
      pre-fix script ignored tags entirely and would have built (exit 0).

  (2) STALE-DIST: a successful build empties ``dist/`` first, so a stale
      artifact from a prior version cannot linger unlisted by SHA256SUMS. The
      pre-fix script kept an existing ``dist/`` and only copied the new files in.

The build-exercising test (2, and the --allow-drift case) needs the `build` +
`setuptools` toolchain and is skipped cleanly if absent; the refusal tests (1)
run everywhere -- the fix refuses before importing `build`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_release.py"


def _load_module():
    """Import scripts/build_release.py as a module (it is not on sys.path)."""
    spec = importlib.util.spec_from_file_location("build_release_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=kw.pop("check", True), **kw)


def _init_repo(tmp_path: Path, version: str = "0.1.0", tag: bool = True) -> Path:
    """A minimal, buildable git repo tagged v<version> at its only commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[build-system]\n'
        'requires = ["setuptools>=61"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\n'
        f'name = "taglock_demo"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    pkg = repo / "taglock_demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '%s'\n" % version, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "release")
    if tag:
        _git(repo, "tag", f"v{version}")
    return repo


def _point_module_at(mod, repo: Path):
    mod.REPO_ROOT = repo
    mod.DIST = repo / "dist"


# ---------------------------------------------------------------------------
# (1) TAG-LOCK refusals -- run everywhere (refuse happens before importing build)
# ---------------------------------------------------------------------------
def test_default_build_refuses_when_head_drifted_past_tag(tmp_path):
    repo = _init_repo(tmp_path, version="0.1.0")
    # A commit AFTER the tag: HEAD is now one past v0.1.0.
    (repo / "extra.txt").write_text("post-tag change\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "post-tag")

    # Sanity: HEAD really has drifted from the tag.
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    tag = _git(repo, "rev-parse", "v0.1.0^{commit}").stdout.strip()
    assert head != tag

    mod = _load_module()
    _point_module_at(mod, repo)
    rc = mod.main([])
    # Pre-fix built HEAD unconditionally (rc 0); the fix REFUSES drift (rc 2).
    assert rc == mod.EXIT_REFUSE == 2
    # And it refused *before building*: no artifacts were produced.
    assert not (repo / "dist").exists()


def test_default_build_refuses_when_no_matching_tag(tmp_path):
    repo = _init_repo(tmp_path, version="0.2.0", tag=False)
    mod = _load_module()
    _point_module_at(mod, repo)
    rc = mod.main([])
    assert rc == mod.EXIT_REFUSE == 2
    assert not (repo / "dist").exists()


def test_explicit_missing_ref_refuses(tmp_path):
    repo = _init_repo(tmp_path, version="0.3.0")
    mod = _load_module()
    _point_module_at(mod, repo)
    rc = mod.main(["--ref", "v9.9.9"])
    assert rc == mod.EXIT_REFUSE == 2


def test_clean_tag_at_head_is_accepted_by_resolver(tmp_path):
    # HEAD exactly at the tag: the resolver must NOT refuse (it returns a ref).
    repo = _init_repo(tmp_path, version="0.4.0")
    mod = _load_module()
    _point_module_at(mod, repo)
    resolved = mod._resolve_build_ref(mod.argparse.Namespace(ref=None, allow_drift=False))
    assert not isinstance(resolved, int), "clean tag-at-HEAD must resolve, not refuse"
    ref, _label = resolved
    assert ref == "v0.4.0"


# ---------------------------------------------------------------------------
# (2) STALE-DIST clearing + tag-faithful build -- needs the build toolchain
# ---------------------------------------------------------------------------
def _have_build_toolchain() -> bool:
    return (
        importlib.util.find_spec("build") is not None
        and importlib.util.find_spec("setuptools") is not None
    )


@pytest.mark.skipif(not _have_build_toolchain(),
                    reason="needs the `build` + `setuptools` toolchain")
def test_successful_build_empties_stale_dist(tmp_path):
    repo = _init_repo(tmp_path, version="0.5.0")
    dist = repo / "dist"
    dist.mkdir()
    # A stale artifact + stale SHA256SUMS from an imaginary prior release.
    stale = dist / "taglock_demo-0.0.1.tar.gz"
    stale.write_bytes(b"STALE")
    (dist / "SHA256SUMS").write_text("deadbeef  taglock_demo-0.0.1.tar.gz\n", encoding="utf-8")

    mod = _load_module()
    _point_module_at(mod, repo)
    rc = mod.main([])
    assert rc == 0, "a clean tag-at-HEAD build must succeed"

    names = sorted(p.name for p in dist.iterdir())
    # Pre-fix kept the stale file; the fix empties dist/ first.
    assert "taglock_demo-0.0.1.tar.gz" not in names, "stale artifact was not cleared from dist/"
    assert "SHA256SUMS" in names
    fresh = [n for n in names if n != "SHA256SUMS"]
    assert fresh and all(n.startswith("taglock_demo-0.5.0") for n in fresh), fresh

    # SHA256SUMS lists exactly the artifacts now present -- no stale entry.
    sums = (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    listed = sorted(line.split("  ", 1)[1] for line in sums if line)
    assert listed == sorted(fresh), (listed, fresh)
    assert "taglock_demo-0.0.1.tar.gz" not in sums


@pytest.mark.skipif(not _have_build_toolchain(),
                    reason="needs the `build` + `setuptools` toolchain")
def test_allow_drift_builds_off_head_when_drifted(tmp_path):
    repo = _init_repo(tmp_path, version="0.6.0")
    (repo / "extra.txt").write_text("post-tag\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "post-tag")

    mod = _load_module()
    _point_module_at(mod, repo)
    # Default refuses; the explicit escape hatch builds.
    assert mod.main([]) == 2
    assert mod.main(["--allow-drift"]) == 0
    assert (repo / "dist" / "SHA256SUMS").exists()


@pytest.mark.skipif(not _have_build_toolchain(),
                    reason="needs the `build` + `setuptools` toolchain")
def test_build_is_tag_faithful_ignoring_working_tree(tmp_path):
    # A working-tree / untracked file present at build time must NOT enter the
    # artifacts: the build comes from the immutable tag object.
    repo = _init_repo(tmp_path, version="0.7.0")
    (repo / "taglock_demo" / "SCRATCH_UNTRACKED.py").write_text("x = 1\n", encoding="utf-8")

    mod = _load_module()
    _point_module_at(mod, repo)
    assert mod.main([]) == 0

    sdists = sorted((repo / "dist").glob("*.tar.gz"))
    assert len(sdists) == 1, sdists
    with tarfile.open(sdists[0]) as tf:
        members = [m.name for m in tf.getmembers() if m.isfile()]
    assert not any("SCRATCH_UNTRACKED.py" in m for m in members), (
        "untracked working-tree file leaked into the tag-faithful sdist"
    )
