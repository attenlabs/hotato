"""The Atlas collector (hotato-atlas) is a private package: it lives in this project's own private
Python repository and is installed into the jobs image at build time, never committed here.
These tests hold the wiring in place: private repository, read-only build access, exact pin, no
public index for the name, no credential in the image, and the interface atlas_job.py uses."""
import importlib
import importlib.util
import os
import re
import sys
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
AUTOPILOT = os.path.dirname(HERE)
REPO = os.path.dirname(AUTOPILOT)


def read(*parts):
    return open(os.path.join(AUTOPILOT, *parts), encoding="utf-8").read()


def tf_block(text, kind, name):
    m = re.search(r'resource "%s" "%s" \{\n(.*?)\n\}\n' % (re.escape(kind), re.escape(name)), text, re.S)
    assert m, f"{kind}.{name} not found"
    return m.group(1)


def cloudbuild_step(step_id):
    text = read("build", "cloudbuild.yaml")
    m = re.search(r"\n  - id: %s\n(.*?)(?=\n  - id: |\n  # |\Z)" % re.escape(step_id), text, re.S)
    assert m, f"step {step_id} not found"
    return m.group(1)


def wait_for(step):
    m = re.search(r"waitFor: \[(.*?)\]", step)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


# --- infra
def test_private_python_repository_is_python_format_and_never_public():
    repo = tf_block(read("infra", "main.tf"), "google_artifact_registry_repository", "private_py")
    assert 'repository_id = "hotato-private-py"' in repo
    assert 'format        = "PYTHON"' in repo
    # No Artifact Registry resource names a public principal (the only public grant in the infra is
    # the opt-in monitoring service, monitor_public).
    for f in os.listdir(os.path.join(AUTOPILOT, "infra")):
        if f.endswith(".tf"):
            for m in re.finditer(r'resource "(\w+)" "\w+" \{\n(.*?)\n\}\n', read("infra", f), re.S):
                if m.group(1).startswith("google_artifact_registry"):
                    assert "allUsers" not in m.group(2) and "allAuthenticatedUsers" not in m.group(2), f


def test_build_only_reads_the_private_repository():
    iam = read("infra", "iam.tf")
    py = tf_block(iam, "google_artifact_registry_repository_iam_member", "build_private_py")
    assert "google_artifact_registry_repository.private_py.name" in py
    assert 'role       = "roles/artifactregistry.reader"' in py
    assert "google_service_account.build.email" in py
    # No identity gets a write role on the private repository in Terraform (the owner uploads).
    for m in re.finditer(r'resource "google_artifact_registry_repository_iam_\w+" "\w+" \{\n(.*?)\n\}\n', iam, re.S):
        if "private_py" in m.group(1):
            assert "artifactregistry.reader" in m.group(1)
    # Image pushes are granted on the image repository only, not project-wide.
    assert "roles/artifactregistry.writer" not in tf_block(iam, "google_project_iam_member", "build")
    images = tf_block(iam, "google_artifact_registry_repository_iam_member", "build_images")
    assert "google_artifact_registry_repository.images.name" in images and "artifactregistry.writer" in images


# --- build
def test_atlas_version_is_an_exact_pin():
    text = read("build", "cloudbuild.yaml")
    m = re.search(r'^  _ATLAS_VERSION: "([^"]*)"$', text, re.M)  # the form publish-atlas-wheel.sh reads
    assert m and re.fullmatch(r"\d+\.\d+\.\d+", m.group(1))
    assert re.search(r"^  _ATLAS_PY_REPO: hotato-private-py$", text, re.M)


def test_wheel_step_uses_only_the_private_index():
    step = cloudbuild_step("atlas-wheel")
    assert '--index-url "https://${_REGION}-python.pkg.dev/$PROJECT_ID/${_ATLAS_PY_REPO}/simple/"' in step
    assert '"hotato-atlas==${_ATLAS_VERSION}"' in step
    assert "--no-deps" in step and "--only-binary=:all:" in step
    assert "extra-index-url" not in read("build", "cloudbuild.yaml")
    assert re.search(r"keyrings\.google-artifactregistry-auth==\d", step)
    assert "--dest autopilot/build/private-wheels" in step
    # The leak scan never reads the wheel, and the monitor image (which copies the whole
    # workspace) is built before the wheel is there.
    assert {"leak-scan", "image-monitor"} <= set(wait_for(step))
    assert "atlas-wheel" not in wait_for(cloudbuild_step("image-monitor"))


def test_jobs_image_installs_the_downloaded_wheel_without_an_index():
    step = cloudbuild_step("image-jobs")
    assert "atlas-wheel" in wait_for(step)
    assert '"--build-arg", "ATLAS_VERSION=${_ATLAS_VERSION}"' in step
    docker = read("build", "Dockerfile.jobs")
    assert "--no-index --no-deps" in docker
    assert "hotato_atlas-${ATLAS_VERSION}-py3-none-any.whl" in docker
    assert "rm -rf autopilot/build/private-wheels" in docker
    assert 'python -c "import atlas.cli, atlas.distill"' in docker
    # No credential is ever handed to the image build.
    args = re.findall(r"^ARG (\w+)", docker, re.M)
    assert args == ["ATLAS_VERSION"]
    for word in ("index-url", "TOKEN", "PASSWORD", "SECRET", "keyring", "pkg.dev"):
        assert word not in docker, word


def test_downloaded_wheel_is_never_committed():
    lines = open(os.path.join(REPO, ".gitignore"), encoding="utf-8").read().splitlines()
    assert "autopilot/build/private-wheels/" in lines


def test_publish_script_never_exposes_the_token():
    script = read("build", "publish-atlas-wheel.sh")
    assert os.access(os.path.join(AUTOPILOT, "build", "publish-atlas-wheel.sh"), os.X_OK)
    assert "git -C \"$SRC\" archive" in script  # built from the last commit only
    assert "--wheel" in script and "sdist" not in script
    assert "TWINE_USERNAME=oauth2accesstoken" in script
    assert "unset TWINE_PASSWORD" in script
    # The token goes to twine through its environment only.
    for bad in ("--password", 'echo "$TWINE_PASSWORD', "echo $TWINE_PASSWORD", "set -x"):
        assert bad not in script, bad
    assert 'versions are never replaced' in script


# --- the job's view of the package
FAKE = {
    "atlas/__init__.py": "",
    "atlas/distill.py": """
        class EngineUnavailable(RuntimeError):
            pass
        ENGINES = {"claude": object}
    """,
    "atlas/cli.py": """
        CALLS = []
        def main(argv):
            CALLS.append(list(argv))
            return 0
    """,
}


@pytest.fixture
def fake_atlas(tmp_path, monkeypatch):
    for rel, body in FAKE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    saved = {k: v for k, v in sys.modules.items() if k == "atlas" or k.startswith("atlas.")}
    for k in saved:
        del sys.modules[k]
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield
    for k in [k for k in sys.modules if k == "atlas" or k.startswith("atlas.")]:
        del sys.modules[k]
    sys.modules.update(saved)


class Store:
    def __init__(self):
        self.blobs = {}

    def get_bytes(self, key):
        return self.blobs.get(key)

    def put_bytes(self, key, data):
        self.blobs[key] = data


def test_atlas_job_runs_the_installed_cli(fake_atlas, monkeypatch):
    import atlas_job as job
    monkeypatch.setattr(job.models, "resolve", lambda name: "model-x")
    rec = {}
    out = job.work(Store(), rec)
    from atlas import cli, distill
    assert distill.ENGINES["claude"] is job.GeminiEngine
    assert cli.CALLS and cli.CALLS[0][2:7] == ["run", "--engine", "claude", "--agents", "stub"]
    assert "--no-pages" in cli.CALLS[0]  # the image has no site checkout to write pages into
    assert out["exit"] == 0 and rec["model"] == "model-x"


@pytest.mark.skipif(importlib.util.find_spec("atlas") is None
                    or importlib.util.find_spec("atlas.cli") is None,
                    reason="hotato-atlas is not installed (it is installed into the jobs image)")
def test_installed_package_has_the_interface_the_job_uses():
    cli = importlib.import_module("atlas.cli")
    distill = importlib.import_module("atlas.distill")
    assert callable(cli.main)
    assert isinstance(distill.ENGINES, dict) and "claude" in distill.ENGINES
    assert issubclass(distill.EngineUnavailable, Exception)
    import atlas_job as job
    job.install_engine()
    assert distill.ENGINES["claude"] is job.GeminiEngine
