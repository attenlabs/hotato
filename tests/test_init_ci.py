"""`hotato init ci --system SYSTEM [--out DIR]`: the one canonical CI config
per system (GitLab CI, Jenkins, Azure Pipelines, CircleCI).

Pinned here: each system writes exactly its one well-known file, the content
pins the CURRENT package version and both gate commands (`hotato contract
verify` + `hotato run`, the same two the starter workflow pins), the YAML
configs parse with yaml.safe_load, the gates are guarded so an empty
contracts/ or fixtures/ directory is a normal starting state (never a red
pipeline), overwrite is refused without --force, --out is honored, output is
deterministic, and every hotato flag a generated config uses is a real,
currently-registered CLI flag (cross-checked against `hotato describe`'s own
manifest, so the configs cannot silently drift from the CLI)."""

import contextlib
import io
import json
import re

import pytest

from hotato import __version__, cli, initcmd


def _scaffold(out_dir, system="gitlab", *extra):
    return cli.main([
        "init", "ci", "--system", system, "--out", str(out_dir), *extra,
    ])


EXPECTED_FILES = {
    "gitlab": ".gitlab-ci.yml",
    "jenkins": "Jenkinsfile",
    "azure": "azure-pipelines.yml",
    "circleci": ".circleci/config.yml",
}

_CONTRACT_GATE = "hotato contract verify contracts --junit hotato.xml"
_RUN_GATE = "hotato run --scenarios fixtures/scenarios --audio fixtures/audio"


def _generated_text(tmp_path, system):
    assert _scaffold(tmp_path, system) == 0
    rel = EXPECTED_FILES[system]
    path = tmp_path
    for part in rel.split("/"):
        path = path / part
    return path.read_text(encoding="utf-8")


# --- the file set -----------------------------------------------------------

def test_ci_systems_cover_the_four_documented_systems():
    assert set(initcmd.CI_SYSTEMS) == set(EXPECTED_FILES)


@pytest.mark.parametrize("system", initcmd.CI_SYSTEMS)
def test_generates_exactly_the_one_canonical_file(tmp_path, system):
    out = tmp_path / system
    assert _scaffold(out, system) == 0
    found = {
        str(p.relative_to(out)).replace("\\", "/")
        for p in out.rglob("*") if p.is_file()
    }
    assert found == {EXPECTED_FILES[system]}


# --- content ----------------------------------------------------------------

@pytest.mark.parametrize("system", initcmd.CI_SYSTEMS)
def test_content_pins_the_current_version_and_both_gates(tmp_path, system):
    text = _generated_text(tmp_path, system)
    assert f"hotato=={__version__}" in text
    assert _CONTRACT_GATE in text
    assert _RUN_GATE in text
    # The comment header says what it does and points at the docs.
    first = text.splitlines()[0]
    assert first.startswith(("#", "//"))
    assert "hotato turn-taking gate" in first
    assert "docs/CI.md" in text


@pytest.mark.parametrize("system", initcmd.CI_SYSTEMS)
def test_gates_never_hard_fail_on_a_fresh_repo(tmp_path, system):
    # Both gate commands sit behind POSIX-shell guards: no contracts/ or
    # fixtures/ yet means an echo, exit 0, never a red pipeline.
    text = _generated_text(tmp_path, system)
    assert "if ls contracts/*.hotato > /dev/null 2>&1; then" in text
    assert "if [ -d fixtures/scenarios ]" in text


@pytest.mark.parametrize("system", initcmd.CI_SYSTEMS)
def test_generation_is_deterministic(tmp_path, system):
    first = _generated_text(tmp_path / "a", system)
    second = _generated_text(tmp_path / "b", system)
    assert first == second


# --- the YAML configs parse, with each system's load-bearing shape ----------

def test_gitlab_config_shape(tmp_path):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_generated_text(tmp_path, "gitlab"))
    job = doc["hotato"]
    assert job["image"] == "python:3.12"
    assert f"pip install hotato=={__version__}" in job["script"][0]
    assert job["artifacts"]["when"] == "always"
    assert job["artifacts"]["reports"]["junit"] == "hotato.xml"
    assert set(job["artifacts"]["paths"]) == {
        "contracts-verify.json", "fixtures-run.json", "hotato.xml",
    }


def test_azure_config_shape(tmp_path):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_generated_text(tmp_path, "azure"))
    assert doc["pool"]["vmImage"] == "ubuntu-latest"
    tasks = [s.get("task") for s in doc["steps"] if "task" in s]
    assert tasks == ["UsePythonVersion@0", "PublishTestResults@2",
                     "CopyFiles@2", "PublishBuildArtifacts@1"]
    publish = [s for s in doc["steps"]
               if s.get("task") in ("PublishTestResults@2", "CopyFiles@2",
                                    "PublishBuildArtifacts@1")]
    assert all(s["condition"] == "always()" for s in publish)
    scripts = "\n".join(s["script"] for s in doc["steps"] if "script" in s)
    assert _CONTRACT_GATE in scripts
    assert _RUN_GATE in scripts


def test_circleci_config_shape(tmp_path):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_generated_text(tmp_path, "circleci"))
    assert doc["version"] == 2.1
    job = doc["jobs"]["hotato"]
    assert job["docker"] == [{"image": "cimg/python:3.12"}]
    steps = job["steps"]
    assert steps[0] == "checkout"
    step_names = [s["run"]["name"] for s in steps
                  if isinstance(s, dict) and "run" in s]
    assert step_names == ["Install hotato", "Verify contracts",
                          "Run fixtures", "Collect reports"]
    assert {"store_test_results": {"path": "hotato-ci-reports"}} in steps
    assert {"store_artifacts": {"path": "hotato-ci-reports"}} in steps
    assert doc["workflows"]["hotato"]["jobs"] == ["hotato"]


def test_jenkinsfile_is_a_declarative_pipeline(tmp_path):
    text = _generated_text(tmp_path, "jenkins")
    assert "pipeline {" in text
    assert "agent { docker { image 'python:3.12' } }" in text
    # Publishes JUnit + the JSON reports whatever the stage outcome, and
    # tolerates the fresh-repo case where neither exists yet.
    assert "post {" in text and "always {" in text
    assert "junit allowEmptyResults: true, testResults: 'hotato.xml'" in text
    assert "allowEmptyArchive: true" in text
    # The venv-scoped binary keeps every stage on the one pinned install.
    assert f".hotato-venv/bin/pip install hotato=={__version__}" in text
    assert ".hotato-venv/bin/hotato contract verify contracts" in text
    # Balanced Groovy braces (a cheap structural parse for a non-YAML file).
    assert text.count("{") == text.count("}")


# --- idempotency -------------------------------------------------------------

@pytest.mark.parametrize("system", initcmd.CI_SYSTEMS)
def test_overwrite_needs_force(tmp_path, system):
    assert _scaffold(tmp_path, system) == 0
    assert _scaffold(tmp_path, system) == 2
    assert _scaffold(tmp_path, system, "--force") == 0


def test_refusal_leaves_the_existing_file_untouched(tmp_path):
    (tmp_path / ".gitlab-ci.yml").write_text("# mine\n", encoding="utf-8")
    assert _scaffold(tmp_path, "gitlab") == 2
    assert (tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8") == "# mine\n"


def test_unknown_system_is_exit_2(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["init", "ci", "--system", "nope", "--out", str(tmp_path / "w")])
    assert excinfo.value.code == 2
    with pytest.raises(initcmd.InitError):
        initcmd.scaffold_ci("nope", str(tmp_path / "w2"))
    assert not (tmp_path / "w2").exists()


# --- --out -------------------------------------------------------------------

def test_out_defaults_to_the_current_directory(tmp_path, monkeypatch):
    # The documented common case: `hotato init ci --system gitlab` run from
    # the repo root; each system reads its config from exactly that root.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "ci", "--system", "gitlab"]) == 0
    assert (tmp_path / ".gitlab-ci.yml").is_file()


def test_out_honored_and_created(tmp_path):
    out = tmp_path / "deep" / "repo"
    assert _scaffold(out, "circleci") == 0
    assert (out / ".circleci" / "config.yml").is_file()


# --- machine JSON -------------------------------------------------------------

def test_json_output_shape(tmp_path, capsys):
    assert _scaffold(tmp_path, "circleci", "--format", "json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "init-ci"
    assert out["system"] == "circleci"
    assert out["files"] == [".circleci/config.yml"]  # '/'-separated, always
    assert out["pinned_version"] == __version__
    assert any("hotato contract verify contracts" in c for c in out["next"])


# --- render_text ---------------------------------------------------------------

def test_render_text_names_the_gate_and_the_docs(tmp_path):
    result = initcmd.scaffold_ci("gitlab", str(tmp_path))
    text = initcmd.render_ci_text(result)
    assert ".gitlab-ci.yml" in text
    assert f"hotato=={__version__}" in text
    assert "docs/CI.md" in text


# --- every hotato flag in a generated config is a real CLI flag --------------

def _describe_manifest():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(["describe", "--format", "json"])
    return json.loads(buf.getvalue())


def _find_subcommand(manifest, dotted_name):
    for c in manifest["subcommands"]:
        if c["name"] == dotted_name:
            return c
        found = _find_subcommand({"subcommands": c.get("subcommands", [])}, dotted_name)
        if found:
            return found
    return None


def _flag_names(cmd) -> set:
    return {a["name"] for a in cmd["args"]}


@pytest.mark.parametrize("system", initcmd.CI_SYSTEMS)
def test_generated_config_flags_are_real(tmp_path, system):
    text = _generated_text(tmp_path, system)
    flags_used = set(re.findall(r"--[a-z][a-z-]*", text))
    manifest = _describe_manifest()
    real_flags = set()
    for name in ("contract verify", "run", "init ci"):
        cmd = _find_subcommand(manifest, name)
        assert cmd is not None, name
        real_flags |= _flag_names(cmd)
    assert flags_used <= real_flags, flags_used - real_flags
