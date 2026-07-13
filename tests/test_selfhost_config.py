"""Config-level tests for the self-host deployment (Dockerfile / compose /
verify script / SELF-HOST.md). These do NOT require a Docker daemon: they lint
the deployment files so a broken container config fails in CI, not in a customer's
VPC. A real ``docker build`` + ``docker compose config`` is run separately (and
only opportunistically, gated on a running daemon) by ``test_docker_build_smoke``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
VERIFY = REPO_ROOT / "deploy" / "verify-zero-egress.sh"
ENTRYPOINT = REPO_ROOT / "deploy" / "entrypoint.sh"
HEALTHCHECK = REPO_ROOT / "deploy" / "healthcheck.py"
SEED = REPO_ROOT / "deploy" / "seed-demo.py"
SELF_HOST = REPO_ROOT / "docs" / "SELF-HOST.md"

# The vouching / decorative-authenticity words the copy law strips from docs.
_VOUCHING = (
    "real", "really", "actual", "actually", "honest", "honestly",
    "genuine", "genuinely", "truly",
)


def _banned_phrases() -> list[str]:
    """The package's own overclaim phrase list, imported when available so this
    test and copy_lint share one source of truth; falls back to a copy."""
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import copy_lint  # type: ignore
        return list(copy_lint.BANNED_PHRASES)
    except Exception:  # pragma: no cover - fallback only
        return [
            "verified fix", "fix verified", "proves the fix", "proves a fix",
            "bug cannot come back", "every push replays", "same on any machine",
            "a red build means the audio changed",
            "every failure points at a concrete fix", "private by construction",
            "keep it from coming back",
        ]


# =========================================================================
# All the files exist
# =========================================================================

def test_all_deployment_files_present():
    for f in (DOCKERFILE, COMPOSE, DOCKERIGNORE, VERIFY, ENTRYPOINT,
              HEALTHCHECK, SEED, SELF_HOST):
        assert f.is_file(), f"missing deployment file: {f}"


# =========================================================================
# Dockerfile lint (no daemon)
# =========================================================================

def test_dockerfile_runs_as_non_root():
    text = DOCKERFILE.read_text(encoding="utf-8")
    users = re.findall(r"(?mi)^\s*USER\s+(\S+)", text)
    assert users, "Dockerfile declares no USER (would run as root)"
    last = users[-1].strip()
    assert last not in ("root", "0"), f"final USER is privileged: {last!r}"


def test_dockerfile_has_healthcheck():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"(?mi)^\s*HEALTHCHECK\b", text), "Dockerfile has no HEALTHCHECK"


def test_dockerfile_no_add_from_url():
    """ADD from a URL fetches at build time (supply-chain surface); use COPY."""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.upper().startswith("ADD "):
            assert not re.search(r"https?://", s), f"ADD from a URL: {s!r}"


def test_dockerfile_no_curl_pipe_shell():
    """No `curl … | sh` / `wget … | bash` pattern (unpinned remote execution).
    Scans instruction lines only, so a comment that merely names the anti-pattern
    is not a false positive."""
    instr = "\n".join(
        ln for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#"))
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(sh|bash)\b", instr), \
        "Dockerfile pipes a download straight into a shell"


def test_dockerfile_is_multistage_slim_and_exposes_port():
    text = DOCKERFILE.read_text(encoding="utf-8")
    froms = re.findall(r"(?mi)^\s*FROM\s+(\S+)", text)
    assert len(froms) >= 2, "Dockerfile is not multi-stage"
    assert any("slim" in f for f in froms), "no slim base image"
    assert re.search(r"(?mi)^\s*EXPOSE\s+8321\b", text), "does not EXPOSE 8321"


def test_dockerfile_installs_from_local_source():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY src" in text, "Dockerfile does not COPY the local source"
    assert "pip install" in text, "Dockerfile does not pip install"
    # installs the current dir, either literally (`pip install .`) or via a
    # computed spec (`SPEC="."` / `SPEC=".[extras]"` then `pip install "$SPEC"`).
    installs_local = (
        'SPEC="."' in text
        or re.search(r'SPEC="\.\[', text) is not None
        or re.search(r"pip install[^\n]*\s\.(\[|\"|\s|$)", text) is not None
    )
    assert installs_local, "Dockerfile does not pip install from the local source"


# =========================================================================
# docker-compose.yml structure (parsed, no daemon)
# =========================================================================

@pytest.fixture(scope="module")
def compose():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_is_valid_yaml_with_expected_services(compose):
    services = compose.get("services", {})
    for name in ("hotato-workspace", "hotato-init", "ollama"):
        assert name in services, f"compose missing service {name!r}"


def test_compose_profiles(compose):
    services = compose["services"]
    assert "demo" in services["hotato-init"].get("profiles", []), \
        "hotato-init must be behind the 'demo' profile"
    assert "judge" in services["ollama"].get("profiles", []), \
        "ollama must be behind the 'judge' profile"
    # the workspace itself must NOT be gated behind a profile
    assert not services["hotato-workspace"].get("profiles"), \
        "the workspace must start by default (no profile)"


def test_compose_publishes_only_loopback_workspace_port(compose):
    services = compose["services"]
    ws_ports = services["hotato-workspace"].get("ports", [])
    assert ws_ports == ["127.0.0.1:8321:8321"], \
        f"workspace must publish exactly 127.0.0.1:8321:8321, got {ws_ports}"
    # neither the judge nor the seeder may publish anything
    assert not services["ollama"].get("ports"), "ollama must publish no port"
    assert not services["hotato-init"].get("ports"), "hotato-init must publish no port"


def test_compose_no_wildcard_or_extra_publish(compose):
    for name, svc in compose["services"].items():
        for spec in svc.get("ports", []) or []:
            spec = str(spec)
            assert spec.startswith("127.0.0.1:"), \
                f"service {name} publishes on a non-loopback interface: {spec!r}"


def test_compose_volumes_declared(compose):
    vols = compose.get("volumes", {})
    for v in ("hotato-data", "ollama-models"):
        assert v in vols, f"compose missing named volume {v!r}"
    # the workspace mounts the data volume at /data
    mounts = compose["services"]["hotato-workspace"].get("volumes", [])
    assert any("hotato-data:/data" in str(m) for m in mounts), \
        "workspace does not mount hotato-data at /data"


def test_compose_judge_endpoint_wired(compose):
    env = compose["services"]["hotato-workspace"].get("environment", {})
    if isinstance(env, list):
        env = dict(e.split("=", 1) for e in env if "=" in e)
    assert env.get("HOTATO_JUDGE_ENDPOINT", "").startswith("http://ollama:"), \
        "HOTATO_JUDGE_ENDPOINT is not wired to the ollama service"


# =========================================================================
# verify-zero-egress.sh basics (no daemon)
# =========================================================================

def test_verify_script_executable_and_strict():
    assert os.access(VERIFY, os.X_OK), "verify-zero-egress.sh is not executable"
    head = VERIFY.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("#!") and ("bash" in head or "sh" in head), \
        "verify-zero-egress.sh has no shell shebang"
    body = VERIFY.read_text(encoding="utf-8")
    assert re.search(r"(?m)^set\s+-[a-z]*e", body), \
        "verify-zero-egress.sh does not `set -e`"


@pytest.mark.parametrize("script", [VERIFY, ENTRYPOINT])
def test_shell_scripts_parse(script):
    """`bash -n` (and shellcheck if installed) must accept the shell scripts."""
    bash = shutil.which("bash")
    if bash:
        r = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, f"bash -n failed for {script.name}: {r.stderr}"
    sc = shutil.which("shellcheck")
    if sc:
        r = subprocess.run([sc, "-S", "error", str(script)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"shellcheck errors in {script.name}: {r.stdout}"


def test_python_deploy_helpers_compile():
    import py_compile
    for f in (HEALTHCHECK, SEED):
        py_compile.compile(str(f), doraise=True)


# =========================================================================
# docs/SELF-HOST.md content
# =========================================================================

@pytest.fixture(scope="module")
def selfhost_text():
    return SELF_HOST.read_text(encoding="utf-8")


REQUIRED_SECTIONS = [
    "Prerequisites",
    "Build",
    "Bring it up",
    "First boot",          # demo/example data
    "Connect your own data",
    "judge",               # enable the local model judge
    "Credentials",
    "Backup",
    "Upgrade",
    "Zero-migration",
    "Air-gapped",
    "no external calls",   # the egress honesty boundary section
]


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_selfhost_has_required_section(selfhost_text, section):
    assert section.lower() in selfhost_text.lower(), \
        f"SELF-HOST.md is missing a section about: {section!r}"


def test_selfhost_no_banned_overclaim_phrases(selfhost_text):
    low = selfhost_text.lower()
    hits = [p for p in _banned_phrases() if p in low]
    assert not hits, f"SELF-HOST.md contains banned overclaim phrase(s): {hits}"


def test_selfhost_no_vouching_words(selfhost_text):
    hits = sorted({
        w for w in _VOUCHING
        if re.search(r"\b" + re.escape(w) + r"\b", selfhost_text, re.IGNORECASE)
    })
    assert not hits, f"SELF-HOST.md uses vouching word(s) (strip them): {hits}"


def test_selfhost_scopes_zero_egress_and_names_the_pull(selfhost_text):
    low = selfhost_text.lower()
    # never claim air-gapped-by-default; the judge pull is a documented download
    assert "air-gapped by default" not in low or "do not describe" in low, \
        "SELF-HOST.md must not claim 'air-gapped by default'"
    assert "download" in low and "model" in low, \
        "SELF-HOST.md must name the one-time model download"
    # the egress claim must point at EGRESS.md for the opt-in paths
    assert "EGRESS.md" in selfhost_text, \
        "SELF-HOST.md must link EGRESS.md for the opt-in egress paths"


def test_selfhost_states_zero_migration_same_schemas(selfhost_text):
    low = re.sub(r"\s+", " ", selfhost_text.lower())  # tolerate line wraps
    assert "same schema" in low, \
        "SELF-HOST.md must state the same-schemas (zero-migration) promise"


# =========================================================================
# Optional: real docker build + compose config (only if a daemon is up)
# =========================================================================

def _docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    return subprocess.run([docker, "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_available(),
                    reason="no Docker daemon; config-level tests cover the rest")
def test_docker_compose_config_valid():
    r = subprocess.run(["docker", "compose", "config"], cwd=str(REPO_ROOT),
                       capture_output=True, text=True)
    assert r.returncode == 0, f"docker compose config failed: {r.stderr}"


@pytest.mark.skipif(
    not (_docker_available() and os.environ.get("HOTATO_SELFHOST_DOCKER_BUILD") == "1"),
    reason="set HOTATO_SELFHOST_DOCKER_BUILD=1 with a daemon to run the slow build smoke")
def test_docker_build_smoke():
    r = subprocess.run(
        ["docker", "build", "-t", "hotato-selfhost:pytest", "."],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert r.returncode == 0, f"docker build failed: {r.stderr[-2000:]}"
