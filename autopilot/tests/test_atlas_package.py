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
    # The one identity with a write role on the private repository is atlas-wheel, which tests a
    # new collector version before uploading it (jobs/atlas_wheel.py). Versions are never replaced.
    for m in re.finditer(r'resource "google_artifact_registry_repository_iam_\w+" "\w+" \{\n(.*?)\n\}\n', iam, re.S):
        if "private_py" in m.group(1):
            assert "artifactregistry.reader" in m.group(1) or (
                "artifactregistry.writer" in m.group(1) and 'job["atlas-wheel"]' in m.group(1))
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


def test_every_wait_for_names_an_earlier_step():
    text = read("build", "cloudbuild.yaml")
    seen = []
    for m in re.finditer(r"\n  - id: ([\w-]+)\n(.*?)(?=\n  - id: |\Z)", text, re.S):
        for dep in wait_for(m.group(2)):
            assert dep == "-" or dep in seen, f"{m.group(1)} waits for {dep}, which is not an earlier step"
        seen.append(m.group(1))
    assert seen.index("image-monitor") < seen.index("atlas-wheel") < seen.index("image-jobs")


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


def test_build_upload_keeps_the_gitignore():
    """`gcloud builds submit` uploads by .gcloudignore. Listing .gitignore there kept the rules
    file out of the build, and the test above then failed in Cloud Build only."""
    lines = [ln.strip() for ln in open(os.path.join(REPO, ".gcloudignore"), encoding="utf-8")]
    assert ".gitignore" not in lines and "/.gitignore" not in lines
    assert "#!include:.gitignore" in lines and ".git" in lines


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
        import json, os
        CALLS = []
        def main(argv):
            CALLS.append(list(argv))
            if "publish" in argv:  # the shrink guard's feed render
                out = argv[argv.index("--out") + 1]
                os.makedirs(out, exist_ok=True)
                with open(os.path.join(out, "feed.json"), "w") as fh:
                    json.dump({"entries": [{"id": "p1"}], "counts": {"reports": 1}}, fh)
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


def test_store_only_modes_run_the_installed_cli_under_the_lock(fake_atlas, monkeypatch, tmp_path):
    import sqlite3
    import common
    import atlas_job as job
    import atlas_state as state
    monkeypatch.setattr(job, "prepare", lambda *a, **k: None)
    monkeypatch.setattr(job.site, "fetch_url", lambda url: None)  # offline: no live feed
    st = common.Store(local_dir=str(tmp_path / "bucket"))
    db = tmp_path / "seed.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE reports(id); CREATE TABLE patterns(id); CREATE TABLE mentions(id);"
                       "CREATE TABLE checks(id); INSERT INTO patterns VALUES ('p1');")
    conn.commit()
    conn.close()
    g = state.Objects(st).write_file(state.DB_KEY, str(db), 0)
    rec = {}
    assert job.work_for("recheck")(st, rec) == "ok"
    from atlas import cli
    ran = [c for c in cli.CALLS if c[-1] == "recheck"]
    assert len(ran) == 1 and ran[0][0] == "--db"
    assert cli.CALLS[-1][-2:] == ["--out", cli.CALLS[-1][-1]] or "--no-pages" in cli.CALLS[-1]
    assert rec["baseline"] == "previous store"  # the shrink guard ran before the save
    assert state.Objects(st).generation(state.DB_KEY) > g  # saved over the generation it read
    assert state.Objects(st).read(state.LOCK_KEY) == (None, 0)  # and released the lock
    assert job.JOBS == {"run": "atlas", "recheck": "atlas-recheck", "audit": "atlas-audit", "cli": "atlas-cli"}
    with pytest.raises(SystemExit):
        job.work_for("cli")


@pytest.mark.skipif(importlib.util.find_spec("atlas") is None
                    or importlib.util.find_spec("atlas.cli") is None,
                    reason="hotato-atlas is not installed (it is installed into the jobs image)")
def test_installed_package_has_the_interface_the_job_uses(tmp_path, monkeypatch):
    cli = importlib.import_module("atlas.cli")
    distill = importlib.import_module("atlas.distill")
    assert callable(cli.main)
    assert isinstance(distill.ENGINES, dict) and "claude" in distill.ENGINES
    assert issubclass(distill.EngineUnavailable, Exception)
    import atlas_job as job
    job.install_engine()
    assert distill.ENGINES["claude"] is job.GeminiEngine
    # The real CLI accepts the job's flags (offline, stub engines, so nothing leaves the machine).
    monkeypatch.chdir(tmp_path)
    argv = ["--offline", "--db", str(tmp_path / "a.db"), "--cache", str(tmp_path / "cache"),
            "run", "--engine", "stub", "--agents", "stub", "--limit", "1", "--no-pages",
            "--out", str(tmp_path / "site")]
    assert cli.main(argv) == 0
