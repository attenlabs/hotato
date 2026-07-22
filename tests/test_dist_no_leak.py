"""The crown-jewel packaging guard (2026-07-22 law).

``.gitignore`` does not keep a file out of a built sdist (setuptools assembles
it from ``MANIFEST.in`` against the working tree, independent of git), so a
gitignored internal / crown-jewel / secret file on disk can still ship to PyPI.
Three independent layers defend against that; this module pins all three:

  1. the ``MANIFEST.in`` excludes for the internal SAA artifacts;
  2. ``scripts/check_dist_no_leak.py`` flags an untracked or forbidden member
     and passes a clean, tracked one (the fail-closed scanner's logic);
  3. the scanner is actually WIRED into CI (sdist-guard) and the publish
     workflow, before any upload.
"""

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER_PATH = os.path.join(ROOT, "scripts", "check_dist_no_leak.py")
MANIFEST = os.path.join(ROOT, "MANIFEST.in")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def _load_scanner():
    if not os.path.exists(SCANNER_PATH):
        pytest.skip("scripts/check_dist_no_leak.py not present (minimal sdist?)")
    spec = importlib.util.spec_from_file_location("hotato_check_dist_no_leak", SCANNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- (2) the scanner's forbidden-pattern logic ------------------------------

@pytest.mark.parametrize("path", [
    "docs/SAA-BEHAVIOR-CARD.md",
    "docs/SAA-FIX-POINTER.md",
    "docs/saa_behavior_card.py",
    "tests/test_saa_card.py",
    "HANDOFF-cc-01-orange.md",
    "deploy/.env",
    "deploy/.env.production",
    "config/credentials.json",
    "keys/id_rsa",
    "certs/server.pem",
    ".claude/settings.json",
    "knowledge/north-star/NORTH-STAR.md",
])
def test_forbidden_paths_are_flagged(path):
    scanner = _load_scanner()
    assert scanner._scan_forbidden(path) is not None, (
        f"{path!r} is an internal/secret path but the scanner did not flag it")


@pytest.mark.parametrize("path", [
    "src/hotato/cli.py",
    "tests/test_scan.py",
    "docs/OBSERVE.md",
    "corpus/classes/build_classes.py",
    "examples/interrupted-tool-call/double-fire/voice_trace.jsonl",
    "deploy/control-plane/.env.example",   # a PUBLIC template, must pass
    "README.md",
    "PKG-INFO",
])
def test_clean_paths_pass(path):
    scanner = _load_scanner()
    assert scanner._scan_forbidden(path) is None, (
        f"{path!r} is a legitimate public file but the scanner flagged it")


def test_check_archive_flags_untracked_and_forbidden(tmp_path):
    # An sdist member that is neither git-tracked nor build-generated is a leak,
    # and an SAA path is a forbidden-pattern leak, independent of tracking.
    scanner = _load_scanner()
    tracked = {"src/hotato/cli.py", "README.md"}
    # Build a tiny tar.gz that looks like an sdist.
    import io
    import tarfile
    p = tmp_path / "pkg-1.0.0.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        for member in [
            "pkg-1.0.0/src/hotato/cli.py",          # tracked -> ok
            "pkg-1.0.0/README.md",                   # tracked -> ok
            "pkg-1.0.0/PKG-INFO",                    # generated -> ok
            "pkg-1.0.0/scratch/notes.txt",           # UNTRACKED -> leak
            "pkg-1.0.0/docs/SAA-FIX-POINTER.md",     # FORBIDDEN -> leak
        ]:
            data = b"x"
            info = tarfile.TarInfo(name=member)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    findings = scanner._check_archive(str(p), tracked)
    joined = "\n".join(findings)
    assert "scratch/notes.txt" in joined, "untracked member not caught"
    assert "SAA-FIX-POINTER.md" in joined, "forbidden SAA member not caught"
    assert "cli.py" not in joined and "README.md" not in joined and "PKG-INFO" not in joined


# --- (1) the MANIFEST excludes ----------------------------------------------

def test_manifest_excludes_the_saa_artifacts():
    if not os.path.exists(MANIFEST):
        pytest.skip("MANIFEST.in not present (minimal sdist?)")
    with open(MANIFEST, encoding="utf-8") as fh:
        text = fh.read()
    for path in ("docs/SAA-BEHAVIOR-CARD.md", "docs/SAA-FIX-POINTER.md",
                 "docs/saa_behavior_card.py", "tests/test_saa_card.py"):
        assert path in text, f"MANIFEST.in does not exclude the internal {path}"


# --- (3) the scanner is wired into CI and the publish workflow ---------------

def _all_workflow_text():
    if not os.path.isdir(WORKFLOWS):
        return ""
    parts = []
    for f in sorted(os.listdir(WORKFLOWS)):
        if f.endswith((".yml", ".yaml")):
            with open(os.path.join(WORKFLOWS, f), encoding="utf-8") as fh:
                parts.append(fh.read())
    return "\n".join(parts)


def test_scanner_is_wired_into_ci_and_publish():
    text = _all_workflow_text()
    if not text:
        pytest.skip("no workflow files present (minimal sdist?)")
    assert "scripts/check_dist_no_leak.py" in text, (
        "check_dist_no_leak.py is not invoked by any workflow; the fail-closed "
        "leak gate must run in CI (sdist-guard) and before the PyPI upload")
    # It must guard the publish path specifically, not only CI.
    pub = os.path.join(WORKFLOWS, "publish-pypi-oidc.yml")
    if os.path.exists(pub):
        with open(pub, encoding="utf-8") as fh:
            assert "check_dist_no_leak.py" in fh.read(), (
                "the publish workflow does not run the leak scan before upload")
