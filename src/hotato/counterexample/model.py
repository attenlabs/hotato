"""Closed constants, canonical encoding, and fail-closed filesystem helpers."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
from typing import Any, Dict, Iterable, List, Optional, Tuple

KIND = "hotato.counterexample.v1"
ORACLE_KIND = "hotato.counterexample-oracle.v1"
CERTIFICATE_KIND = "hotato.reduction-certificate.v1"
VERSION = 1
REDUCER_SET = "hotato.reducers.v1"
ALGORITHM = "hierarchical-ddmin"

PRIVATE_PROFILE = "private-runnable-v1"
SHARE_PROFILE = "share-safe-v1"
PROFILES = (PRIVATE_PROFILE, SHARE_PROFILE)

PRESERVED = "PRESERVED"
ABSENT = "ABSENT"
DRIFTED = "DRIFTED"
UNRESOLVED = "UNRESOLVED"

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 96
MAX_TURNS = 10_000
MAX_TOOLS = 10_000
MAX_BUDGET = 100_000
DEFAULT_BUDGET = 512
MAX_CAPSULE_FILES = 1_024
MAX_CAPSULE_BYTES = 256 * 1024 * 1024
MAX_CAPSULE_MEMBER_BYTES = 64 * 1024 * 1024


class CounterexampleRefusal(ValueError):
    """A deterministic refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(obj: Any, *, pretty: bool = False) -> str:
    """RFC-8259 JSON with a stable key order and a trailing newline."""
    kwargs: Dict[str, Any] = {
        "sort_keys": True,
        "ensure_ascii": True,
        "allow_nan": False,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return json.dumps(obj, **kwargs) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_obj(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def prefixed_digest(obj: Any) -> str:
    return "sha256:" + digest_obj(obj)


def assert_finite(value: Any, where: str = "input") -> None:
    """Reject non-finite numbers recursively before hashing or writing."""
    stack: List[Tuple[Any, str, int]] = [(value, where, 0)]
    while stack:
        current, path, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise CounterexampleRefusal(
                "input_too_deep", f"{where} exceeds the {MAX_JSON_DEPTH}-level JSON depth limit"
            )
        if isinstance(current, float) and not math.isfinite(current):
            raise CounterexampleRefusal(
                "non_finite_number", f"{path} contains NaN or Infinity"
            )
        if isinstance(current, dict):
            for key, child in current.items():
                stack.append((child, f"{path}.{key}", depth + 1))
        elif isinstance(current, list):
            for index, child in enumerate(current):
                stack.append((child, f"{path}[{index}]", depth + 1))


def _regular_no_symlink(path: str) -> os.stat_result:
    try:
        lst = os.lstat(path)
    except OSError:
        raise
    if stat.S_ISLNK(lst.st_mode):
        raise CounterexampleRefusal(
            "symlink_refused", f"{path!r} is a symlink; counterexample inputs must be regular files"
        )
    if not stat.S_ISREG(lst.st_mode):
        raise CounterexampleRefusal(
            "special_file_refused", f"{path!r} is not a regular file"
        )
    return lst


def require_within_workspace(path: str, workspace: str) -> str:
    """Resolve an existing regular file and refuse traversal/symlink escape."""
    ws = os.path.realpath(os.path.abspath(workspace))
    if not os.path.isdir(ws):
        raise CounterexampleRefusal("workspace_missing", f"workspace {workspace!r} is not a directory")
    absolute = os.path.abspath(path)
    resolved = os.path.realpath(absolute)
    try:
        inside = os.path.commonpath([ws, resolved]) == ws
    except ValueError:
        inside = False
    if not inside:
        raise CounterexampleRefusal(
            "workspace_escape", f"{path!r} resolves outside the declared workspace"
        )
    # Reject a symlink at any path component between workspace and the file.
    rel = os.path.relpath(absolute, ws)
    cursor = ws
    for part in rel.split(os.sep):
        cursor = os.path.join(cursor, part)
        if os.path.islink(cursor):
            raise CounterexampleRefusal(
                "symlink_refused", f"{path!r} contains a symlink component"
            )
    st = _regular_no_symlink(absolute)
    if st.st_size > MAX_INPUT_BYTES:
        raise CounterexampleRefusal(
            "input_too_large",
            f"{path!r} is {st.st_size} bytes; limit is {MAX_INPUT_BYTES}",
        )
    return absolute


def read_regular_bytes(path: str, *, max_bytes: int = MAX_INPUT_BYTES) -> bytes:
    # The initial lstat gives a clear refusal.  O_NOFOLLOW plus fstat closes the
    # lstat->open substitution window on platforms that expose it; O_NONBLOCK
    # prevents a raced FIFO from hanging the verifier on platforms that do not.
    st = _regular_no_symlink(path)
    if st.st_size > max_bytes:
        raise CounterexampleRefusal(
            "input_too_large", f"{path!r} is {st.st_size} bytes; limit is {max_bytes}"
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CounterexampleRefusal("symlink_refused", f"{path!r} became a symlink") from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise CounterexampleRefusal("special_file_refused", f"{path!r} is not a regular file")
        if opened.st_size > max_bytes:
            raise CounterexampleRefusal(
                "input_too_large", f"{path!r} is {opened.st_size} bytes; limit is {max_bytes}"
            )
        chunks: List[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise CounterexampleRefusal("input_too_large", f"{path!r} exceeds {max_bytes} bytes")
    return data


def load_json(path: str, *, max_bytes: int = MAX_INPUT_BYTES) -> Any:
    def reject_duplicate_names(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        value: Dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise CounterexampleRefusal(
                    "duplicate_json_name", f"{path!r} contains duplicate object name {key!r}"
                )
            value[key] = child
        return value

    try:
        value = json.loads(
            read_regular_bytes(path, max_bytes=max_bytes).decode("utf-8"),
            object_pairs_hook=reject_duplicate_names,
        )
    except UnicodeDecodeError as exc:
        raise CounterexampleRefusal("invalid_utf8", f"{path!r} is not UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CounterexampleRefusal("invalid_json", f"{path!r} is not JSON: {exc}") from exc
    assert_finite(value, os.path.basename(path))
    return value


def validate_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CounterexampleRefusal("invalid_budget", "budget must be an integer >= 1")
    if value > MAX_BUDGET:
        raise CounterexampleRefusal(
            "budget_too_large", f"budget {value} exceeds the hard limit {MAX_BUDGET}"
        )
    return value


def count_leaves(value: Any) -> int:
    if isinstance(value, dict):
        return sum(count_leaves(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_leaves(v) for v in value)
    return 1


def mode_private(path: str) -> None:
    """Best-effort POSIX privacy modes; no-op semantics on non-POSIX hosts."""
    try:
        if os.path.isdir(path):
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o600)
    except OSError:
        pass


def inventory_files(root: str, *, exclude: Iterable[str] = ()) -> List[Dict[str, Any]]:
    excluded = set(exclude)
    rows: List[Dict[str, Any]] = []
    total_bytes = 0
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs:
            absolute_dir = os.path.join(base, name)
            lst = os.lstat(absolute_dir)
            if stat.S_ISLNK(lst.st_mode):
                raise CounterexampleRefusal(
                    "symlink_refused", f"{absolute_dir!r} is a symlink directory"
                )
            if not stat.S_ISDIR(lst.st_mode):
                raise CounterexampleRefusal(
                    "special_file_refused", f"{absolute_dir!r} is not a directory"
                )
        for name in files:
            absolute = os.path.join(base, name)
            rel = os.path.relpath(absolute, root).replace(os.sep, "/")
            if rel in excluded:
                continue
            _regular_no_symlink(absolute)
            data = read_regular_bytes(absolute, max_bytes=MAX_CAPSULE_MEMBER_BYTES)
            total_bytes += len(data)
            if len(rows) + 1 > MAX_CAPSULE_FILES:
                raise CounterexampleRefusal(
                    "capsule_too_many_files", f"capsule exceeds {MAX_CAPSULE_FILES} files"
                )
            if total_bytes > MAX_CAPSULE_BYTES:
                raise CounterexampleRefusal(
                    "capsule_too_large", f"capsule exceeds {MAX_CAPSULE_BYTES} bytes"
                )
            rows.append({"path": rel, "sha256": sha256_bytes(data), "bytes": len(data)})
    return rows
