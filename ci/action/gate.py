"""Gate step for the root GitHub Action (stdlib only, no network of its own).

Reads the Action inputs from ``HOTATO_ACTION_*`` environment variables (the
composite step maps ``inputs.*`` into env, so no input string is ever shell
evaluated), validates them, installs the pinned hotato, runs exactly one
hotato command with argv-safe construction, renders the five-lane job summary
from the machine JSON (pass AND fail), writes the step outputs, and exits with
hotato's own gate code.

Install modes (``hotato-version`` input):

* ``action`` (default): run the Action checkout itself
  (``github.action_path``) directly off ``PYTHONPATH`` -- no pip, no build
  backend, no package index (zero-egress); the executed code is exactly the pinned
  Action revision; zero package-index egress.
* ``preinstalled``: skip installation; hotato must already be importable
  (used by hotato's own CI and the local harness test).
* an exact version such as ``1.3.3``: ``pip install --no-deps hotato==X``.

Exit contract: the step exits with the hotato process exit code (0 pass,
1 fail, 2 refuse/usage per the CLI's own contract). An input-validation or
environment failure that prevents the run exits 2, with an ERROR summary.
The advisory (model-judged) lane never changes this exit unless the consumer
opted in with ``gate-advisory: true`` (hotato's own ``--gate-judge``).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import summary as summary_mod  # noqa: E402  (sibling module, stdlib only)

_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,199}$")
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.]{0,31}$")
_RESULT_NAME = {
    "suite": "suite-run.json",
    "test": "test-run.json",
    "contracts": "contract-verify.json",
}


class InputError(ValueError):
    """An Action input failed validation; the run is refused (exit 2)."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _bool_input(name: str) -> bool:
    raw = _env(name).lower()
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise InputError(f"input {name.split('_')[-1].lower()!r} must be"
                     f" 'true' or 'false', got {raw!r}")


def _workspace() -> str:
    return os.path.realpath(_env("GITHUB_WORKSPACE") or os.getcwd())


def _contained_path(value: str, workspace: str, *, name: str,
                    expect: Optional[str]) -> str:
    """Validate a workspace-relative path (no absolute, no traversal, no
    symlink escape) and return it normalized. ``expect`` is ``file``, ``dir``,
    or ``None`` for a path that may not exist yet."""
    if "\n" in value or "\r" in value:
        raise InputError(f"input {name!r} must be a single line")
    if os.path.isabs(value):
        raise InputError(f"input {name!r} must be workspace-relative,"
                         f" got the absolute path {value!r}")
    norm = os.path.normpath(value)
    if norm == ".." or norm.startswith(".." + os.sep):
        raise InputError(f"input {name!r} escapes the workspace: {value!r}")
    real = os.path.realpath(os.path.join(workspace, norm))
    if real != workspace and not real.startswith(workspace + os.sep):
        raise InputError(f"input {name!r} resolves outside the workspace"
                         f" (symlink escape): {value!r}")
    if expect == "file" and not os.path.isfile(real):
        raise InputError(f"input {name!r} is not a regular file in the"
                         f" workspace: {value!r}")
    if expect == "dir" and not os.path.isdir(real):
        raise InputError(f"input {name!r} is not a directory in the"
                         f" workspace: {value!r}")
    return norm


def validate_inputs() -> Dict[str, Any]:
    """Validate every HOTATO_ACTION_* input BEFORE any install or run."""
    workspace = _workspace()
    suite = _env("HOTATO_ACTION_SUITE")
    test = _env("HOTATO_ACTION_TEST")
    contracts = _env("HOTATO_ACTION_CONTRACTS")
    modes = [m for m, v in (("suite", suite), ("test", test),
                            ("contracts", contracts)) if v]
    if len(modes) != 1:
        raise InputError(
            "exactly one of the inputs 'suite', 'test', 'contracts' is"
            f" required; got {modes or 'none'}"
        )
    mode = modes[0]
    cfg: Dict[str, Any] = {"mode": mode, "workspace": workspace}

    if mode == "suite":
        cfg["target"] = _contained_path(suite, workspace, name="suite",
                                        expect="file")
    elif mode == "test":
        cfg["target"] = _contained_path(test, workspace, name="test",
                                        expect="file")
    else:
        cfg["target"] = _contained_path(contracts, workspace,
                                        name="contracts", expect="dir")

    agent = _env("HOTATO_ACTION_AGENT")
    if mode in ("suite", "test"):
        if not agent:
            raise InputError("input 'agent' is required for a suite or test run")
        if not _AGENT_RE.match(agent):
            raise InputError("input 'agent' must be a safe identifier"
                             " (letters, digits, dot, underscore, hyphen)")
        cfg["agent"] = agent
    elif agent:
        raise InputError("input 'agent' does not apply to a contracts run")

    release = _env("HOTATO_ACTION_RELEASE") or _env("GITHUB_SHA")
    if release:
        if not _RELEASE_RE.match(release):
            raise InputError("input 'release' must be a safe identifier")
        cfg["release"] = release

    output = _env("HOTATO_ACTION_OUTPUT") or os.path.join(".hotato", "results")
    cfg["output"] = _contained_path(output, workspace, name="output",
                                    expect=None)

    parallel = _env("HOTATO_ACTION_PARALLEL")
    if parallel:
        if mode != "suite":
            raise InputError("input 'parallel' applies only to a suite run")
        try:
            n = int(parallel)
        except ValueError:
            raise InputError("input 'parallel' must be an integer")
        if not 1 <= n <= 64:
            raise InputError("input 'parallel' must be between 1 and 64")
        cfg["parallel"] = n

    gate_advisory = _bool_input("HOTATO_ACTION_GATE_ADVISORY")
    if gate_advisory and mode != "test":
        raise InputError(
            "input 'gate-advisory' applies only to a test run (a suite's"
            " advisory lane never gates; see docs/CI.md)"
        )
    cfg["gate_advisory"] = gate_advisory
    cfg["render_records"] = _bool_input("HOTATO_ACTION_RENDER_RECORDS")

    for name, env_name in (("transcript", "HOTATO_ACTION_TRANSCRIPT"),
                           ("trace", "HOTATO_ACTION_TRACE"),
                           ("state", "HOTATO_ACTION_STATE")):
        value = _env(env_name)
        if not value:
            continue
        allowed = ("test",) if name != "transcript" else ("test", "contracts")
        if mode not in allowed:
            raise InputError(f"input {name!r} does not apply to a"
                             f" {mode} run")
        cfg[name] = _contained_path(value, workspace, name=name, expect="file")

    version = _env("HOTATO_ACTION_VERSION") or "action"
    if version not in ("action", "preinstalled") and not _VERSION_RE.match(version):
        raise InputError(
            "input 'hotato-version' must be 'action', 'preinstalled', or an"
            " exact version such as 1.3.3 (never a range or 'latest')"
        )
    cfg["version"] = version
    return cfg


def _run(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, **kwargs)  # noqa: S603 (fixed argv list)


def install_hotato(cfg: Dict[str, Any]) -> str:
    """Install per the pinned mode; returns the install-source description."""
    version = cfg["version"]
    if version == "preinstalled":
        probe = _run([sys.executable, "-c", "import hotato"],
                     capture_output=True)
        if probe.returncode != 0:
            raise InputError("hotato-version 'preinstalled' but hotato is"
                             " not importable in this python")
        return "preinstalled"
    if version == "action":
        action_path = _env("HOTATO_ACTION_PATH")
        if not action_path or not os.path.isfile(
                os.path.join(action_path, "pyproject.toml")):
            raise InputError("the action path is unavailable; cannot run"
                             " the pinned Action revision")
        src = os.path.join(action_path, "src")
        if not os.path.isdir(os.path.join(src, "hotato")):
            raise InputError("the pinned Action checkout has no src/hotato tree")
        # Zero-egress by construction: the pinned checkout runs directly off
        # PYTHONPATH (gate.py always invokes ``python -m hotato``), so there is
        # no pip step, no isolated build backend fetched, and no package index
        # contact at all -- the executed code is exactly the pinned revision.
        # Subprocesses inherit this environment.
        prev = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = src + (os.pathsep + prev if prev else "")
        probe = _run([sys.executable, "-c", "import hotato"],
                     capture_output=True, text=True)
        if probe.returncode != 0:
            raise InputError("the pinned Action checkout is not importable:"
                             f" {(probe.stderr or '').strip()[-400:]}")
        return "action path (PYTHONPATH, zero-egress)"
    # An exact version pin installs from PyPI: index access is the documented
    # intent of this mode. hotato ships a wheel, so no source build runs.
    target, source = f"hotato=={version}", f"hotato=={version} (exact pin)"
    proc = _run([sys.executable, "-m", "pip", "install", "--no-deps",
                 "--quiet", target], capture_output=True, text=True)
    if proc.returncode != 0:
        raise InputError(f"pip install of {source} failed:"
                         f" {proc.stderr.strip()[-400:]}")
    return source


def hotato_version() -> str:
    proc = _run([sys.executable, "-m", "hotato", "--version"],
                capture_output=True, text=True)
    out = (proc.stdout or "").strip().split()
    return out[-1] if proc.returncode == 0 and out else "unknown"


def build_argv(cfg: Dict[str, Any]) -> List[str]:
    """The exact hotato argv (after ``hotato``), built as a list; no shell."""
    mode, target, output = cfg["mode"], cfg["target"], cfg["output"]
    if mode == "suite":
        argv = ["suite", "run", target, "--agent", cfg["agent"],
                "--format", "json", "--no-registry",
                "--out", os.path.join(output, "artifact")]
        if cfg.get("release"):
            argv += ["--release", cfg["release"]]
        if cfg.get("parallel"):
            argv += ["--parallel", str(cfg["parallel"])]
        return argv
    if mode == "test":
        argv = ["test", "run", target, "--agent", cfg["agent"],
                "--format", "json", "--no-store",
                "--out", os.path.join(output, "artifact")]
        for name in ("transcript", "trace", "state"):
            if cfg.get(name):
                argv += [f"--{name}", cfg[name]]
        if cfg["gate_advisory"]:
            argv.append("--gate-judge")
        return argv
    argv = ["contract", "verify", target, "--format", "json",
            "--junit", os.path.join(output, "contracts-junit.xml")]
    if cfg.get("transcript"):
        argv += ["--transcript", cfg["transcript"]]
    return argv


def run_hotato(cfg: Dict[str, Any]) -> Tuple[int, str]:
    """Run the command from the workspace; capture stdout to the result file.

    Returns (exit_code, result_path). The captured exit code is preserved
    exactly; stderr streams through to the step log.
    """
    workspace = cfg["workspace"]
    out_dir = os.path.join(workspace, cfg["output"])
    os.makedirs(out_dir, exist_ok=True)
    result_path = os.path.join(cfg["output"], _RESULT_NAME[cfg["mode"]])
    argv = [sys.executable, "-m", "hotato"] + build_argv(cfg)
    proc = _run(argv, cwd=workspace, capture_output=True, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    with open(os.path.join(workspace, result_path), "w",
              encoding="utf-8") as fh:
        fh.write(proc.stdout)
    return proc.returncode, result_path


def render_records(cfg: Dict[str, Any], result_path: str) -> Tuple[str, str]:
    """Optionally render Failure Records through ``hotato record render``.

    The renderer ships separately; when the installed hotato has no ``record``
    subcommand (or it fails for any reason) the Action carries on without it.
    Returns (records_dir_or_empty, note)."""
    if not cfg.get("render_records"):
        return "", ("not requested (set render-records: true once your"
                    " hotato version ships hotato record render)")
    records_dir = os.path.join(cfg["output"], "records")
    try:
        proc = _run(
            [sys.executable, "-m", "hotato", "record", "render", result_path,
             "--out", records_dir],
            cwd=cfg["workspace"], capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", "renderer unavailable; the gate result is unaffected"
    full = os.path.join(cfg["workspace"], records_dir)
    if proc.returncode == 0 and os.path.isdir(full) and os.listdir(full):
        return records_dir, ""
    return "", ("hotato record render is not available in this hotato"
                " version; the gate result is unaffected")


def write_outputs(values: Dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            value = str(value)
            if "\n" in value:
                value = value.replace("\n", " ")
            fh.write(f"{key}={value}\n")


def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def main() -> int:
    meta: Dict[str, Any] = {
        "action_ref": _env("GITHUB_ACTION_REF") or _env("GITHUB_ACTION_PATH")
        or "local",
    }
    outputs: Dict[str, str] = {
        "output": "", "suite-result": "", "summary": "", "records": "",
        "exit-code": "2", "status": "error", "hotato-version": "unknown",
    }
    exit_code: Optional[int] = None
    doc = None
    error: Optional[str] = None
    summary_path = ""

    try:
        cfg = validate_inputs()
        outputs["output"] = cfg["output"]
        meta["output"] = cfg["output"]
        meta["reproduce"] = shlex.join(["hotato"] + build_argv(cfg))
        meta["install_source"] = install_hotato(cfg)
        version = hotato_version()
        meta["hotato_version"] = version
        outputs["hotato-version"] = version
        exit_code, result_path = run_hotato(cfg)
        outputs["suite-result"] = result_path
        meta["result_path"] = result_path
        if cfg["mode"] == "contracts":
            junit = os.path.join(cfg["output"], "contracts-junit.xml")
            if os.path.isfile(os.path.join(cfg["workspace"], junit)):
                meta["junit_path"] = junit
        doc, error = summary_mod.load_result(
            os.path.join(cfg["workspace"], result_path))
        if error is not None and exit_code == 0:
            # A green exit without a readable machine result never passes:
            # the gate is owned by evidence, not by the process code alone.
            exit_code = 2
            error += " (exit raised to 2: no readable machine result)"
        records, records_note = render_records(cfg, result_path)
        outputs["records"] = records
        meta["records"] = records
        meta["records_note"] = records_note
        summary_path = os.path.join(cfg["output"], "summary.md")
        meta["summary_path"] = summary_path
    except InputError as exc:
        error = str(exc)
        exit_code = None
    except Exception as exc:  # never lose the summary or the outputs
        error = f"internal action error: {exc}"
        exit_code = exit_code if isinstance(exit_code, int) else None

    markdown, status = summary_mod.render(doc, exit_code, meta, error=error)
    gate = exit_code if isinstance(exit_code, int) else 2
    outputs["exit-code"] = str(gate)
    outputs["status"] = status

    if summary_path:
        try:
            workspace = _workspace()
            full = os.path.join(workspace, summary_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(markdown + "\n")
            outputs["summary"] = summary_path
        except OSError:
            outputs["summary"] = ""
    write_summary(markdown)
    write_outputs(outputs)
    print(markdown)
    return gate


if __name__ == "__main__":
    sys.exit(main())
