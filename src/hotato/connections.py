"""Local, private credential store for ``hotato connect`` (never phoned home).

``hotato connect <stack>`` captures the credentials for a voice stack once and
writes them here so ``hotato pull`` / ``hotato sweep`` can reuse them without
re-passing ``--api-key`` (and without ``--stack`` when exactly one stack is
connected). The store is a single JSON file at ``~/.hotato/connections.json``,
created with directory mode ``0700`` and file mode ``0600``.

Hard rules, enforced here:
  * The file is LOCAL ONLY. Nothing in this module makes a network call; the
    credentials are used solely by the per-stack fetch adapters to talk directly
    to the vendor's own API. They are never sent to any Hotato server.
  * The location is overridable with ``HOTATO_HOME`` (a directory), which keeps
    the tests hermetic (they point it at a tmp dir) and lets an operator relocate
    the store. Default: ``~/.hotato``.
  * Credential values are never logged or printed by this module; callers print
    the file PATH and the stack name, never the secret.

The stored shape is ``{stack: {field: value, ...}, ...}`` -- e.g.
``{"vapi": {"api_key": "..."}, "twilio": {"account_sid": "AC...",
"auth_token": "..."}}``.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, List, Optional

from .errors import open_regular as _open_regular

__all__ = [
    "home_dir",
    "connections_path",
    "load_all",
    "get",
    "save",
    "remove",
    "connected_stacks",
]


def home_dir() -> str:
    """The Hotato home directory: ``$HOTATO_HOME`` if set, else ``~/.hotato``."""
    override = os.environ.get("HOTATO_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".hotato")


def connections_path() -> str:
    """Absolute path to ``connections.json`` inside :func:`home_dir`."""
    return os.path.join(home_dir(), "connections.json")


def _refuse_if_insecure_mode(path: str) -> None:
    """Fail closed if the file at ``path`` is readable or writable by group
    or other. POSIX only -- Windows has no equivalent permission bits, so
    this is a no-op there, mirroring the os.fchmod guard used on the write
    path. Never includes any credential value in the raised message."""
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        # Can't stat it here; the normal open()/exists() handling below deals
        # with a genuinely missing or inaccessible path.
        return
    if mode & 0o077:
        raise ValueError(
            f"{path!r} is group/world-accessible (mode {oct(mode)}); "
            f"refusing to load credentials. Run `chmod 600 {path}` "
            "and re-run the command."
        )


def load_all() -> Dict[str, dict]:
    """Return the whole store as a dict. Missing file -> ``{}``. A corrupt or
    non-object file is a clean, explicit error (never a silent reset that would
    drop a working connection). A group/world-readable file is refused rather
    than silently trusted, so permission drift on a live-credential store is
    never read without warning.

    Note: only the file itself is checked, not its containing directory --
    ``_ensure_home`` sets the directory to ``0700`` on every write, but a
    caller may legitimately point ``HOTATO_HOME`` at a directory it does not
    own the permissions of (e.g. a shared/read-only mount) as long as the
    connections file inside it is properly locked down."""
    path = connections_path()
    if not os.path.exists(path):
        return {}
    _refuse_if_insecure_mode(path)
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{path!r} is not readable as JSON ({exc}). Fix or delete it, then "
            "re-run `hotato connect STACK`."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"{path!r} does not contain a JSON object of connections. Delete it "
            "and re-run `hotato connect STACK`."
        )
    # Only keep well-formed {stack: {field: value}} entries; ignore stray keys
    # rather than trusting arbitrary structure from a hand-edited file.
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def get(stack: str) -> Optional[dict]:
    """The stored credentials for ``stack``, or ``None`` if not connected."""
    return load_all().get((stack or "").strip().lower())


def connected_stacks() -> List[str]:
    """Sorted list of connected stack names."""
    return sorted(load_all().keys())


def _ensure_home() -> str:
    d = home_dir()
    # 0700: only the owner can traverse the directory that holds secrets.
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass
    return d


def save(stack: str, creds: Dict[str, str]) -> str:
    """Merge ``creds`` for ``stack`` into the store and write it back with file
    mode ``0600``. Returns the file path. Never logs the credential values.

    The write is atomic (temp file in the same dir + ``os.replace``) so a crash
    mid-write cannot corrupt an existing store, and the temp file is created
    ``0600`` from the start so the secret is never briefly world-readable.
    """
    stack = (stack or "").strip().lower()
    if not stack:
        raise ValueError("save() needs a stack name")
    clean = {k: v for k, v in creds.items() if v is not None and v != ""}
    if not clean:
        raise ValueError(f"no credentials to store for {stack!r}")

    d = _ensure_home()
    store = load_all()
    store[stack] = clean
    path = connections_path()

    fd, tmp = tempfile.mkstemp(prefix=".connections-", dir=d)
    try:
        # os.fchmod is POSIX-only. mkstemp already creates the file 0o600, so
        # on Windows (no fchmod, no POSIX mode bits) the credentials file is
        # still owner-scoped by the OS default ACL, and this call is a no-op
        # rather than an AttributeError crash on `hotato connect`.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass
    return path


def remove(stack: str) -> bool:
    """Delete ``stack`` from the store. Returns True if something was removed."""
    stack = (stack or "").strip().lower()
    store = load_all()
    if stack not in store:
        return False
    del store[stack]
    d = _ensure_home()
    path = connections_path()
    fd, tmp = tempfile.mkstemp(prefix=".connections-", dir=d)
    try:
        # os.fchmod is POSIX-only. mkstemp already creates the file 0o600, so
        # on Windows (no fchmod, no POSIX mode bits) the credentials file is
        # still owner-scoped by the OS default ACL, and this call is a no-op
        # rather than an AttributeError crash on `hotato connect`.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True
