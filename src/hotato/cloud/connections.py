"""Voice-platform connection probes. Fixed hosts only: the URL is never taken
from user input, so a saved key cannot be pointed at an internal address."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Tuple

PROVIDERS = {
    "vapi": "Vapi",
    "retell": "Retell",
}


def probe(provider: str, api_key: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """One cheap authenticated read. Returns (ok, short human detail); the key
    is never part of the detail."""
    if provider == "vapi":
        req = urllib.request.Request(
            "https://api.vapi.ai/call?limit=1",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "hotato-cloud"})
    elif provider == "retell":
        req = urllib.request.Request(
            "https://api.retellai.com/v3/list-calls", method="POST",
            data=json.dumps({"limit": 1}).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "User-Agent": "hotato-cloud"})
    else:
        return False, "unknown provider"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 fixed https host
            return (200 <= resp.status < 300), f"{PROVIDERS[provider]} answered HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"{PROVIDERS[provider]} rejected the key (HTTP {exc.code})"
        return False, f"{PROVIDERS[provider]} answered HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, f"could not reach {PROVIDERS[provider]}"
