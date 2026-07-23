"""Candidate-identity binding: the hermetic half of the candidate-bound proof path.

``hotato prove --candidate-config-hash H --provider vapi`` raises a before/after
proof to claim_scope ``candidate_revision`` -- but only when ``H`` actually binds
the candidate that ran. This module is what PRODUCES a legitimate ``H``: it
fetches the provider's live assistant config, strips the volatile identity noise
that changes without a semantic change, and computes a deterministic sha256 over
what remains. The same config hashes identically on every machine and every run;
a real config change (a moved interruption threshold, a new system prompt) moves
the hash, and nothing else does.

The flow the operator runs:

    hotato candidate hash   --provider vapi --assistant <id>            # BEFORE
    # ... drive the before/after calls against the candidate ...
    hotato candidate verify --provider vapi --assistant <id> --expect H # AFTER
    hotato prove --before ... --after ... --candidate-config-hash H --provider vapi

``candidate verify`` is the refuse-on-drift gate: it re-fetches, recomputes, and
exits non-zero when the config no longer hashes to ``H``. A drifted candidate
means the before/after calls and the config no longer describe the same thing, so
a ``candidate_revision`` proof over that run is void.

Scope of the binding (documented plainly, matching prove's honesty walls): the
config hash is a MEASURED binding of the candidate's CONFIGURATION -- it says
"these before/after calls ran against a config that hashes to H" -- it is NOT an
authentication of the runner. ``prove``'s evidence_authority stays ``measured``;
this binding never claims ``runner_authenticated``.

Zero third-party dependencies: stdlib ``hashlib`` / ``json`` / ``os`` for the
pure hashing path, and the vendored ``capture`` HTTP primitives (stdlib
``urllib`` under the hood, with the credential-safe redirect handler and the
default-deny SSRF guard) for the one network function.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional, Tuple

from .errors import load_json_file as _load_json_file

__all__ = [
    "VOLATILE_KEYS",
    "PROVIDERS",
    "canonicalize_config",
    "config_hash",
    "fetch_vapi_assistant",
    "fetch_assistant",
    "resolve_api_key",
    "hash_from_config_file",
    "hash_from_provider",
    "verify_from_provider",
]

# Config keys that change WITHOUT a semantic change, so two fetches of the SAME
# logical candidate config must hash identically regardless of them. Stripped at
# EVERY nesting level (a nested ``updatedAt`` is dropped too). This set is
# deliberately CONSERVATIVE and EXPLICIT: anything not listed here is kept, so a
# genuine semantic field is never silently dropped from the binding.
#   * id / orgId              -- server-assigned identity of the assistant/org.
#   * createdAt / updatedAt   -- server-set timestamps that move on every read
#                                that touches the record, with no config change.
#   * isServerUrlSecretSet    -- a server-set secret-PRESENCE flag (whether a
#                                server-url secret exists), not the config itself.
VOLATILE_KEYS = frozenset({
    "id",
    "orgId",
    "createdAt",
    "updatedAt",
    "isServerUrlSecretSet",
})

# Providers whose live assistant config `candidate hash`/`verify` can fetch. Only
# vapi is implemented; the dispatch refuses an unknown provider as a usage error
# so more can be added without changing the surface.
PROVIDERS = ("vapi",)

_PROVIDER_ENV = {"vapi": "VAPI_API_KEY"}
_PROVIDER_BASE_URL = {"vapi": "https://api.vapi.ai"}


# =========================================================================
# pure path: canonicalize + hash (stdlib-only, deterministic, no network)
# =========================================================================

def _strip_volatile(value: Any) -> Any:
    """Recursively drop every :data:`VOLATILE_KEYS` key from ``value``. Recurses
    into nested dicts AND lists so a volatile key nested anywhere is removed."""
    if isinstance(value, dict):
        return {
            key: _strip_volatile(val)
            for key, val in value.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def canonicalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``config`` with the volatile identity-noise keys stripped at every
    level, so two fetches of the same logical config canonicalize identically.

    Only the keys in :data:`VOLATILE_KEYS` are removed; every semantic field
    (model, voice, firstMessage, transcriber, the start/stopSpeakingPlan
    interruption settings, the system prompt, tools, ...) is kept verbatim."""
    if not isinstance(config, dict):
        raise ValueError(
            "a candidate config must be a JSON object (the provider's assistant "
            f"config), got {type(config).__name__}"
        )
    return _strip_volatile(config)


def config_hash(config: Dict[str, Any]) -> str:
    """``"sha256:" + sha256`` over the canonical JSON of
    :func:`canonicalize_config` -- sorted keys, no insignificant whitespace,
    ``ensure_ascii`` -- so the same logical config yields the same hash on every
    machine and run, and a semantic change (and only a semantic change) moves it.
    ``allow_nan=False`` refuses a digest over a value that cannot round-trip
    through a standard JSON reader."""
    canonical = canonicalize_config(config)
    blob = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


# =========================================================================
# network path: fetch a live assistant config (the ONLY network function)
# =========================================================================

def fetch_vapi_assistant(
    assistant_id: str,
    api_key: str,
    base_url: str = "https://api.vapi.ai",
    timeout: int = 30,
) -> Dict[str, Any]:
    """``GET {base_url}/assistant/{assistant_id}`` with ``Authorization: Bearer
    <key>`` and return the parsed assistant config object.

    Thin and separately testable: it does nothing but fetch and parse. The
    response is treated as untrusted data -- parsed as JSON (never eval'd) and
    required to be a JSON object. Network / HTTP / non-JSON failures surface as
    the tool's standard clean usage ``ValueError`` (via the shared capture HTTP
    primitives, which also carry the credential-safe redirect handler and the
    default-deny SSRF guard), never a raw traceback."""
    from urllib.parse import quote

    from . import capture as _capture

    aid = str(assistant_id or "").strip()
    if not aid:
        raise ValueError(
            "an assistant id is required to fetch a Vapi assistant config"
        )
    key = str(api_key or "").strip()
    if not key:
        raise ValueError(
            "a Vapi API key is required to fetch an assistant config"
        )
    url = f"{str(base_url).rstrip('/')}/assistant/{quote(aid, safe='')}"
    obj = _capture._http_get_json(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=timeout,
    )
    return _capture._require_json_object(obj, f"Vapi assistant {aid!r}")


def _require_known_provider(provider: Optional[str]) -> str:
    prov = (provider or "").strip().lower()
    if prov not in PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r}; candidate binding supports: "
            + ", ".join(PROVIDERS)
        )
    return prov


def fetch_assistant(
    provider: str,
    assistant_id: str,
    api_key: str,
    *,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Provider dispatch for the live-config fetch. Only ``vapi`` is implemented;
    an unknown provider is a clean usage error so more can be added later."""
    prov = _require_known_provider(provider)
    if prov == "vapi":
        return fetch_vapi_assistant(
            assistant_id, api_key,
            base_url=base_url or _PROVIDER_BASE_URL["vapi"],
            timeout=timeout,
        )
    # unreachable: _require_known_provider gates PROVIDERS, kept as a guard so a
    # newly-listed provider without a branch fails loudly rather than silently.
    raise ValueError(  # pragma: no cover
        f"provider {prov!r} is listed but has no fetch implementation"
    )


def resolve_api_key(provider: str, api_key: Optional[str]) -> str:
    """The API key for ``provider``: the explicit ``--api-key`` if given, else the
    provider's environment variable (``VAPI_API_KEY`` for vapi). Raises a clean
    usage error when neither is present."""
    if api_key and str(api_key).strip():
        return str(api_key).strip()
    prov = (provider or "").strip().lower()
    env = _PROVIDER_ENV.get(prov)
    if env:
        val = os.environ.get(env, "")
        if val and val.strip():
            return val.strip()
    raise ValueError(
        f"a {prov or 'provider'} API key is required: pass --api-key or set the "
        f"{env or 'provider'} environment variable"
    )


# =========================================================================
# orchestration: the two things the CLI does (kept here so both are testable)
# =========================================================================

def hash_from_config_file(path: str) -> Tuple[str, Dict[str, Any]]:
    """Hash a LOCAL config file (no network, no key). Returns
    ``(config_hash, canonicalized_config)``."""
    config = _load_json_file(path, label=f"candidate config {path!r}")
    return config_hash(config), canonicalize_config(config)


def hash_from_provider(
    provider: str,
    assistant: str,
    api_key: Optional[str],
    *,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Tuple[str, Dict[str, Any]]:
    """Fetch the live config and hash it. Returns
    ``(config_hash, canonicalized_config)``."""
    prov = _require_known_provider(provider)
    key = resolve_api_key(prov, api_key)
    config = fetch_assistant(prov, assistant, key, base_url=base_url, timeout=timeout)
    return config_hash(config), canonicalize_config(config)


def verify_from_provider(
    provider: str,
    assistant: str,
    expect: str,
    api_key: Optional[str],
    *,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Re-fetch, recompute, and compare to ``expect``. Returns
    ``{provider, expected, actual, held}`` where ``held`` is True only when the
    recomputed hash equals ``expect`` (the candidate is unchanged)."""
    expected = str(expect or "").strip()
    if not expected:
        raise ValueError(
            "candidate verify needs --expect HASH: the config hash you recorded "
            "before the change (from `hotato candidate hash`)"
        )
    prov = _require_known_provider(provider)
    key = resolve_api_key(prov, api_key)
    config = fetch_assistant(prov, assistant, key, base_url=base_url, timeout=timeout)
    actual = config_hash(config)
    return {
        "provider": prov,
        "expected": expected,
        "actual": actual,
        "held": actual == expected,
    }
