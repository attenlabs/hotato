"""``hotato init --auto``: zero-config onboarding.

One flag on the existing ``init`` group that (1) READ-ONLY inspects the
project's declared dependencies for a known voice-agent framework, (2)
locates any committed call recordings, and (3) hands off to
``scaffold_starter`` for the detected stack, printing a first-baseline next
step. Pinned here: detection reads pyproject.toml / requirements.txt /
package.json without importing project code, the stack it picks is always a
real ``STARTER_STACKS`` entry (so `init --auto` can only produce a kit `init
starter` could have produced by hand), a multi-framework repo resolves
deterministically, a recordings-only repo falls back to the generic kit, and
nothing detected refuses cleanly (exit 2) with the manual path -- writing
nothing. The existing `init starter` path is untouched.

Hermetic: every test runs in its own tmp project directory; nothing network,
nothing global.
"""

import json

import pytest

from hotato import cli, initcmd

STARTER_FILES = {
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


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A few bytes are enough: detection is by extension, never by decoding.
    path.write_bytes(b"RIFF____WAVE")


# --- the registry can only ever name a real starter stack -------------------

def test_registry_only_maps_to_real_starter_stacks():
    """The invariant that makes `--auto` safe: every framework maps to a stack
    `init starter` already ships, so auto-detection can never invent a kit."""
    mapped = {stack for stack, _family in initcmd.FRAMEWORK_REGISTRY.values()}
    assert mapped <= set(initcmd.STARTER_STACKS)
    assert set(initcmd._STACK_PRIORITY) == set(initcmd.STARTER_STACKS)


# --- framework detection (read-only, per source file) -----------------------

def test_detects_framework_from_pyproject(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[project]\nname = "x"\ndependencies = ["vapi>=1.0", "httpx"]\n')
    dets = initcmd.detect_frameworks(str(tmp_path))
    assert dets == [{
        "file": "pyproject.toml", "dependency": "vapi",
        "framework": "vapi", "stack": "vapi",
    }]


def test_detects_framework_from_pyproject_optional_and_poetry(tmp_path):
    # optional-dependencies groups AND a poetry table both count.
    _write(tmp_path / "pyproject.toml",
           '[project]\nname = "x"\n'
           '[project.optional-dependencies]\nvoice = ["retell-sdk"]\n')
    dets = initcmd.detect_frameworks(str(tmp_path))
    assert [d["stack"] for d in dets] == ["retell"]

    poetry = tmp_path / "poetry"
    _write(poetry / "pyproject.toml",
           '[tool.poetry.dependencies]\npython = "^3.10"\n'
           'pipecat-ai = "^0.1"\n')
    dets = initcmd.detect_frameworks(str(poetry))
    assert [d["stack"] for d in dets] == ["pipecat"]


def test_detects_framework_from_requirements_txt(tmp_path):
    _write(tmp_path / "requirements.txt",
           "# my deps\n"
           "retell-sdk==1.2.0\n"
           "requests>=2\n"
           "-e git+https://example.invalid/pkg.git#egg=thing\n")
    dets = initcmd.detect_frameworks(str(tmp_path))
    assert [d["stack"] for d in dets] == ["retell"]
    assert dets[0]["file"] == "requirements.txt"


def test_detects_framework_from_package_json_including_npm_scope(tmp_path):
    _write(tmp_path / "package.json",
           '{"dependencies": {"@livekit/agents": "^1.0", "react": "^18"}}')
    dets = initcmd.detect_frameworks(str(tmp_path))
    assert [d["stack"] for d in dets] == ["livekit"]
    assert dets[0]["dependency"] == "@livekit/agents"


def test_detection_normalizes_pep503_names(tmp_path):
    # `LiveKit_Agents` and `livekit-agents` are the same distribution.
    _write(tmp_path / "requirements.txt", "LiveKit_Agents==0.9\n")
    dets = initcmd.detect_frameworks(str(tmp_path))
    assert [d["stack"] for d in dets] == ["livekit"]


def test_no_framework_when_deps_are_unrelated(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[project]\ndependencies = ["flask", "sqlalchemy"]\n')
    assert initcmd.detect_frameworks(str(tmp_path)) == []


def test_prose_mentioning_a_framework_is_not_a_dependency(tmp_path):
    # A framework name inside a description string must not be mistaken for a
    # declared dependency (only the leading token of a spec is a name).
    _write(tmp_path / "pyproject.toml",
           '[project]\n'
           'description = "a vapi and retell competitor"\n'
           'dependencies = ["httpx"]\n')
    assert initcmd.detect_frameworks(str(tmp_path)) == []


def test_detection_is_read_only(tmp_path):
    # Detection reads and never rewrites the inspected files.
    before = '[project]\ndependencies = ["vapi"]\n'
    p = tmp_path / "pyproject.toml"
    _write(p, before)
    initcmd.detect_frameworks(str(tmp_path))
    assert p.read_text(encoding="utf-8") == before


def test_pyproject_fallback_without_tomllib(tmp_path, monkeypatch):
    # The 3.9/3.10 floor has no tomllib: the text-scan fallback still detects.
    monkeypatch.setattr(initcmd, "_tomllib", None)
    _write(tmp_path / "pyproject.toml",
           '[project]\ndependencies = ["vapi>=1.0", "twilio"]\n')
    stacks = {d["stack"] for d in initcmd.detect_frameworks(str(tmp_path))}
    assert stacks == {"vapi", "twilio"}


# --- recording location ------------------------------------------------------

def test_locates_recordings_in_common_dirs_and_root(tmp_path):
    _wav(tmp_path / "recordings" / "call-001.wav")
    _wav(tmp_path / "recordings" / "nested" / "call-002.wav")
    _wav(tmp_path / "calls" / "c.wav")
    _wav(tmp_path / "top-level.wav")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    rec = initcmd.locate_recordings(str(tmp_path))
    assert rec["total"] == 4
    assert rec["files"] == sorted([
        "calls/c.wav",
        "recordings/call-001.wav",
        "recordings/nested/call-002.wav",
        "top-level.wav",
    ])
    dirs = {d["dir"]: d["count"] for d in rec["dirs"]}
    assert dirs == {"recordings": 2, "calls": 1}


def test_locate_recordings_none(tmp_path):
    rec = initcmd.locate_recordings(str(tmp_path))
    assert rec == {"dirs": [], "files": [], "total": 0}


def test_locate_recordings_forward_slashes(tmp_path):
    _wav(tmp_path / "logs" / "deep" / "x.wav")
    rec = initcmd.locate_recordings(str(tmp_path))
    assert all("\\" not in f for f in rec["files"])
    assert rec["files"] == ["logs/deep/x.wav"]


# --- deterministic stack choice ---------------------------------------------

def test_choose_stack_prefers_vendor_then_priority(tmp_path):
    # vapi + livekit declared: vapi (higher priority) wins, deterministically.
    _write(tmp_path / "pyproject.toml",
           '[project]\ndependencies = ["livekit-agents", "vapi"]\n')
    dets = initcmd.detect_frameworks(str(tmp_path))
    assert initcmd.choose_stack(dets) == ("vapi", "vapi")


def test_choose_stack_generic_loses_to_a_vendor(tmp_path):
    # elevenlabs -> generic, twilio -> twilio: the tuned vendor wins.
    _write(tmp_path / "requirements.txt", "elevenlabs\ntwilio\n")
    dets = initcmd.detect_frameworks(str(tmp_path))
    assert initcmd.choose_stack(dets)[0] == "twilio"


def test_choose_stack_none_for_empty():
    assert initcmd.choose_stack([]) is None


# --- scaffold_auto: the full onboarding --------------------------------------

def test_scaffold_auto_generates_tuned_starter_kit(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[project]\ndependencies = ["vapi"]\n')
    _wav(tmp_path / "recordings" / "first.wav")

    result = initcmd.scaffold_auto(str(tmp_path), str(tmp_path))
    assert result["kind"] == "init-auto"
    assert result["stack"] == "vapi"
    assert result["detected"]["framework"] == "vapi"
    assert result["auto_pull"] is True
    assert result["credential_env"] == ["VAPI_API_KEY"]
    assert set(result["files"]) == STARTER_FILES

    # The kit really landed on disk, tuned for the detected stack.
    found = {
        str(p.relative_to(tmp_path)).replace("\\", "/")
        for p in tmp_path.rglob("*") if p.is_file()
    }
    assert STARTER_FILES <= found
    cfg = (tmp_path / "hotato.yaml").read_text(encoding="utf-8")
    assert "stack: vapi" in cfg

    # The baseline next step points at the recording it found.
    assert result["next"][0] == "hotato investigate recordings/first.wav"
    # The CI-gate step carries the real verify command, GUARDED so it is a
    # clean no-op on the freshly scaffolded (empty) contracts/ instead of the
    # exit-2 "no contracts" usage error a bare command would hit (M3). out != "."
    # here, so it is run from out via a cd prefix.
    gate = result["next"][-1]
    assert "hotato contract verify contracts --junit hotato.xml" in gate
    assert gate.startswith("cd ")
    assert "if ls contracts/*.hotato" in gate


def test_scaffold_auto_matches_a_hand_run_starter(tmp_path):
    # `--auto` for vapi produces exactly the file set `init starter --stack
    # vapi` produces: same tuned kit, chosen for you.
    auto_dir = tmp_path / "auto"
    auto_dir.mkdir()
    _write(auto_dir / "pyproject.toml", '[project]\ndependencies = ["vapi"]\n')
    auto = initcmd.scaffold_auto(str(auto_dir), str(auto_dir))

    manual = initcmd.scaffold_starter("vapi", str(tmp_path / "manual"))
    assert set(auto["files"]) == set(manual["files"])
    assert auto["auto_pull"] == manual["auto_pull"]
    assert auto["credential_env"] == manual["credential_env"]


def test_scaffold_auto_recordings_only_falls_back_to_generic(tmp_path):
    _wav(tmp_path / "calls" / "a.wav")
    result = initcmd.scaffold_auto(str(tmp_path), str(tmp_path))
    assert result["stack"] == "generic"
    assert result["detected"]["framework"] is None
    assert result["recordings"]["total"] == 1
    assert result["next"][0] == "hotato investigate calls/a.wav"
    cfg = (tmp_path / "hotato.yaml").read_text(encoding="utf-8")
    assert "stack: generic" in cfg


def test_scaffold_auto_no_recording_uses_demo_baseline(tmp_path):
    _write(tmp_path / "requirements.txt", "vapi\n")
    result = initcmd.scaffold_auto(str(tmp_path), str(tmp_path))
    assert result["next"][0] == "hotato start --demo"


def test_scaffold_auto_gate_next_command_runs_clean_on_fresh_scaffold(tmp_path):
    """M3: the printed CI-gate next-command must run exit 0 on the scaffold it
    just wrote. A bare ``hotato contract verify contracts`` on the freshly
    scaffolded (empty) contracts/ is a usage error (exit 2) -- proven here so
    the guard is not vacuous -- yet the guarded command hotato prints is a
    clean no-op (exit 0), matching the CI job the same scaffold generates."""
    import subprocess

    from hotato import contract as _contract

    _write(tmp_path / "pyproject.toml", '[project]\ndependencies = ["vapi"]\n')
    _wav(tmp_path / "recordings" / "first.wav")
    result = initcmd.scaffold_auto(str(tmp_path), str(tmp_path))

    # The empty scaffolded contracts/ really is an unguarded usage error.
    contracts_dir = tmp_path / "contracts"
    assert contracts_dir.is_dir()
    with pytest.raises(ValueError):
        _contract.verify_contracts(str(contracts_dir))

    # The PRINTED gate command guards it, so running it verbatim on the fresh
    # scaffold is a clean no-op: exit 0, never the exit-2 error. The empty
    # branch never invokes the `hotato` binary, so this stays hermetic.
    gate = result["next"][-1]
    proc = subprocess.run(
        gate, shell=True, cwd=str(tmp_path),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (gate, proc.stdout, proc.stderr)
    assert "no contracts yet" in proc.stdout


# --- clean refusal -----------------------------------------------------------

def test_scaffold_auto_refuses_when_nothing_detected(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[project]\ndependencies = ["flask"]\n')
    with pytest.raises(initcmd.InitError) as excinfo:
        initcmd.scaffold_auto(str(tmp_path), str(tmp_path))
    msg = str(excinfo.value)
    assert "could not auto-detect" in msg
    # The refusal names the manual path.
    assert "hotato init starter --stack generic" in msg
    # Nothing was written.
    found = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert found == {"pyproject.toml"}


def test_scaffold_auto_missing_root_is_init_error(tmp_path):
    with pytest.raises(initcmd.InitError):
        initcmd.scaffold_auto(str(tmp_path / "does-not-exist"), ".")


# --- idempotency / force -----------------------------------------------------

def test_scaffold_auto_refuses_existing_files_without_force(tmp_path):
    _write(tmp_path / "pyproject.toml", '[project]\ndependencies = ["vapi"]\n')
    assert initcmd.scaffold_auto(str(tmp_path), str(tmp_path))["stack"] == "vapi"
    with pytest.raises(initcmd.InitError):
        initcmd.scaffold_auto(str(tmp_path), str(tmp_path))
    # --force overwrites.
    again = initcmd.scaffold_auto(str(tmp_path), str(tmp_path), force=True)
    assert again["stack"] == "vapi"


# --- CLI wiring --------------------------------------------------------------

def test_cli_auto_text(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "pyproject.toml", '[project]\ndependencies = ["twilio"]\n')
    assert cli.main(["init", "--auto"]) == 0
    out = capsys.readouterr().out
    assert "detected twilio" in out
    assert "hotato-contracts.yml" in out
    assert (tmp_path / "hotato.yaml").is_file()


def test_cli_auto_json_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "requirements.txt", "pipecat-ai\n")
    _wav(tmp_path / "recordings" / "r.wav")
    assert cli.main(["init", "--auto", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "hotato"
    assert out["kind"] == "init-auto"
    assert out["stack"] == "pipecat"
    assert out["detected"]["framework"] == "pipecat"
    assert out["recordings"]["total"] == 1
    assert set(out["files"]) == STARTER_FILES


def test_cli_auto_refusal_is_exit_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "pyproject.toml", '[project]\ndependencies = ["flask"]\n')
    assert cli.main(["init", "--auto"]) == 2


def test_cli_auto_refusal_json_is_structured_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--auto", "--format", "json"]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False
    assert err["exit_code"] == 2
    assert "could not auto-detect" in err["message"]


def test_cli_init_bare_still_needs_a_mode(tmp_path, monkeypatch):
    # `hotato init` with no subcommand and no mode is still a usage error.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 2


def test_existing_init_starter_still_works(tmp_path):
    # The additive `--auto` mode must not disturb `init starter`.
    assert cli.main(["init", "starter", "--stack", "generic",
                     "--out", str(tmp_path)]) == 0
    assert (tmp_path / "hotato.yaml").is_file()
