"""Name bans for the vendor-neutrality tests, read from the private ban list.

The banned internal and maintainer names are not in this repository in any
form: they live as sha256 digests in the maintainers' private ban list (see
``scripts/private_banlist.py``; ``$HOTATO_PRIVATE_BANLIST``, default
``~/.config/hotato/private-banlist.sha256``). ``leaks(text)`` is True when a
token or a run of 2-3 tokens of ``text`` hashes to an entry.

Without the file (CI, outside contributors) ``leaks`` is always False and a
one-line notice goes to stderr; the public vendor-name checks in each test
still run. ``HOTATO_REQUIRE_PRIVATE_BANLIST=1`` makes an absent or empty file
fail the calling test.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_banlist_module():
    name = "hotato_private_banlist"
    mod = sys.modules.get(name)
    if mod is None:
        path = os.path.join(_ROOT, "scripts", "private_banlist.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
    return mod


_BANLIST = _load_banlist_module()


def leaks(text: str, internal_only: bool = False) -> bool:
    """True when a banned token/phrase appears. ``internal_only`` checks just
    the ``internal`` entries (not the maintainer's public names)."""
    try:
        names = _BANLIST.digests("internal" if internal_only else "all")
    except _BANLIST.BanlistUnavailable as exc:
        pytest.fail(str(exc))
    return _BANLIST.first_hit(text, names) is not None if names else False
