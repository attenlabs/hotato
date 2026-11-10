"""Digest-based word bans for the vendor-neutrality tests.

The banned words are stored only as sha256 digests of the lowercased word
token (or of the space-joined 2- or 3-word phrase), so no test file spells them.
``leaks(text)`` returns True when any token or adjacent token pair of ``text``
hashes into the set.
"""

from __future__ import annotations

import hashlib
import re

_DIGESTS = frozenset({
    "9231737bc7cc04d1ff1027e7733795ff06bf44d611b5fd0b46863e23542463e1",
    "73e56e619961e6561fa67fb0f786167aa171d91ace9307c584abf1c61aa81687",
    "ace427bac30b0b6b6b86493a5271335d259e0d106b703e4bed7724f4dcdfd64f",
    "a88c512f80dc3381d3b3b1bf52c20ffdf0439cbf512f0500e16d80d78c7d396b",
    "9a8834fadfbad28d94bfa114bfaacba3ebbab704d43c5d60da6cdf08a372ec11",
    "89391ff3d40f359ee8ded90db82ea10fb67545005bb00314ebe113ece67621fd",
})


def _keys(text: str):
    toks = re.findall(r"[a-z0-9]+", text.lower())
    yield from toks
    for n in (2, 3):
        for i in range(len(toks) - n + 1):
            yield " ".join(toks[i:i + n])


_INTERNAL = frozenset({
    "9231737bc7cc04d1ff1027e7733795ff06bf44d611b5fd0b46863e23542463e1",
})


def leaks(text: str, internal_only: bool = False) -> bool:
    """True when a banned token/phrase appears; ``internal_only`` checks just
    the internal model name (not the public company name)."""
    pool = _INTERNAL if internal_only else _DIGESTS
    return any(hashlib.sha256(k.encode()).hexdigest() in pool for k in _keys(text))
