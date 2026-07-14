"""`hotato init starter --stack STACK --out DIR`: the whole-repo starter kit
(CI gate, hotato.yaml, fixtures/, contracts/, reports/).

Pinned here: the eleven files land, exactly once (idempotent, refused
without --force), hotato.yaml and the generated GitHub Actions workflow are
both valid YAML, the CI workflow never hard-fails on an empty contracts/ or
fixtures/ directory (a fresh repo's normal starting state), the per-stack
split (auto-pull vs capture-in-your-infra) matches the REAL adapter set in
capture.py/initcmd.py rather than an invented one, and every command the
generated docs tell a human to run is a real, currently-registered hotato
CLI flag (cross-checked against `hotato describe`'s own manifest, so the
starter's docs cannot silently drift from the CLI)."""

import json
import os
import re

import pytest

from hotato import cli, initcmd


def _scaffold(tmp_path, stack="vapi", *extra):
    return cli.main([
        "init", "starter", "--stack", stack, "--out", str(tmp_path), *extra,
    ])


EXPECTED_FILES = {
    "hotato.yaml",
    "HOTATO.md",
    ".gitignore",
    ".github/workflows/hotato-contracts.yml",
    "fixtures/README.md",
    "fixtures/scenarios/.gitkeep",
    "fixtures/audio/.gitkeep",
    "contracts/README.md",
    "contracts/.gitkeep",
    "reports/README.md",
    "reports/.gitkeep",
}


# --- no fabricated stack claims ---------------------------------------------

def test_starter_stacks_match_the_real_adapter_set():
    """Every stack init starter offers is a REAL shipped connector (see
    docs/ADAPTER-STATUS.md): capture.STACKS is the ground truth for what
    hotato can actually pull or capture. This is not a coincidence -- it is
    asserted in initcmd.py itself and re-checked here so a future edit to
    either set trips a test instead of silently drifting into a fabricated
    claim."""
    from hotato import capture

    assert set(initcmd.STARTER_STACKS) == set(capture.STACKS)
    assert set(initcmd._STARTER_AUTO_PULL) <= set(capture.DUAL_PULL_STACKS)
    for stack in initcmd._STARTER_CAPTURE_ONLY:
        assert stack not in capture.DUAL_PULL_STACKS


# --- scaffolding -------------------------------------------------------------

@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_scaffolds_all_eleven_files(tmp_path, stack):
    out = tmp_path / stack
    assert _scaffold(out, stack) == 0
    found = {
        str(p.relative_to(out)).replace("\\", "/")
        for p in out.rglob("*") if p.is_file()
    }
    assert found == EXPECTED_FILES


@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_generated_files_are_nonempty(tmp_path, stack):
    out = tmp_path / stack
    assert _scaffold(out, stack) == 0
    for rel in EXPECTED_FILES:
        text = (out / rel).read_text(encoding="utf-8")
        assert text.strip(), f"{rel} is empty"


def test_out_dot_works(tmp_path, monkeypatch):
    # The documented common case: `hotato init starter --stack vapi --out .`
    # run from inside an existing repo.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "starter", "--stack", "vapi", "--out", "."]) == 0
    assert (tmp_path / "hotato.yaml").is_file()
    assert (tmp_path / "HOTATO.md").is_file()


# --- idempotency ---------------------------------------------------------

def test_overwrite_needs_force(tmp_path):
    assert _scaffold(tmp_path, "vapi") == 0
    assert _scaffold(tmp_path, "vapi") == 2
    assert _scaffold(tmp_path, "vapi", "--force") == 0


def test_no_partial_write_on_refusal(tmp_path):
    # A pre-existing single file (e.g. the repo's own hotato.yaml from a
    # prior run) is enough to refuse the WHOLE scaffold, never a partial one.
    (tmp_path / "hotato.yaml").write_text("# pre-existing\n", encoding="utf-8")
    assert _scaffold(tmp_path, "vapi") == 2
    # Nothing else was written.
    for rel in EXPECTED_FILES - {"hotato.yaml"}:
        assert not (tmp_path / rel).exists()
    # The pre-existing file was left untouched.
    assert (tmp_path / "hotato.yaml").read_text(encoding="utf-8") == "# pre-existing\n"


def test_unknown_stack_is_exit_2(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["init", "starter", "--stack", "nope", "--out", str(tmp_path / "w")])
    assert excinfo.value.code == 2
    with pytest.raises(initcmd.InitError):
        initcmd.scaffold_starter("nope", str(tmp_path / "w2"))
    assert not (tmp_path / "w2").exists()


def test_json_output_shape(tmp_path, capsys):
    assert _scaffold(tmp_path, "vapi", "--format", "json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "init-starter"
    assert out["stack"] == "vapi"
    assert set(out["files"]) == EXPECTED_FILES
    assert out["auto_pull"] is True
    assert out["credential_env"] == ["VAPI_API_KEY"]


@pytest.mark.parametrize("stack,auto_pull,env", [
    ("vapi", True, ["VAPI_API_KEY"]),
    ("retell", True, ["RETELL_API_KEY"]),
    ("twilio", True, ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"]),
    ("livekit", False, []),
    ("pipecat", False, []),
])
def test_json_output_shape_per_stack(tmp_path, capsys, stack, auto_pull, env):
    assert _scaffold(tmp_path / stack, stack, "--format", "json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["auto_pull"] is auto_pull
    assert out["credential_env"] == env


# --- hotato.yaml -------------------------------------------------------------

@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_hotato_yaml_is_valid_yaml(tmp_path, stack):
    yaml = pytest.importorskip("yaml")
    assert _scaffold(tmp_path, stack) == 0
    cfg = yaml.safe_load((tmp_path / "hotato.yaml").read_text(encoding="utf-8"))
    assert cfg["version"] == 1
    assert cfg["stack"] == stack
    assert cfg["fixtures"] == {
        "scenarios_dir": "fixtures/scenarios", "audio_dir": "fixtures/audio",
    }
    assert cfg["contracts"] == {"dir": "contracts"}
    assert cfg["reports"]["dir"] == "reports"
    assert set(cfg["reports"]["formats"]) == {"json", "html"}
    assert cfg["ci"]["junit"] == "hotato.xml"
    envs = list(initcmd._STARTER_ENV_VARS[stack])
    assert cfg["credentials"]["env"] == envs
    if stack in initcmd._STARTER_AUTO_PULL:
        assert cfg["recording"]["access"] == "auto-pull"
    else:
        assert cfg["recording"]["access"] == "capture-in-your-infra"
        assert cfg["credentials"]["env"] == []


# --- the CI workflow ---------------------------------------------------------

@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_workflow_is_valid_yaml(tmp_path, stack):
    yaml = pytest.importorskip("yaml")
    assert _scaffold(tmp_path, stack) == 0
    text = (tmp_path / ".github" / "workflows" / "hotato-contracts.yml").read_text(
        encoding="utf-8")
    doc = yaml.safe_load(text)
    # YAML 1.1 parses a bare `on:` key as the boolean True (a well-known
    # GitHub Actions quirk this repo's OWN shipped workflow also lives with
    # unquoted -- see .github/workflows/hotato.yml); assert on the parsed
    # key rather than assuming a string "on" survives the round trip.
    assert True in doc or "on" in doc
    jobs = doc["jobs"]
    assert "verify" in jobs
    steps = jobs["verify"]["steps"]
    run_steps = "\n".join(s.get("run", "") for s in steps if "run" in s)
    assert "hotato contract verify contracts --junit hotato.xml" in run_steps
    assert "hotato run --scenarios fixtures/scenarios --audio fixtures/audio" in run_steps
    # Never hard-fails on a fresh repo with no contracts/fixtures yet.
    assert "compgen -G" in run_steps or "if [ -d" in run_steps


@pytest.mark.parametrize("stack", initcmd._STARTER_AUTO_PULL)
def test_workflow_has_disabled_weekly_sweep_for_auto_pull_stacks(tmp_path, stack):
    yaml = pytest.importorskip("yaml")
    assert _scaffold(tmp_path, stack) == 0
    text = (tmp_path / ".github" / "workflows" / "hotato-contracts.yml").read_text(
        encoding="utf-8")
    doc = yaml.safe_load(text)
    jobs = doc["jobs"]
    assert "weekly-sweep" in jobs
    sweep = jobs["weekly-sweep"]
    assert sweep["if"] is False  # disabled by default; a human opts in
    steps = sweep["steps"]
    run_steps = " ".join(s.get("run", "") for s in steps if "run" in s)
    assert f"hotato sweep --stack {stack}" in run_steps
    for env_name in initcmd._STARTER_ENV_VARS[stack]:
        assert env_name in text


@pytest.mark.parametrize("stack", initcmd._STARTER_CAPTURE_ONLY)
def test_workflow_has_no_sweep_job_for_capture_only_stacks(tmp_path, stack):
    yaml = pytest.importorskip("yaml")
    assert _scaffold(tmp_path, stack) == 0
    text = (tmp_path / ".github" / "workflows" / "hotato-contracts.yml").read_text(
        encoding="utf-8")
    doc = yaml.safe_load(text)
    assert "weekly-sweep" not in doc["jobs"]
    assert "hotato sweep" not in text
    assert f"hotato setup --stack {stack}" in text


# --- .gitignore --------------------------------------------------------------

@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_gitignore_keeps_pinned_audio_committed(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    lines = set(text.splitlines())
    assert "*.wav" in lines
    assert "!fixtures/audio/*.wav" in lines
    assert "!contracts/**/audio/*.wav" in lines
    assert "hotato.xml" in lines


# --- README stubs reference REAL, currently-registered CLI flags -----------

def _describe_manifest():
    out = cli.main
    import contextlib
    import io

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


@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_contracts_readme_flags_are_real(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    text = (tmp_path / "contracts" / "README.md").read_text(encoding="utf-8")
    flags_used = set(re.findall(r"--[a-z][a-z-]*", text))
    manifest = _describe_manifest()
    create = _find_subcommand(manifest, "contract create")
    verify = _find_subcommand(manifest, "contract verify")
    assert create is not None and verify is not None
    real_flags = _flag_names(create) | _flag_names(verify)
    assert flags_used <= real_flags, flags_used - real_flags


@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_fixtures_readme_flags_are_real(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    text = (tmp_path / "fixtures" / "README.md").read_text(encoding="utf-8")
    flags_used = set(re.findall(r"--[a-z][a-z-]*", text))
    manifest = _describe_manifest()
    create = _find_subcommand(manifest, "fixture create")
    promote = _find_subcommand(manifest, "fixture promote")
    run = _find_subcommand(manifest, "run")
    assert create is not None and promote is not None and run is not None
    real_flags = _flag_names(create) | _flag_names(promote) | _flag_names(run)
    assert flags_used <= real_flags, flags_used - real_flags


@pytest.mark.parametrize("stack", initcmd.STARTER_STACKS)
def test_hotato_md_flags_are_real(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    text = (tmp_path / "HOTATO.md").read_text(encoding="utf-8")
    flags_used = set(re.findall(r"--[a-z][a-z-]*", text))
    manifest = _describe_manifest()
    names = (
        ["connect", "sweep", "contract create", "setup", "verify", "inspect"]
    )
    real_flags = set()
    for name in names:
        cmd = _find_subcommand(manifest, name)
        assert cmd is not None, name
        real_flags |= _flag_names(cmd)
    assert flags_used <= real_flags, flags_used - real_flags


# --- render_text -------------------------------------------------------------

def test_render_text_mentions_ci_gate_and_next_steps(tmp_path):
    result = initcmd.scaffold_starter("vapi", str(tmp_path))
    text = initcmd.render_starter_text(result)
    assert "hotato-contracts.yml" in text
    assert "hotato connect vapi" in text


def test_render_text_capture_only_next_steps(tmp_path):
    result = initcmd.scaffold_starter("pipecat", str(tmp_path))
    text = initcmd.render_starter_text(result)
    assert "hotato setup --stack pipecat" in text
    assert "hotato connect" not in text


# --- public locators are '/'-separated on every platform ---------------------

def test_public_locator_is_normalized_to_forward_slashes():
    # The public machine-JSON 'files' locators must never carry native
    # (Windows) backslash separators. The helper normalizes only the public
    # locator; native paths are still used for I/O.
    assert initcmd._as_posix(r".github\workflows\deploy.yml") == \
        ".github/workflows/deploy.yml"
    assert initcmd._as_posix("fixtures/scenarios/.gitkeep") == \
        "fixtures/scenarios/.gitkeep"


def test_starter_files_use_forward_slashes_on_a_windows_style_checkout(
        tmp_path, monkeypatch):
    # Simulate os.path.relpath returning native backslash separators (its
    # Windows behavior): the public 'files' list must still be '/'-separated.
    real_relpath = os.path.relpath

    def windows_relpath(path, start=None):
        return real_relpath(path, start).replace("/", "\\")

    monkeypatch.setattr(initcmd.os.path, "relpath", windows_relpath)
    result = initcmd.scaffold_starter("vapi", str(tmp_path))
    assert all("\\" not in f for f in result["files"]), result["files"]
    assert ".github/workflows/hotato-contracts.yml" in result["files"]


def test_webhook_files_use_forward_slashes_on_a_windows_style_checkout(
        tmp_path, monkeypatch):
    real_relpath = os.path.relpath

    def windows_relpath(path, start=None):
        return real_relpath(path, start).replace("/", "\\")

    monkeypatch.setattr(initcmd.os.path, "relpath", windows_relpath)
    result = initcmd.scaffold_webhook("vapi", "fastapi", str(tmp_path))
    assert all("\\" not in f for f in result["files"]), result["files"]
    assert ".github/workflows/deploy.yml" in result["files"]
    assert "tests/test_webhook_contract.py" in result["files"]
