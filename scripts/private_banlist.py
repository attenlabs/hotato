"""The maintainers' private name ban list, read from a file outside the repo.

Names that must never reach a public artifact are not written in this
repository in any form. Maintainers keep them as sha256 digests in a private
file, and three checks read it: the pre-publish leak scan
(``scripts/check_dist_no_leak.py``), the Atlas neutrality lint
(``scripts/build_atlas.py``) and the vendor-neutrality tests
(``tests/_banned.py``).

Location: ``$HOTATO_PRIVATE_BANLIST``, default
``~/.config/hotato/private-banlist.sha256`` (keep it mode 600).

Format, one entry per line; ``#`` starts a comment, and a comment after a
digest is that entry's label in findings::

    <sha256 hex> [internal|public]   # label

A digest is sha256 over one lowercased ``[a-z0-9]+`` token, or over 2 or 3
adjacent tokens joined by single spaces (so ``a-b``, ``a_b`` and ``a/b`` all
read as ``a b``). ``internal`` (the default) is refused everywhere: in a built
artifact, in Atlas text and in verdicts. ``public`` is refused only in Atlas
text and verdicts; use it for a public name that legitimately ships in files
such as LICENSE.

When the file is absent the name check is skipped with a one-line notice, so
CI and outside contributors run every other check. With
``HOTATO_REQUIRE_PRIVATE_BANLIST=1`` an absent or empty file raises
``BanlistUnavailable``; the publish runbook sets it for the pre-publish scan.
A malformed or unreadable file always raises.

Stdlib-only, no network.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from typing import Dict, Iterator, Optional, Tuple

ENV_PATH = "HOTATO_PRIVATE_BANLIST"
ENV_REQUIRE = "HOTATO_REQUIRE_PRIVATE_BANLIST"
DEFAULT_PATH = "~/.config/hotato/private-banlist.sha256"
SCOPES = ("internal", "public")
MAX_WORDS = 3

_HEX64 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[a-z0-9]+")

# (path, mtime_ns, size) -> {digest: (scope, label)}
_CACHE: Dict[Tuple[str, int, int], Dict[str, Tuple[str, str]]] = {}
_NOTICED: set = set()


class BanlistUnavailable(RuntimeError):
    """The private ban list is required but absent or empty, or it is malformed."""


def configured_path() -> str:
    """The path as configured (unexpanded, so a notice never prints a home dir)."""
    return os.environ.get(ENV_PATH) or DEFAULT_PATH


def required() -> bool:
    return os.environ.get(ENV_REQUIRE, "").strip() == "1"


def parse(text: str) -> Dict[str, Tuple[str, str]]:
    """``{digest: (scope, label)}`` from ban-list text; raise on a malformed line."""
    out: Dict[str, Tuple[str, str]] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        body, _, comment = raw.partition("#")
        fields = body.split()
        if not fields:
            continue
        digest = fields[0].lower()
        scope = fields[1].lower() if len(fields) > 1 else "internal"
        if len(fields) > 2 or not _HEX64.fullmatch(digest) or scope not in SCOPES:
            raise BanlistUnavailable(
                f"private ban list line {n}: expected '<sha256 hex> [internal|public]'")
        out[digest] = (scope, comment.strip() or f"private ban list line {n}")
    return out


def load() -> Optional[Dict[str, Tuple[str, str]]]:
    """Every entry, or ``None`` when the file is absent or empty and not required."""
    shown = configured_path()
    path = os.path.expanduser(shown)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise BanlistUnavailable(f"private ban list {shown}: {exc.strerror}") from None
    if st is not None:
        key = (path, st.st_mtime_ns, st.st_size)
        if key not in _CACHE:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                raise BanlistUnavailable(f"private ban list {shown}: unreadable ({exc})") from None
            _CACHE[key] = parse(text)
        if _CACHE[key]:
            return _CACHE[key]
        state = "is empty"
    else:
        state = "not found"
    if required():
        raise BanlistUnavailable(
            f"private ban list {state} at {shown} and {ENV_REQUIRE}=1: "
            "refusing to skip the internal-name check")
    if path not in _NOTICED:
        _NOTICED.add(path)
        print(f"notice: private ban list {state} at {shown} (${ENV_PATH}); "
              "internal-name check skipped", file=sys.stderr)
    return None


def digests(scope: str) -> Optional[Dict[str, str]]:
    """``{digest: label}`` for ``"internal"`` entries or for ``"all"`` entries;
    ``None`` when the check is skipped (file absent, not required)."""
    if scope not in ("internal", "all"):
        raise ValueError(f"unknown ban-list scope {scope!r}")
    entries = load()
    if entries is None:
        return None
    return {d: label for d, (s, label) in entries.items() if scope == "all" or s == scope}


def keys(text: str) -> Iterator[str]:
    """Each lowercased token of ``text`` and each run of 2..MAX_WORDS tokens."""
    toks = _TOKEN.findall(text.lower())
    for n in range(1, MAX_WORDS + 1):
        for i in range(len(toks) - n + 1):
            yield " ".join(toks[i:i + n])


def first_hit(text: str, names: Dict[str, str]) -> Optional[str]:
    """The label of the first banned token or phrase in ``text``, else ``None``."""
    if not names:
        return None
    seen: set = set()
    for key in keys(text):
        if key in seen:
            continue
        seen.add(key)
        label = names.get(hashlib.sha256(key.encode()).hexdigest())
        if label is not None:
            return label
    return None
