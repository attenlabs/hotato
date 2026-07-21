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

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import summary as summary_mod  # noqa: E402  (sibling module, stdlib only)

# Whether the openat-style publish primitive below is usable at all: a
# directory opened as a trusted descriptor plus dir_fd-relative opens. POSIX
# has both; Windows has NEITHER -- ``os.open`` of a DIRECTORY raises
# PermissionError (the CRT open cannot take a directory) and every ``dir_fd``
# argument raises NotImplementedError (``os.supports_dir_fd`` is empty there,
# per the os docs) -- so Windows publishes each leaf by path inside the
# private output directory the gate just created, where O_CREAT|O_EXCL still
# refuses ANY pre-existing name (a planted file or symlink) rather than
# truncating through it.
_HAS_DIR_FD = os.open in os.supports_dir_fd

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
    # Return the path with "/" separators on every OS (byte-identical on
    # POSIX, where os.sep IS "/"): the value lands in step outputs, the job
    # summary, and the reproduce command, all consumed by workflow YAML where
    # "/" is the convention, and Windows file APIs accept "/" wherever the
    # gate reuses it as a real path.
    return norm.replace(os.sep, "/")


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

    record_limit = _env("HOTATO_ACTION_RECORD_LIMIT") or "100"
    try:
        limit = int(record_limit)
    except ValueError:
        raise InputError("input 'record-limit' must be an integer between 1"
                         f" and 500, got {record_limit!r}")
    if not 1 <= limit <= 500:
        raise InputError("input 'record-limit' must be between 1 and 500,"
                         f" got {limit}")
    cfg["record_limit"] = limit

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
    if cfg.get("step_summary"):
        argv += ["--step-summary", cfg["step_summary"]]
    if cfg.get("pr_comment"):
        argv += ["--pr-comment", cfg["pr_comment"]]
    if cfg.get("transcript"):
        argv += ["--transcript", cfg["transcript"]]
    return argv


def _verify_flag_supported(flag: str) -> bool:
    """True when the INSTALLED hotato's ``contract verify`` knows ``flag``,
    probed from its own ``--help`` text (an exact older PyPI pin predates a
    flag and must keep its unchanged argv, exactly the graceful-degradation
    discipline ``_classify_unsupported`` applies to ``record render --all``).
    A failed probe reads as unsupported: the flag is simply not passed and the
    verify runs exactly as before."""
    try:
        proc = _run([sys.executable, "-m", "hotato", "contract", "verify",
                     "--help"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and flag in (proc.stdout or "")


def _step_summary_supported() -> bool:
    return _verify_flag_supported("--step-summary")


def _pr_comment_supported() -> bool:
    return _verify_flag_supported("--pr-comment")


def append_contract_step_summary(cfg: Dict[str, Any],
                                 meta: Dict[str, Any]) -> None:
    """Append hotato's own tight contract-verify Markdown (the
    ``--step-summary`` leaf the verify subprocess wrote inside the private
    output directory) to the job summary, ahead of the five-lane summary the
    gate renders below. Additive presentation and fail-open by contract: a
    missing or empty leaf and any read/write problem change NOTHING about
    the gate exit or the step outputs."""
    leaf = cfg.get("step_summary")
    if not leaf:
        return
    try:
        with open(os.path.join(cfg["workspace"], leaf), "r",
                  encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return
    if not content.strip():
        return
    try:
        write_summary(content.rstrip("\n"))
    except OSError:
        return
    meta["contract_summary_path"] = leaf


def _publish_flags() -> int:
    """Open flags for publishing a brand-new output leaf: create-exclusive so an
    existing name is refused, no-follow so a terminal symlink is never opened,
    write-only. O_NOFOLLOW is absent on some platforms (defaulted to 0)."""
    return (os.O_CREAT | os.O_EXCL | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0))


def _open_new(name: str, *, dir_fd: Optional[int] = None,
              dir_path: Optional[str] = None):
    """Create a NEW output file with no-follow/exclusive semantics and return a
    UTF-8 text handle. A planted symlink or pre-existing regular file at the
    path can never be truncated: O_CREAT|O_EXCL fails (EEXIST) on any existing
    name, and O_NOFOLLOW refuses a terminal symlink. ``name`` is opened relative
    to ``dir_fd`` (the private output directory) when given, so the write is not
    re-resolved through a hostile path prefix. ``dir_path`` is the path-based
    stand-in for platforms without dir_fd (see ``_HAS_DIR_FD``): the same
    exclusive-create flags inside the private directory. There CREATE_NEW
    refuses any existing name and any link to an existing target; a DANGLING
    planted symlink could still redirect the create, but the directory was
    created privately by this run moments earlier and symlink creation on
    Windows needs elevation, so the residual window has no unprivileged
    attacker. ``newline="\\n"`` keeps the published bytes identical across
    platforms: these files are hashed and compared byte-for-byte downstream,
    and the CRT's default newline translation would break that on Windows."""
    target = name if dir_fd is not None else os.path.join(dir_path or ".", name)
    try:
        fd = os.open(target, _publish_flags(), 0o600,
                     **({"dir_fd": dir_fd} if dir_fd is not None else {}))
    except OSError as exc:
        raise InputError(
            f"refusing to publish output {name!r}: a file already exists at "
            f"that path (possible planted symlink): {exc.strerror}"
        )
    return os.fdopen(fd, "w", encoding="utf-8", newline="\n")


def _prepare_output_dir(workspace: str, output: str) -> Tuple[str, Optional[int]]:
    """Create the workspace output directory PRIVATELY and refuse a pre-existing
    tree, then return (absolute_dir, dir_fd).

    A consumer PR can commit ``<output>/`` -- or the fixed leaves inside it
    (suite-run.json, summary.md, contracts-junit.xml, artifact/, records/) -- as
    symlinks, so a naive open()/makedirs would truncate or redirect an
    accessible target. Requiring the leaf directory to be created by us (mode
    0700, ``mkdir`` never follows a final symlink) guarantees it is empty and
    ours; the returned no-follow directory descriptor scopes every leaf write to
    that directory."""
    full = os.path.join(workspace, output)
    parent = os.path.dirname(full) or workspace
    os.makedirs(parent, exist_ok=True)
    try:
        os.mkdir(full, 0o700)
    except FileExistsError:
        raise InputError(
            f"output directory {output!r} already exists; the Action refuses "
            "to publish into a pre-existing tree because a committed symlink "
            "there could redirect a write. Use a fresh 'output' path."
        )
    except OSError as exc:
        raise InputError(f"cannot create output directory {output!r}: "
                         f"{exc.strerror}")
    if not _HAS_DIR_FD:
        # Windows (no dir_fd): there is no directory descriptor to anchor the
        # leaf writes, so leaves publish by path inside this just-created
        # private directory instead; ``_open_new``'s O_CREAT|O_EXCL still
        # refuses any pre-existing name there, never truncating a target.
        return full, None
    dir_fd = os.open(full, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    return full, dir_fd


def run_hotato(cfg: Dict[str, Any]) -> Tuple[int, str]:
    """Run the command from the workspace; capture stdout to the result file.

    Returns (exit_code, result_path). The captured exit code is preserved
    exactly; stderr streams through to the step log. The output directory is
    created privately and every fixed leaf is published with no-follow/
    exclusive semantics, so a planted symlink cannot make the Action truncate an
    accessible file. The private directory (created fresh, refused if it already
    exists) also contains the subprocess's ``artifact/``, ``contracts-junit.xml``
    and ``records/`` outputs, which therefore cannot land on a pre-planted link.
    """
    workspace = cfg["workspace"]
    full_out, dir_fd = _prepare_output_dir(workspace, cfg["output"])
    cfg["_output_fd"] = dir_fd
    cfg["_output_full"] = full_out
    result_name = _RESULT_NAME[cfg["mode"]]
    result_path = os.path.join(cfg["output"], result_name)
    argv = [sys.executable, "-m", "hotato"] + build_argv(cfg)
    proc = _run(argv, cwd=workspace, capture_output=True, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    with _open_new(result_name, dir_fd=dir_fd, dir_path=full_out) as fh:
        fh.write(proc.stdout)
    return proc.returncode, result_path


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECORD_DIR_RE = re.compile(r"^sha256-[0-9a-f]{64}$")


def _empty_record_set(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The neutral record-set result: no directory, no index, empty counts, no
    entries. Every gate path that produces no trustworthy record set returns
    this shape (only ``note``/``warning`` differ), so the outputs are uniformly
    empty and never claim records that were not written and validated."""
    return {
        "records_dir": "", "index_path": "", "count": "", "total": "",
        "entries": [], "truncated": False, "limit": cfg.get("record_limit", 100),
        "note": "", "warning": "",
    }


def _classify_unsupported(stderr: str) -> bool:
    """True when a non-zero ``record render`` means the INSTALLED hotato simply
    lacks ``record render --all`` (an exact older PyPI pin), rather than a
    genuine renderer error. Detected from argparse's own refusal text: an
    unknown ``record``/``render`` subcommand, or ``--all``/``--limit`` reported
    as unrecognized. Anything else is a real error and is surfaced as a
    warning."""
    s = stderr or ""
    if "invalid choice: 'record'" in s or "invalid choice: 'render'" in s:
        return True
    if "unrecognized arguments" in s and ("--all" in s or "--limit" in s):
        return True
    return False


def _source_digest_hex(workspace: str, result_path: str) -> Optional[str]:
    """sha256 (lowercase hex) of the machine-result bytes on disk -- the SAME
    bytes ``hotato record render`` hashes for the index's ``source.digest``, so
    the digest-scoped directory name matches the index the subprocess writes."""
    try:
        with open(os.path.join(workspace, result_path), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _validate_index(index: Any, source_hex: str) -> Optional[str]:
    """Structurally validate the record-set ``index.json`` WITHOUT trusting it:
    the closed kind/version, the source digest matching the machine result we
    hashed, integer counts, a boolean truncation flag, and a ``rendered`` count
    that matches the number of entries. Returns a reason string on the first
    violation, or ``None`` when the index is internally consistent."""
    if not isinstance(index, dict):
        return "index is not a JSON object"
    if index.get("kind") != "hotato.failure-record-index.v1":
        return f"unexpected index kind {index.get('kind')!r}"
    if index.get("version") != "1.0":
        return f"unexpected index version {index.get('version')!r}"
    source = index.get("source")
    if not isinstance(source, dict) or source.get("digest") != f"sha256:{source_hex}":
        return "index source digest does not match the machine result"
    total = index.get("total_failures")
    rendered = index.get("rendered")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return "index total_failures is not a non-negative integer"
    if not isinstance(rendered, int) or isinstance(rendered, bool) or rendered < 0:
        return "index rendered is not a non-negative integer"
    if not isinstance(index.get("truncated"), bool):
        return "index truncated is not a boolean"
    records = index.get("records")
    if not isinstance(records, list) or len(records) != rendered:
        return "index rendered count does not match its records array"
    if rendered > total:
        return "index rendered count exceeds total_failures"
    return None


def _read_record_set(cfg: Dict[str, Any], digest_scoped: str,
                     source_hex: str) -> Dict[str, Any]:
    """Read and VALIDATE the record set the renderer wrote into the private
    output tree, then build compact per-record summaries for the job summary.

    Nothing from ``index.json`` is trusted without a check: the index is
    structurally validated, every entry's content-addressed directory name is
    pattern-checked and resolved with ``realpath`` so it must land BENEATH the
    record-set root (no traversal, no symlink escape), and every listed
    ``failure-record.json`` is cross-checked to carry the same record id,
    status, headline, and test id the index advertised. If ANY check fails the
    whole set is treated as absent (empty outputs) and a warning is surfaced --
    records are never reported as present unless the index and every listed
    record validate."""
    result = _empty_record_set(cfg)
    workspace = cfg["workspace"]
    root_abs = os.path.realpath(os.path.join(workspace, digest_scoped))
    index_abs = os.path.join(root_abs, "index.json")
    try:
        with open(index_abs, "r", encoding="utf-8") as fh:
            index = json.load(fh)
    except (OSError, ValueError) as exc:
        result["warning"] = ("Failure Record index was not readable"
                             f" ({exc}); the gate result is unaffected")
        result["note"] = result["warning"]
        return result

    problem = _validate_index(index, source_hex)
    if problem is not None:
        result["warning"] = (f"Failure Record index failed validation"
                             f" ({problem}); the gate result is unaffected")
        result["note"] = result["warning"]
        return result

    total = int(index["total_failures"])
    rendered = int(index["rendered"])
    entries: List[Dict[str, str]] = []
    for entry in index["records"]:
        problem = _validate_entry(entry, workspace, root_abs)
        if problem is not None:
            result["warning"] = (f"Failure Record entry failed validation"
                                 f" ({problem}); the gate result is unaffected")
            result["note"] = result["warning"]
            return result
        entries.append({
            "test_id": entry["test_id"],
            "headline": entry["headline"],
            "path": os.path.join(digest_scoped, entry["directory"],
                                 "failure-record.md"),
        })

    if total == 0:
        # An all-pass source writes a zero-record index (never a fabricated
        # failure). There is nothing to upload, so the record outputs stay
        # empty; the count is reported honestly and the summary says so.
        result["count"] = "0"
        result["total"] = "0"
        result["note"] = "0 non-passing units"
        return result

    result["records_dir"] = digest_scoped
    result["index_path"] = os.path.join(digest_scoped, "index.json")
    result["count"] = str(rendered)
    result["total"] = str(total)
    result["truncated"] = bool(index["truncated"])
    result["entries"] = entries
    return result


def _validate_entry(entry: Any, workspace: str, root_abs: str) -> Optional[str]:
    """Validate ONE index entry and its child ``failure-record.json``: the
    id/directory patterns, containment of the child under the record-set root
    (realpath, so a symlink or ``..`` escape is refused), and a cross-check of
    record id, status, headline, and test id against the child file. Returns a
    reason string on the first violation, else ``None``."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    record_id = entry.get("record_id")
    directory = entry.get("directory")
    for key in ("status", "test_id", "headline"):
        if not isinstance(entry.get(key), str) or not entry.get(key):
            return f"entry {key} is missing"
    if not isinstance(record_id, str) or not _RECORD_ID_RE.match(record_id):
        return "entry record_id is malformed"
    if not isinstance(directory, str) or not _RECORD_DIR_RE.match(directory):
        return "entry directory is malformed"
    if directory != "sha256-" + record_id.split(":", 1)[1]:
        return "entry directory does not match its record_id"
    # Containment: the child directory and record file must resolve BENEATH the
    # record-set root; realpath collapses any symlink or traversal first.
    child_dir = os.path.realpath(os.path.join(root_abs, directory))
    if child_dir != os.path.join(root_abs, directory):
        return "entry directory escaped the record-set root"
    if not (child_dir == root_abs or child_dir.startswith(root_abs + os.sep)):
        return "entry directory is outside the record-set root"
    record_abs = os.path.join(child_dir, "failure-record.json")
    if os.path.realpath(record_abs) != record_abs:
        return "failure-record.json escaped the record-set root"
    try:
        with open(record_abs, "r", encoding="utf-8") as fh:
            child = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"failure-record.json unreadable ({exc})"
    if not isinstance(child, dict):
        return "failure-record.json is not an object"
    subject = child.get("subject")
    child_test = subject.get("test_id") if isinstance(subject, dict) else None
    if (child.get("record_id") != record_id
            or child.get("status") != entry["status"]
            or child.get("headline") != entry["headline"]
            or child_test != entry["test_id"]):
        return "failure-record.json does not match its index entry"
    return None


def render_records(cfg: Dict[str, Any], result_path: str) -> Dict[str, Any]:
    """Render one share-safe Failure Record per non-passing unit through
    ``hotato record render --all`` into a source-digest-scoped directory under
    the consumer's configured output root, then read and validate the result.

    The records land under ``<output>/records/sha256-<source hex>/``, inside the
    private output directory ``run_hotato`` created fresh (mode 0700, refused if
    it already exists) and holds a no-follow descriptor to; the record files are
    therefore covered by the SAME no-follow/exclusive containment guarantee as
    the result file and ``summary.md``. This function itself never opens a
    record path for WRITING -- the renderer subprocess does, exclusively inside
    that private tree -- and it reads the index and child records only after a
    realpath containment check under the record-set root.

    The gate exit is owned by hotato's evaluation. A presentation failure here
    NEVER changes it: on a renderer error the record outputs stay empty and a
    warning is surfaced; the caller preserves the exit code untouched. Returns a
    record-set dict (see ``_empty_record_set``)."""
    if not cfg.get("render_records"):
        result = _empty_record_set(cfg)
        result["note"] = ("not requested (render-records: false; local Failure"
                          " Record artifacts are disabled)")
        return result

    workspace = cfg["workspace"]
    limit = cfg.get("record_limit", 100)
    source_hex = _source_digest_hex(workspace, result_path)
    if source_hex is None or not _SHA256_HEX.match(source_hex):
        result = _empty_record_set(cfg)
        result["warning"] = ("could not hash the machine result to render"
                             " Failure Records; the gate result is unaffected")
        result["note"] = result["warning"]
        return result

    # "/"-joined for the same reason _contained_path normalizes its return:
    # this string is a step output consumed by workflow YAML on every runner
    # OS (byte-identical to os.path.join on POSIX).
    digest_scoped = "/".join((cfg["output"], "records",
                              "sha256-" + source_hex))
    try:
        proc = _run(
            [sys.executable, "-m", "hotato", "record", "render", result_path,
             "--all", "--limit", str(limit), "--out", digest_scoped],
            cwd=workspace, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = _empty_record_set(cfg)
        result["warning"] = (f"Failure Record renderer did not run ({exc});"
                             " the gate result is unaffected")
        result["note"] = result["warning"]
        return result

    if proc.returncode != 0:
        result = _empty_record_set(cfg)
        if _classify_unsupported(proc.stderr):
            result["note"] = ("installed version does not support record sets"
                              " (hotato record render --all); the gate result"
                              " is unaffected")
            return result
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = detail[-1].strip() if detail else f"exit {proc.returncode}"
        result["warning"] = (f"Failure Record rendering failed: {detail}; the"
                             " gate result is unaffected")
        result["note"] = result["warning"]
        return result

    return _read_record_set(cfg, digest_scoped, source_hex)


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


def _emit_warning(message: str) -> None:
    """Surface a GitHub Actions ``::warning::`` annotation. It is a presentation
    signal only -- it never changes the gate exit code."""
    print("::warning::" + " ".join(str(message).split()))


def main() -> int:
    meta: Dict[str, Any] = {
        "action_ref": _env("GITHUB_ACTION_REF") or _env("GITHUB_ACTION_PATH")
        or "local",
    }
    outputs: Dict[str, str] = {
        "output": "", "suite-result": "", "summary": "", "records": "",
        "records-index": "", "records-count": "", "records-total": "",
        "pr-comment": "",
        "exit-code": "2", "status": "error", "hotato-version": "unknown",
    }
    exit_code: Optional[int] = None
    doc = None
    error: Optional[str] = None
    summary_path = ""
    cfg: Optional[Dict[str, Any]] = None
    record_warning = ""

    try:
        cfg = validate_inputs()
        outputs["output"] = cfg["output"]
        meta["output"] = cfg["output"]
        meta["reproduce"] = shlex.join(["hotato"] + build_argv(cfg))
        meta["install_source"] = install_hotato(cfg)
        version = hotato_version()
        meta["hotato_version"] = version
        outputs["hotato-version"] = version
        if cfg["mode"] == "contracts":
            # hotato renders its own tight verify Markdown (and the share-safe
            # PR-comment block) into leaves of the private output directory; the
            # reproduce line is recomputed so it stays the exact executed argv.
            # Each leaf is probed independently, so an exact older pin without a
            # given flag keeps its unchanged argv (graceful degradation).
            if _step_summary_supported():
                cfg["step_summary"] = "/".join((cfg["output"],
                                                "contract-summary.md"))
            if _pr_comment_supported():
                cfg["pr_comment"] = "/".join((cfg["output"],
                                              "contract-pr-comment.md"))
            if cfg.get("step_summary") or cfg.get("pr_comment"):
                meta["reproduce"] = shlex.join(["hotato"] + build_argv(cfg))
        exit_code, result_path = run_hotato(cfg)
        outputs["suite-result"] = result_path
        meta["result_path"] = result_path
        if cfg["mode"] == "contracts":
            junit = os.path.join(cfg["output"], "contracts-junit.xml")
            if os.path.isfile(os.path.join(cfg["workspace"], junit)):
                meta["junit_path"] = junit
            append_contract_step_summary(cfg, meta)
            # Expose the rendered PR-comment leaf as an output (a poster step
            # posts it; presentation only, and the leaf is a direct child of
            # the private output directory the verify subprocess wrote into).
            pr_leaf = cfg.get("pr_comment")
            if pr_leaf and os.path.isfile(os.path.join(cfg["workspace"],
                                                       pr_leaf)):
                outputs["pr-comment"] = pr_leaf
                meta["pr_comment_path"] = pr_leaf
        doc, error = summary_mod.load_result(
            os.path.join(cfg["workspace"], result_path))
        if error is not None and exit_code == 0:
            # A green exit without a readable machine result never passes:
            # the gate is owned by evidence, not by the process code alone.
            exit_code = 2
            error += " (exit raised to 2: no readable machine result)"
        # Failure Records are rendered ONLY from a machine result that loaded
        # cleanly (a malformed/missing result gets no record attempt). A
        # rendering failure never touches exit_code below.
        if error is None and isinstance(doc, dict):
            rec = render_records(cfg, result_path)
        else:
            rec = _empty_record_set(cfg)
            rec["note"] = ("no record attempt: the machine result was missing"
                           " or malformed")
        outputs["records"] = rec["records_dir"]
        outputs["records-index"] = rec["index_path"]
        outputs["records-count"] = rec["count"]
        outputs["records-total"] = rec["total"]
        meta["records"] = rec["records_dir"]
        meta["records_note"] = rec["note"]
        meta["record_set"] = rec
        record_warning = rec.get("warning") or ""
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

    dir_fd = cfg.get("_output_fd") if isinstance(cfg, dict) else None
    out_dir = cfg.get("_output_full") if isinstance(cfg, dict) else None
    if summary_path and (isinstance(dir_fd, int) or out_dir):
        # summary.md is a direct child of the private output directory created
        # by run_hotato; publish it descriptor-relative with the same no-follow/
        # exclusive semantics so a planted symlink is refused, never followed
        # (path-based inside the same private directory without dir_fd support).
        try:
            with _open_new("summary.md", dir_fd=dir_fd, dir_path=out_dir) as fh:
                fh.write(markdown + "\n")
            outputs["summary"] = summary_path
        except (OSError, InputError):
            outputs["summary"] = ""
    if isinstance(dir_fd, int):
        try:
            os.close(dir_fd)
        except OSError:
            pass
    if record_warning:
        # A renderer error is visible but inert: it never changed `gate`.
        _emit_warning(record_warning)
    write_summary(markdown)
    write_outputs(outputs)
    print(markdown)
    return gate


if __name__ == "__main__":
    sys.exit(main())
