"""Real per-stack capture: turn a call you actually ran into a scored verdict.

This is the *out-of-box aha*. Instead of scoring synthetic fixtures, point Hotato
at one of YOUR OWN recordings and get the same three timing signals and the same
honest fix. One command per stack:

    hotato capture --stack vapi   --call-id <id>          # + VAPI_API_KEY
    hotato capture --stack retell --call-id <id>          # + RETELL_API_KEY
    hotato capture --stack twilio --recording-sid RE...   # + TWILIO_ACCOUNT_SID/TOKEN
    hotato capture --stack livekit --caller a.wav --agent b.wav
    hotato capture --stack pipecat --stereo captured.wav

    hotato setup --stack <stack>          # scaffold the exact recording config
    hotato capture --stack <stack> --demo # prove the loop, fully offline, no deps

Design rules honoured here:
  * The core scorer stays stdlib-only. Vapi, Retell and Twilio capture use nothing
    but ``urllib`` + your API key -> near-zero friction. LiveKit and Pipecat live
    capture run inside YOUR infra; ``setup`` scaffolds them and ``capture`` scores
    the file they produce. Every stack SDK is imported lazily, never at module load.
  * Every stack has a ``--demo`` that copies a bundled two-channel reference and
    runs it straight through the scorer, so the capture -> score loop works
    end-to-end OFFLINE with zero third-party deps and zero network.
  * Honesty is unchanged: energy is not intent; no accuracy percentage; no claim
    that any adapter was tested against a live stack in this build -- the live
    paths use the documented APIs and are marked for live verification on your side.

The scoring itself is delegated unchanged to ``hotato.core.run_single`` (the
vendored MIT engine). This module adds only capture plumbing.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
from typing import Callable, Optional, Tuple

from . import errors as _errors
from ._engine.audio import write_wav  # noqa: F401  (used by the pipecat scaffold)
from .core import process_exit_code, run_single
from .errors import wav_read as _wav_read

# Provider metadata and action responses are small JSON documents.  Recording
# downloads are deliberately given a much larger ceiling, but still cannot ask
# the process to allocate without bound.
_HTTP_JSON_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
_HTTP_RECORDING_RESPONSE_MAX_BYTES = 512 * 1024 * 1024
_HTTP_ERROR_DETAIL_MAX_BYTES = 4 * 1024

__all__ = [
    "STACKS",
    "MONO_STACKS",
    "CAPTURE_STACKS",
    "DUAL_PULL_STACKS",
    "PULL_STACKS",
    "CONNECT_STACKS",
    "LIST_STACKS",
    "STACK_CHANNELS",
    "CONNECT_SPECS",
    "score",
    "score_two_channel",
    "report",
    "demo",
    "capture",
    "capture_vapi",
    "capture_twilio",
    "capture_retell",
    "capture_bland",
    "capture_elevenlabs",
    "capture_synthflow",
    "capture_millis",
    "capture_cartesia",
    "fetch_one",
    "list_calls",
    "pull",
    "resolve_stack",
    "resolve_creds",
    "auth_check",
    "setup_text",
    "run_capture",
    "run_setup",
    "run_connect",
    "run_pull",
    "run_sweep",
]

# The original five capture/setup stacks (unchanged: existing tests + `setup`
# scaffolds are keyed on exactly this tuple).
STACKS = ("vapi", "twilio", "livekit", "pipecat", "retell")

# Stacks treated as mono/mixed (no trusted per-party channel separation), so
# every one is scored ONLY behind an explicit --allow-mono / HOTATO_ALLOW_MONO=1
# opt-in, labelled indicative only. NOTE the two provenance tiers, kept honest:
#   * bland / elevenlabs / synthflow / millis are spec-CONFIRMED mono-only
#     (INTEGRATION-SPEC-2026-07-07.md tags each [yes-mono-only]).
#   * cartesia is spec-tagged [unclear]: the spec could NOT confirm its channel
#     count either way (dual_channel: UNCLEAR / NOT CONFIRMED IN DOCS), so it is
#     DEFENSIVELY treated as mono (fail-safe default) until a live test proves
#     otherwise -- NOT because the spec verified it. capture_cartesia's own
#     docstring says the same; do not "upgrade" this to a verified claim.
MONO_STACKS = ("bland", "elevenlabs", "synthflow", "millis", "cartesia")

# `hotato capture --stack` accepts the original five plus the mono adapters.
CAPTURE_STACKS = STACKS + MONO_STACKS

# Auto-pull, dual-channel: Hotato fetches a separated (2-channel) recording.
DUAL_PULL_STACKS = ("vapi", "twilio", "retell")

# `connect` / `pull` / `sweep` operate on the vendor-hosted-recording stacks.
# LiveKit and Pipecat are capture-in-your-infra (no vendor list/fetch), so they
# are deliberately NOT here; Regal is webhook-push only (no list, no REST fetch).
PULL_STACKS = DUAL_PULL_STACKS + MONO_STACKS
CONNECT_STACKS = PULL_STACKS

# Channel mode per stack (drives the --allow-mono gate on pull/capture).
STACK_CHANNELS = {
    "vapi": "dual", "twilio": "dual", "retell": "dual",
    "bland": "mono", "elevenlabs": "mono", "synthflow": "mono",
    "millis": "mono", "cartesia": "mono",
}

# Stacks whose list-recent-calls endpoint the spec confirms VERBATIM. Retell is
# excluded on purpose: the spec marks its list-calls endpoint unconfirmed/none,
# so Hotato never fabricates one -- pull Retell from an explicit --call-id list.
LIST_STACKS = ("vapi", "twilio", "bland", "elevenlabs", "synthflow",
               "millis", "cartesia")

# Per-stack credential contract for connect/pull/sweep. ``fields`` are required;
# ``optional`` are only needed for the list endpoint (e.g. Synthflow's model_id,
# Cartesia's agent_id) and are surfaced with an honest error at list time if
# missing. ``env`` maps each field to the environment variable it falls back to.
CONNECT_SPECS = {
    "vapi": {"fields": ["api_key"], "optional": [],
             "env": {"api_key": "VAPI_API_KEY"}},
    "retell": {"fields": ["api_key"], "optional": [],
               "env": {"api_key": "RETELL_API_KEY"}},
    "twilio": {"fields": ["account_sid", "auth_token"], "optional": [],
               "env": {"account_sid": "TWILIO_ACCOUNT_SID",
                       "auth_token": "TWILIO_AUTH_TOKEN"}},
    "bland": {"fields": ["api_key"], "optional": [],
              "env": {"api_key": "BLAND_API_KEY"}},
    "elevenlabs": {"fields": ["api_key"], "optional": [],
                   "env": {"api_key": "ELEVENLABS_API_KEY"}},
    "synthflow": {"fields": ["api_key"], "optional": ["model_id"],
                  "env": {"api_key": "SYNTHFLOW_API_KEY",
                          "model_id": "SYNTHFLOW_MODEL_ID"}},
    "millis": {"fields": ["api_key"], "optional": ["base_url"],
               "env": {"api_key": "MILLIS_API_KEY",
                       "base_url": "MILLIS_BASE_URL"}},
    "cartesia": {"fields": ["api_key"], "optional": ["agent_id"],
                 "env": {"api_key": "CARTESIA_API_KEY",
                         "agent_id": "CARTESIA_AGENT_ID"}},
}

# Each stack's --demo uses a bundled two-channel reference so the loop runs with
# zero deps and zero network. All bundled fixtures PASS, so every demo exits 0.
_DEMO_SCENARIO = {
    "vapi": "01-hard-interruption",     # clean yield -- the flagship happy path
    "twilio": "05-telephony-8khz",      # telephony-flavoured yield
    "livekit": "01-hard-interruption",  # yield
    "pipecat": "02-backchannel-mhm",    # a HOLD: the agent should keep the floor
    "retell": "08-rapid-turn-taking",   # yield
}


# --- bundled resources ----------------------------------------------------

def _bundled_audio(name: str):
    from importlib import resources  # deferred: costs ~17ms at interpreter start

    return resources.files("hotato").joinpath("data", "audio", name)


def _scenario_meta(scenario_id: str) -> Tuple[Optional[float], str, str]:
    """Read a bundled scenario label -> (caller_onset_sec, expect, title)."""
    from importlib import resources  # deferred: costs ~17ms at interpreter start

    label = resources.files("hotato").joinpath(
        "data", "scenarios", scenario_id + ".json"
    )
    # open-ok: bundled importlib resource (installed package data, not a user path)
    sc = json.loads(label.read_text(encoding="utf-8"))
    onset = sc.get("caller_onset_sec")
    expect = "yield" if sc.get("expected", {}).get("yield", True) else "hold"
    return onset, expect, sc.get("title", scenario_id)


# --- scoring (the one thing every path funnels into) ----------------------

def score(
    wav_path: str,
    *,
    stack: str = "generic",
    onset_sec: Optional[float] = None,
    expect: str = "yield",
    caller_channel: int = 0,
    agent_channel: int = 1,
) -> dict:
    """Score ONE two-channel capture (caller on ``caller_channel``, agent on
    ``agent_channel``) through the tool and return the standard envelope.

    ``expect`` is 'yield' (the agent should stop for a real interruption) or
    'hold' (the caller event is a backchannel and the agent should keep the floor).
    """
    return run_single(
        stereo=wav_path,
        stack=stack,
        onset_sec=onset_sec,
        expect=expect,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
    )


def score_two_channel(
    caller_wav: str,
    agent_wav: str,
    *,
    stack: str = "generic",
    onset_sec: Optional[float] = None,
    expect: str = "yield",
) -> dict:
    """Score two MONO recordings (one per party) -- e.g. two LiveKit track egresses."""
    return run_single(
        caller=caller_wav,
        agent=agent_wav,
        stack=stack,
        onset_sec=onset_sec,
        expect=expect,
    )


def report(env: dict, fmt: str = "text") -> int:
    """Print the scored verdict (the three timing signals + PASS/FAIL + any fix)
    and return the process exit code for the envelope. ``fmt`` is 'text' or
    'json'. The JSON envelope itself is printed untouched; the return value is
    ``core.process_exit_code(env)``, which maps a single run whose every event
    is not scorable to the CLI's exit-2 unusable-input convention."""
    pec = process_exit_code(env)
    if fmt == "json":
        print(_errors.safe_json_dumps(env, indent=2))
        return pec
    ev = env["events"][0]
    v = ev["verdict"]
    print(f"hotato [capture] stack={env['stack']} offline={env['offline']}")
    if ev.get("scorable") is False:
        # An input problem, never an agent verdict: no PASS, no FAIL.
        print(f"  [NOT SCORABLE] {ev['event_id']}")
        print(f"         reason: {ev['not_scorable_reason']}")
    else:
        tty = v["seconds_to_yield"]
        tty_s = "-" if tty is None else f"{tty:.2f}s"
        mark = "PASS" if v["passed"] else "FAIL"
        print(
            f"  [{mark}] {ev['event_id']}: did_yield={v['did_yield']} "
            f"seconds_to_yield={tty_s} talk_over={v['talk_over_sec']:.2f}s"
        )
        echo = (ev.get("signals") or {}).get("echo") or {}
        if v["did_yield"] and echo.get("echo_suspected"):
            print(
                "         WARNING: this yield coincides with high cross-channel "
                f"echo coherence ({echo.get('coherence')} at lag "
                f"{echo.get('lag_sec')}s). The caller channel looks like a copy "
                "of the agent's own audio, so the agent may have yielded to its "
                "own voice bleed, not a real caller. Do not treat this as a "
                "clean yield; check echo cancellation / channel separation."
            )
        if not v["passed"] and ev.get("fix"):
            fx = ev["fix"]
            print(f"         fix[{fx['fix_class']}]: {fx['title']}")
            if fx["fix_class"] == "config" and fx.get("knob"):
                print(f"            knob: {fx['knob']['parameter']}")
                print(f"            move: {fx['knob']['direction']}")
            elif fx["fix_class"] == "engagement-control" and fx.get("pointer"):
                print(f"            -> {fx['pointer']['layer']}")
    # Fully-scorable runs keep the exact `exit_code=` line; when the process
    # code differs (all-not-scorable single run -> 2), print that instead of
    # the misleading envelope 0.
    if pec != env["exit_code"]:
        print(f"  process_exit_code={pec}")
    else:
        print(f"  exit_code={env['exit_code']}")
    return pec


# --- zero-dependency, offline demo ----------------------------------------

def demo(stack: str, fmt: str = "text") -> int:
    """Copy a bundled two-channel reference for ``stack`` and score it end to end.

    No live agent, no third-party deps, no network: this stands in for "a capture
    wrote this WAV" and proves the capture -> score loop before you wire anything.
    """
    stack = stack.strip().lower()
    scenario_id = _DEMO_SCENARIO.get(stack, "01-hard-interruption")
    onset, expect, title = _scenario_meta(scenario_id)
    out = tempfile.NamedTemporaryFile(
        prefix=f"hotato-{stack}-", suffix=".captured.wav", delete=False
    ).name
    from importlib import resources  # deferred: costs ~17ms at interpreter start

    with resources.as_file(_bundled_audio(scenario_id + ".example.wav")) as src:
        shutil.copyfile(src, out)
    # Progress is logging, not output: it goes to STDERR so that under
    # --format json stdout stays a single, parseable envelope (the live
    # vapi/retell/twilio paths already log their '[stack] downloaded' lines to
    # stderr for the same reason). In text mode it still shows, just on stderr.
    sys.stderr.write(
        f"[demo] {stack}: bundled two-channel reference '{scenario_id}' ({title})\n")
    sys.stderr.write(f"[demo] wrote two-channel capture -> {out}\n")
    env = score(out, stack=stack, onset_sec=onset, expect=expect)
    return report(env, fmt)


# --- tiny stdlib HTTP (keeps the core zero-dependency) --------------------

class _HTTPStatusError(ValueError):
    """HTTP error that keeps the status code so callers can branch on it
    (e.g. Twilio returns 400 when dual-channel media is unavailable)."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class _CredentialSafeRedirectHandler:
    """urllib follows 3xx redirects and, by default, RE-SENDS every original
    request header -- INCLUDING ``Authorization`` -- to the redirect target even
    when it is a DIFFERENT host. Every authenticated call here carries a Bearer
    API key (Vapi/Retell/Bland/Synthflow/Millis/Cartesia) or a Twilio Basic
    ``AccountSid:AuthToken`` token, so a tampered/compromised vendor endpoint, a
    malicious CDN/proxy in front of it, a DNS-poisoned path, or an operator's bad
    ``--base-url`` could 302 the request to attacker infra and receive the full
    credential verbatim (= vendor-account takeover).

    This handler strips credential headers whenever a redirect crosses to a
    different normalized origin (scheme + canonical host + effective port), so
    a path-only redirect on the same origin still works but a different port or
    an HTTPS-to-HTTP downgrade never receives the secret. It complements
    ``_auth_headers_for``, which only guards the vendor-JSON-supplied URL BEFORE
    the fetch and does nothing once the vendor's own endpoint issues a redirect
    mid-request."""

    def __new__(cls):
        import urllib.request

        base = urllib.request.HTTPRedirectHandler

        class _Handler(base):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                new = super().redirect_request(req, fp, code, msg, headers, newurl)
                if new is not None and not _same_origin(req.full_url, new.full_url):
                    for collection in (new.headers, new.unredirected_hdrs):
                        for h in list(collection):
                            if h.lower() in (
                                "authorization", "proxy-authorization", "cookie",
                            ):
                                del collection[h]
                # A 3xx can point the request at an internal / metadata IP just as
                # the original URL could, so re-apply the default-deny SSRF guard
                # to the redirect TARGET (not just the credential-stripping check).
                if new is not None:
                    from urllib.parse import urlparse

                    host = urlparse(new.full_url).hostname
                    if host:
                        _reject_private_host(host, "a redirected recording fetch")
                return new

        return _Handler()


_SAFE_OPENER_INSTALLED = False


def _ensure_safe_opener() -> None:
    """Install (once, process-wide) a default urllib opener whose redirect handler
    strips credentials on a cross-origin redirect. Installed lazily on the first
    network call so importing the module has no global side effect. Tests that
    monkeypatch ``urllib.request.urlopen`` replace the call entirely and are
    unaffected; the real ``urlopen`` uses this opener in production.

    ``build_opener()`` always installs a default ``ProxyHandler``, which honors
    the standard ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY`` env-var convention
    (like curl/pip/git) so hotato works behind a corporate proxy. TLS
    certificate verification is never disabled, so a proxy cannot silently
    read or alter a credentialed request without also controlling a CA the
    machine already trusts. See docs/THREAT-MODEL.md's "Network trust"
    section for the full reasoning. A caller who does not trust the ambient
    proxy environment can set ``HOTATO_NO_PROXY=1`` to force this opener to
    ignore ``HTTP_PROXY``/``HTTPS_PROXY`` for every request in this process,
    or unset those env vars before running the command."""
    global _SAFE_OPENER_INSTALLED
    if _SAFE_OPENER_INSTALLED:
        return
    import urllib.request

    handlers: list = [_CredentialSafeRedirectHandler()]
    if os.environ.get("HOTATO_NO_PROXY", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        # An empty proxies dict REPLACES the default (env-derived) ProxyHandler
        # that build_opener() would otherwise install, forcing every request
        # through this opener to ignore HTTP_PROXY/HTTPS_PROXY.
        handlers.append(urllib.request.ProxyHandler({}))
    urllib.request.install_opener(urllib.request.build_opener(*handlers))
    _SAFE_OPENER_INSTALLED = True


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:  # pragma: no cover
        return "0"


def _http_get(url: str, headers: Optional[dict] = None, timeout: int = 60,
              *, max_bytes: int = _HTTP_JSON_RESPONSE_MAX_BYTES) -> bytes:
    import http.client
    import socket
    import urllib.error
    import urllib.request

    _ensure_safe_opener()
    # Provider APIs (Vapi, Retell, ...) sit behind Cloudflare, which 403s (error
    # 1010) the DEFAULT urllib User-Agent as a bot signature -- the request never
    # reaches the vendor and a valid key looks like an auth failure. Send an
    # explicit hotato UA (honest, not a spoofed browser) so the credential probe
    # and every pull actually reach the API. Callers may still override it.
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", f"hotato/{_version()} (+https://hotato.dev)")
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user-supplied API
            return _errors.read_bounded_http_body(
                resp,
                max_bytes=max_bytes,
                subject=f"response from {_errors.sanitize_url(url)}",
            )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = _errors.read_bounded_http_body(
                exc,
                max_bytes=_HTTP_ERROR_DETAIL_MAX_BYTES,
                subject="HTTP error response",
            ).decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise _HTTPStatusError(
            f"HTTP {exc.code} from {_errors.sanitize_url(url)}: "
            f"{_errors.sanitize_urls_in_text(exc.reason)}. "
            f"{_errors.sanitize_urls_in_text(body)}".strip(), exc.code
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - live path
        raise ValueError(
            f"network error fetching {_errors.sanitize_url(url)}: "
            f"{_errors.sanitize_urls_in_text(exc.reason)}"
        ) from exc
    except (TimeoutError, socket.timeout, ConnectionError, http.client.IncompleteRead) as exc:
        raise ValueError(
            f"connection interrupted while reading {_errors.sanitize_url(url)}: "
            f"{_errors.sanitize_urls_in_text(exc)}"
        ) from exc


def _http_get_json(url: str, headers: Optional[dict] = None, timeout: int = 60):
    """GET ``url`` and parse a JSON body. A 200 response whose body is NOT JSON
    (a WAF/captcha interstitial, an HTML error page, an expired-session redirect,
    or a typo'd --base-url all commonly served with HTTP 200) would otherwise
    surface as a raw, context-free ``json.JSONDecodeError`` ("Expecting value:
    line 1 column 1"). Turn it into the same clean, actionable usage error the
    sibling validators (_require_json_object / _require_url_str) give: name the
    URL and show the start of the non-JSON body."""
    raw = _http_get(url, headers=headers, timeout=timeout).decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = _errors.sanitize_urls_in_text(" ".join(raw.split())[:200])
        raise ValueError(
            f"expected a JSON response from {_errors.sanitize_url(url)}, "
            "got a non-JSON body "
            f"({exc.msg}). This is usually a proxy/CDN or WAF error page, an "
            "expired-session HTML redirect, a vendor outage page served with "
            "HTTP 200, or a wrong --base-url. First bytes: "
            f"{preview!r}"
        ) from exc


def _http_post(
    url: str,
    data: bytes,
    headers: Optional[dict] = None,
    timeout: int = 60,
    content_type: str = "application/json",
    *,
    max_bytes: int = _HTTP_JSON_RESPONSE_MAX_BYTES,
) -> bytes:
    """POST ``data`` (bytes) to ``url`` and return the response body. The
    write-side twin of :func:`_http_get`, sharing every safety property that path
    already has: the lazy process-wide safe opener (so a cross-host 3xx redirect
    strips the ``Authorization`` header before it can leak a credential), the
    explicit ``hotato/<ver>`` User-Agent (Cloudflare 403s urllib's default UA
    before the key is ever checked), and the ``_HTTPStatusError``-carrying-``.code``
    on an HTTP error so a caller can branch on the status (e.g. Twilio's 400).

    The only verb this issues is POST -- there is no PUT/PATCH/DELETE surface
    here, so the drive-a-call path can CREATE a provider call but can never mutate
    an existing provider resource (an assistant config, a number) in place. The
    clone/apply path's own primitive (``apply._http_json``) enforces the same
    GET/POST-only allowlist; this is the capture-side equivalent for the call
    origination + status-poll flow."""
    import http.client
    import socket
    import urllib.error
    import urllib.request

    _ensure_safe_opener()
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", f"hotato/{_version()} (+https://hotato.dev)")
    hdrs.setdefault("Content-Type", content_type)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user-supplied API
            return _errors.read_bounded_http_body(
                resp,
                max_bytes=max_bytes,
                subject=f"response from POST {_errors.sanitize_url(url)}",
            )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = _errors.read_bounded_http_body(
                exc,
                max_bytes=_HTTP_ERROR_DETAIL_MAX_BYTES,
                subject="HTTP error response",
            ).decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise _HTTPStatusError(
            f"HTTP {exc.code} from POST {_errors.sanitize_url(url)}: "
            f"{_errors.sanitize_urls_in_text(exc.reason)}. "
            f"{_errors.sanitize_urls_in_text(body)}".strip(), exc.code
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - live path
        raise ValueError(
            f"network error posting to {_errors.sanitize_url(url)}: "
            f"{_errors.sanitize_urls_in_text(exc.reason)}"
        ) from exc
    except (TimeoutError, socket.timeout, ConnectionError, http.client.IncompleteRead) as exc:
        raise ValueError(
            f"connection interrupted while posting to {_errors.sanitize_url(url)}: "
            f"{_errors.sanitize_urls_in_text(exc)}"
        ) from exc


def _parse_json_response(raw: str, url: str) -> object:
    """Parse a JSON body or raise the same clean, named usage error the GET path
    gives (a proxy/CDN error page, an HTML redirect, or a vendor outage served
    with a 2xx would otherwise surface as a context-free JSONDecodeError). Shared
    by the POST-JSON and POST-form helpers so both write paths report a non-JSON
    body identically to :func:`_http_get_json`."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = _errors.sanitize_urls_in_text(" ".join(raw.split())[:200])
        raise ValueError(
            f"expected a JSON response from POST {_errors.sanitize_url(url)}, "
            "got a non-JSON body "
            f"({exc.msg}). This is usually a proxy/CDN or WAF error page, an "
            "expired-session HTML redirect, a vendor outage page served with a "
            "2xx, or a wrong --base-url. First bytes: "
            f"{preview!r}"
        ) from exc


def _http_post_json(url: str, body: object, headers: Optional[dict] = None,
                    timeout: int = 60):
    """POST a JSON ``body`` and parse the JSON response, mirroring
    :func:`_http_get_json` on the write side. Used to originate a Vapi call
    (``POST /call``)."""
    raw = _http_post(
        url, json.dumps(body).encode("utf-8"), headers=headers, timeout=timeout,
        content_type="application/json",
    ).decode("utf-8", "replace")
    return _parse_json_response(raw, url)


def _http_post_form(url: str, fields: dict, headers: Optional[dict] = None,
                    timeout: int = 60):
    """POST an ``application/x-www-form-urlencoded`` body (Twilio's REST content
    type) and parse the JSON response. Used to originate a Twilio call
    (``POST /2010-04-01/Accounts/{sid}/Calls.json``)."""
    from urllib.parse import urlencode

    raw = _http_post(
        url, urlencode(fields).encode("utf-8"), headers=headers, timeout=timeout,
        content_type="application/x-www-form-urlencoded",
    ).decode("utf-8", "replace")
    return _parse_json_response(raw, url)


def _require_json_object(value, what: str) -> dict:
    """The vendor endpoints here are documented to return a single JSON OBJECT.
    A proxy/CDN error page, a misconfigured ``--base-url``, or a vendor failure can
    instead return a JSON array, string, or null -- which ``json.loads`` accepts and
    which then blows up on the first ``.get()`` with a raw AttributeError. Reject a
    non-object here so it is a clean usage error, matching the isinstance checks the
    list adapters already do."""
    if not isinstance(value, dict):
        raise ValueError(
            f"expected a JSON object for {what}, got {type(value).__name__}. The "
            "endpoint returned an unexpected shape (a proxy/error page, a wrong "
            "--base-url, or a vendor failure response)."
        )
    return value


def _require_url_str(value, what: str) -> str:
    """A recording location pulled out of a vendor JSON response (``stereoUrl``,
    ``recording_multi_channel_url``, ``recording_url``, ``recording.recording_url``
    ...) is documented to be a string, but a proxy/error page, a wrong
    ``--base-url``, or a vendor failure can put a list / dict / number there
    instead. Reject a non-string (or empty) here with a clean, named ValueError so
    it never reaches ``urlparse`` in ``_validate_download_url`` /
    ``_same_origin`` /
    ``_auth_headers_for`` as a raw AttributeError. Mirrors ``_require_json_object``
    for the URL fields the adapters read."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"expected a URL string for {what}, got {type(value).__name__}. The "
            "vendor response returned an unexpected shape for the recording "
            "location (a proxy/error page, a wrong --base-url, or a vendor "
            "failure response)."
        )
    return value


_ALLOWED_DOWNLOAD_SCHEMES = ("http", "https")


def _canonical_host(h: str) -> str:
    """Canonicalize a hostname for HOTATO_INGEST_ALLOWED_HOSTS comparison.

    ``urlparse`` lowercases an IPv6 literal but does NOT canonicalize it, so
    ``http://[::1]/x`` and ``http://[0:0:0:0:0:0:0:1]/x`` yield the
    textually-different hostnames ``::1`` and ``0:0:0:0:0:0:0:1`` even though
    they are the same address. Comparing those raw strings against the
    operator's allowlist makes an allowed IPv6 host spuriously fail the
    allowlist check depending on which form the vendor or the operator
    happened to write. Route both the allowlist entries and the incoming
    hostname through ``ipaddress.ip_address`` first so equal addresses always
    compare equal, regardless of literal form (zero-padded, expanded,
    bracketed, or shorthand); a non-IP hostname (a DNS name) falls back to a
    plain lowercase compare, unchanged from before.

    This only affects which hosts are treated as MATCHING the allowlist; the
    default-deny SSRF guard (``_reject_private_host``) resolves and checks
    every hostname independently afterward regardless of the allowlist
    outcome, so this helper cannot widen what is ultimately fetchable -- it
    only fixes false-negative (spurious deny) allowlist comparisons."""
    stripped = h.strip().strip("[]")
    try:
        import ipaddress

        return str(ipaddress.ip_address(stripped))
    except ValueError:
        return h.strip().lower()


def _resolve_host_addresses(hostname: str):
    """Resolve ``hostname`` to a list of IP strings. A single seam so the SSRF
    guard has ONE resolver and tests can stub it. An IP literal resolves to
    itself with no network I/O."""
    import socket

    return [info[4][0] for info in socket.getaddrinfo(hostname, None)]


def _reject_private_host(hostname: str, what: str) -> None:
    """Default-deny SSRF guard: resolve ``hostname`` and refuse if ANY resolved
    address is loopback / private (RFC1918) / link-local (169.254.0.0/16 incl.
    the AWS/GCP/Azure metadata endpoint 169.254.169.254) / multicast / reserved /
    unspecified. Both the vendor-JSON download URL and the untrusted webhook
    recording_url flow through here BEFORE any fetch, so a compromised vendor
    account, tampered metadata, malicious --base-url, or a spoofed webhook can no
    longer make the host fetch an internal service or cloud-metadata endpoint.

    This is default-DENY, not opt-in: the operator can set
    ``HOTATO_ALLOW_PRIVATE_URLS=1`` to deliberately permit internal hosts (a
    local test recording server), but the safe posture requires no configuration.

    A hostname that does not resolve is left to the fetch layer to fail on its
    own (there is no reachable target, so it is not an SSRF vector); only a host
    that DOES resolve to a non-public address is refused here."""
    import ipaddress

    if os.environ.get("HOTATO_ALLOW_PRIVATE_URLS", "").strip() in ("1", "true", "TRUE"):
        return
    try:
        addrs = _resolve_host_addresses(hostname)
    except OSError:
        # Unresolvable -> the real fetch cannot reach anything either; not SSRF.
        return
    for ip_str in addrs:
        try:
            ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(
                f"refusing to fetch {what}: host {hostname!r} resolves to the "
                f"non-public address {ip_str} (loopback / private / link-local / "
                "cloud-metadata / reserved). This is a default-deny SSRF guard; "
                "set HOTATO_ALLOW_PRIVATE_URLS=1 only if you intend to reach an "
                "internal host."
            )


def _validate_download_url(url: str) -> str:
    """Every download URL here (``stereoUrl`` / ``recording_url`` /
    ``recording_multi_channel_url`` ...) is taken VERBATIM from the vendor's JSON
    RESPONSE -- untrusted data. ``urllib`` will happily open ``file://`` (arbitrary
    local-file read, e.g. ~/.hotato/connections.json holding every stack's
    credentials) or reach an internal/metadata endpoint over http, so a compromised
    account, tampered metadata, or a redirect could turn a "download the recording"
    step into local-file exfiltration or SSRF. Restrict it to an http(s) URL with a
    host before it is ever fetched. ``HOTATO_INGEST_ALLOWED_HOSTS`` (shared with
    ingest) is the operator lever to also pin the allowed download hosts."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_DOWNLOAD_SCHEMES:
        raise ValueError(
            f"refusing to download a recording from scheme {scheme or '(none)'!r}: "
            "the URL comes from the vendor's response, and only http(s) is allowed. "
            "file://, data:, ftp:// and similar are refused so a tampered or "
            "compromised response cannot read a local file or an internal endpoint."
        )
    if not parsed.hostname:
        raise ValueError(
            "refusing to download a recording from a URL with no host "
            "(from the vendor's response); only http(s) URLs with a host are fetched."
        )
    allow = os.environ.get("HOTATO_INGEST_ALLOWED_HOSTS", "").strip()
    if allow:
        hosts = {_canonical_host(h) for h in allow.split(",") if h.strip()}
        if _canonical_host(parsed.hostname) not in hosts:
            raise ValueError(
                f"recording download host {parsed.hostname!r} is not in "
                "HOTATO_INGEST_ALLOWED_HOSTS; refusing to fetch it."
            )
    # Default-deny SSRF: block a host that resolves to an internal / metadata IP.
    _reject_private_host(parsed.hostname, "a recording download")
    return url


def _download(
    url: str,
    dest: str,
    headers: Optional[dict] = None,
    timeout: int = 120,
    validate: Optional[Callable[[str], None]] = None,
) -> str:
    _validate_download_url(url)
    data = _http_get(
        url,
        headers=headers,
        timeout=timeout,
        max_bytes=_HTTP_RECORDING_RESPONSE_MAX_BYTES,
    )
    # Atomic local write: a temp file in dest's OWN directory, then os.replace,
    # mirroring _atomic_write_text / cli._atomic_write_text / connections.save.
    # ``open(dest, "wb")`` truncates any pre-existing file the instant it opens,
    # so a local write failure AFTER a successful fetch (ENOSPC / quota /
    # permission race / kill mid-write) would clobber a previously-good file at
    # --out with a truncated mix. Writing to a sibling temp and renaming means
    # dest is only ever the old bytes or the complete new bytes -- never partial.
    #
    # Validate-before-publish: ``validate`` (e.g. the 2-channel check) runs on
    # the TEMP file BEFORE os.replace. A rejected download therefore never lands
    # at dest and never overwrites a pre-existing user file there -- the temp is
    # unlinked and the rejection propagates, so unvalidated/unscoreable audio is
    # deleted, not kept (audit: rejected provider audio must fail closed).
    d = os.path.dirname(os.path.abspath(dest)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".hotato-dl-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if validate is not None:
            validate(tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


_DEFAULT_ORIGIN_PORTS = {"http": 80, "https": 443}


def _normalized_origin(url: str):
    """Return ``(scheme, canonical host, effective port)`` or ``None``.

    Credential forwarding follows RFC origin boundaries.  Default ports are
    made explicit so ``https://example.test`` and
    ``https://example.test:443`` compare equal, while a different port or
    protocol never does.  Invalid ports fail closed.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname
        if not scheme or not host:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    effective_port = port if port is not None else _DEFAULT_ORIGIN_PORTS.get(scheme)
    if effective_port is None:
        return None
    return scheme, _canonical_host(host), effective_port


def _same_origin(url: str, base_url: str) -> bool:
    """True only when both URLs have the same normalized network origin."""
    left = _normalized_origin(url)
    return left is not None and left == _normalized_origin(base_url)


def _auth_headers_for(url: str, base_url: str, headers: Optional[dict]) -> Optional[dict]:
    """Return ``headers`` (the credential) ONLY when ``url`` is on the vendor's
    own origin (``base_url``). A ``recording_url`` comes from the vendor's JSON
    RESPONSE, not from something the operator typed, so if it points off-domain
    (a compromised account, tampered metadata, a redirect, or a mis-set
    ``--base-url``) attaching the API key would exfiltrate the credential to that
    host. Vendor download URLs are pre-signed and need no auth, so dropping the
    header when the host does not match keeps the download working while never
    sending the secret anywhere but the vendor's own API host."""
    if headers and _same_origin(url, base_url):
        return headers
    return None


def _out_wav(out_path: Optional[str], prefix: str) -> str:
    if out_path:
        return out_path
    return tempfile.NamedTemporaryFile(
        prefix=prefix, suffix=".captured.wav", delete=False
    ).name


# --- channel validation + the mono policy ----------------------------------

_MONO_WHY = (
    "a mono recording mixes caller and agent into one signal, so talk-over "
    "cannot be attributed to either party; separated scoring needs one party "
    "per channel"
)


def _wav_channels(path: str) -> Optional[int]:
    """Channel count of a PCM WAV, or None if the file is not readable as WAV."""
    import struct
    import wave

    try:
        with _wav_read(path) as wf:
            return wf.getnchannels()
    except (wave.Error, EOFError, OSError, RuntimeError, struct.error):
        # RuntimeError: stdlib ``wave`` raises it for a well-formed RIFF/WAVE
        # header with a malformed/oversized inner sub-chunk; treat as unreadable.
        # struct.error: a truncated/garbage header can fault inside ``wave``'s own
        # ``struct.unpack`` before it raises wave.Error; that is still "unreadable",
        # not a traceback the adapter should leak.
        return None


def _require_two_channels(path: str, source: str) -> None:
    ch = _wav_channels(path)
    if ch is None:
        raise ValueError(
            f"downloaded file from {source} is not a readable PCM WAV; the scorer "
            "reads 2-channel PCM WAV (one party per channel)."
        )
    if ch != 2:
        raise ValueError(
            f"downloaded WAV from {source} has {ch} channel(s), expected 2. "
            f"Scoring needs a 2-channel file: {_MONO_WHY}."
        )


def _env_allow_mono() -> bool:
    return os.environ.get("HOTATO_ALLOW_MONO", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# --- Vapi (flagship): GET /call/{id} -> artifact.recording.stereoUrl -------

def capture_vapi(
    *,
    call_id: str,
    api_key: str,
    out_path: Optional[str] = None,
    base_url: str = "https://api.vapi.ai",
    timeout: int = 60,
) -> str:
    """Download a Vapi call's TWO-CHANNEL recording and return the local WAV path.

    API basis (verified against docs.vapi.ai, 2026-07-06):
      ``GET {base_url}/call/{id}`` with ``Authorization: Bearer <private key>``
      -> the Call object. Since the 2025-04-29 API update, recordings live on
      ``artifact.recording``; the stereo (2-channel) file is
      ``artifact.recording.stereoUrl`` (customer on channel 0, assistant on
      channel 1). The older ``artifact.stereoRecordingUrl`` and top-level
      ``call.stereoRecordingUrl`` are deprecated; we still fall back to them so
      captures keep working against older payloads. No SDK required; the only
      egress is Vapi -> your machine.

    Live-verification is on your side: recording must be enabled and the call must
    have ENDED for the stereo artifact to exist.
    """
    call = _require_json_object(
        _http_get_json(
            f"{base_url.rstrip('/')}/call/{call_id}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        ),
        f"Vapi call {call_id!r}",
    )
    # artifact / recording are documented objects; a wrong-typed vendor response
    # (a string/list where a dict is expected) must be a clean usage error, not a
    # raw AttributeError on the next ``.get``.
    artifact = _require_json_object(
        call.get("artifact") or {}, f"artifact on Vapi call {call_id!r}"
    )
    recording = _require_json_object(
        artifact.get("recording") or {},
        f"artifact.recording on Vapi call {call_id!r}",
    )
    # Defensive: some payload variants nest a {"url": ...} dict under
    # recording.stereo; read it only when it is actually a dict.
    stereo_obj = recording.get("stereo")
    stereo_obj_url = stereo_obj.get("url") if isinstance(stereo_obj, dict) else None
    # Current shape first, then defensive variants, then the two deprecated
    # legacy shapes.
    url = (
        recording.get("stereoUrl")
        or recording.get("stereoRecordingUrl")
        or stereo_obj_url
        or artifact.get("stereoRecordingUrl")
        or call.get("stereoRecordingUrl")
    )
    if not url:
        raise ValueError(
            "no stereo recording on this call: looked for "
            "artifact.recording.stereoUrl (current), then the deprecated "
            "artifact.stereoRecordingUrl and call.stereoRecordingUrl. Ensure "
            "recording is enabled and the call has ended; a stereo (2-channel) "
            f"artifact is what Hotato needs ({_MONO_WHY})."
        )
    url = _require_url_str(url, "Vapi stereo recording URL (artifact.recording.stereoUrl)")
    dest = _out_wav(out_path, "hotato-vapi-")

    def _validate_vapi(tmp: str) -> None:
        # Validate on the temp download BEFORE it is published to dest, so a
        # rejected file is deleted and never lands at --out. Fail closed at the
        # capture boundary exactly like retell/twilio: reject the
        # unreadable/corrupt/non-audio (ch is None) case too, not just ch != 2 --
        # the old check let a non-WAV download (e.g. an HTML error body served at
        # the stereo URL) pass silently and returned it as a "valid" recording.
        _require_two_channels(tmp, "Vapi (artifact.recording.stereoUrl)")

    _download(url, dest, timeout=max(timeout, 120), validate=_validate_vapi)
    return dest


# --- Retell: GET /v2/get-call/{id} -> *_multi_channel_url -------------------

def capture_retell(
    *,
    call_id: str,
    api_key: str,
    out_path: Optional[str] = None,
    base_url: str = "https://api.retellai.com",
    timeout: int = 60,
    allow_mono: bool = False,
) -> str:
    """Download a Retell call's TWO-CHANNEL recording and return the local WAV path.

    API basis (verified against docs.retellai.com/api-references/get-call,
    2026-07-06):
      ``GET {base_url}/v2/get-call/{call_id}`` with
      ``Authorization: Bearer <RETELL_API_KEY>`` -> the call object, which
      carries per-party recordings once the call has ended:
        * ``scrubbed_recording_multi_channel_url`` -- each party on its own
          channel, PII scrubbed (preferred),
        * ``recording_multi_channel_url`` -- each party on its own channel,
        * ``recording_url`` -- the plain mono mix.

    We prefer the scrubbed multi-channel file, fall back to the unscrubbed one,
    and validate the download has exactly 2 channels. The plain mono
    ``recording_url`` is rejected unless ``allow_mono=True``: a mono mix cannot
    attribute talk-over to caller vs agent.
    """
    call = _require_json_object(
        _http_get_json(
            f"{base_url.rstrip('/')}/v2/get-call/{call_id}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        ),
        f"Retell call {call_id!r}",
    )
    url = call.get("scrubbed_recording_multi_channel_url") or call.get(
        "recording_multi_channel_url"
    )
    if url:
        url = _require_url_str(url, "Retell recording_multi_channel_url")
        dest = _out_wav(out_path, "hotato-retell-")
        # Validate 2 channels on the temp download BEFORE publishing to dest: a
        # mono/mislabelled multi-channel URL is rejected and deleted, never kept.
        _download(
            url, dest, timeout=max(timeout, 120),
            validate=lambda tmp: _require_two_channels(
                tmp, "Retell (multi-channel recording)"
            ),
        )
        return dest
    mono_url = call.get("recording_url")
    if mono_url:
        mono_url = _require_url_str(mono_url, "Retell recording_url")
        if not allow_mono:
            raise ValueError(
                "this Retell call only exposes the mono recording_url; no "
                "recording_multi_channel_url / scrubbed_recording_multi_channel_url "
                f"is present yet. Hotato will not score it silently: {_MONO_WHY}. "
                "Multi-channel URLs appear after the call ends, on accounts with "
                "call recording enabled. To score the mono mix anyway (degraded, "
                "indicative only), pass --allow-mono (adapter) or set "
                "HOTATO_ALLOW_MONO=1 (hotato capture)."
            )
        sys.stderr.write(
            "[retell] degraded: downloading the MONO recording_url on your "
            f"explicit --allow-mono; {_MONO_WHY}. Treat results as indicative "
            "only.\n"
        )
        dest = _out_wav(out_path, "hotato-retell-")
        return _download(mono_url, dest, timeout=max(timeout, 120))
    raise ValueError(
        "no recording on this Retell call (no scrubbed_recording_multi_channel_url, "
        "recording_multi_channel_url, or recording_url). Recordings are available "
        "after the call ends, on agents with call recording enabled."
    )


# --- Twilio: dual-channel recording media ---------------------------------

def capture_twilio(
    *,
    recording_sid: str,
    account_sid: str,
    auth_token: str,
    out_path: Optional[str] = None,
    base_url: str = "https://api.twilio.com",
    timeout: int = 60,
    allow_mono: bool = False,
) -> str:
    """Download a Twilio DUAL-CHANNEL recording as WAV and return the local path.

    API basis (verified against twilio.com/docs/voice/api/recording, 2026-07-06):
      ``GET {base}/2010-04-01/Accounts/{AccountSid}/Recordings/{RecordingSid}
      .wav?RequestedChannels=2`` with HTTP Basic auth (AccountSid:AuthToken).
      Appending ``?RequestedChannels=2`` to the media URL is the documented way
      to request the dual-channel file. When the dual-channel format is not
      available, Twilio returns ``400 Bad Request``; the documented fallback is
      to re-request with ``RequestedChannels=1`` (mono), which we do only on
      your explicit ``allow_mono=True``. We validate the download has exactly
      2 channels.

    Channel order (per Twilio's dual-channel recording docs): for a two-party
    call the first (left) channel is the customer/caller and the second (right)
    channel is the agent -- Hotato's default of caller on channel 0, agent on
    channel 1 matches. Conference recordings differ (first channel = the first
    participant to join, second channel = everyone else); if caller and agent
    look swapped, pass different --caller-channel/--agent-channel.

    Record dual-channel when the recording is CREATED (``RecordingChannels=dual``
    on the REST API / ``<Dial record="record-from-answer-dual">`` /
    ``<Record recordingChannels="dual">``) so a 2-channel file exists to fetch.
    """
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    auth = {"Authorization": f"Basic {token}"}
    media_base = (
        f"{base_url.rstrip('/')}/2010-04-01/Accounts/{account_sid}"
        f"/Recordings/{recording_sid}.wav"
    )
    dest = _out_wav(out_path, "hotato-twilio-")
    try:
        # Validate 2 channels on the temp download BEFORE publishing to dest, so
        # a 200-OK-but-mono media response is rejected and deleted, never kept.
        _download(
            media_base + "?RequestedChannels=2", dest, headers=auth,
            timeout=max(timeout, 120),
            validate=lambda tmp: _require_two_channels(
                tmp, "Twilio (RequestedChannels=2 media)"
            ),
        )
    except _HTTPStatusError as exc:
        if exc.code != 400:
            raise
        if not allow_mono:
            raise ValueError(
                f"Twilio returned 400 for recording {recording_sid} with "
                "RequestedChannels=2: the dual-channel format is not available, "
                f"so this recording is a mono mix and {_MONO_WHY}. Re-record "
                "with RecordingChannels=dual (REST) / "
                '<Dial record="record-from-answer-dual"> / '
                '<Record recordingChannels="dual"> (TwiML) for a valid score. '
                "To score the mono mix anyway (degraded, indicative only), pass "
                "--allow-mono (adapter) or set HOTATO_ALLOW_MONO=1 (hotato "
                "capture)."
            ) from exc
        sys.stderr.write(
            "[twilio] degraded: dual-channel media unavailable (HTTP 400); "
            f"downloading the MONO mix on your explicit --allow-mono; {_MONO_WHY}. "
            "Treat results as indicative only.\n"
        )
        return _download(
            media_base + "?RequestedChannels=1", dest, headers=auth,
            timeout=max(timeout, 120),
        )
    # The 2-channel download was already validated on its temp file inside
    # _download (validate-before-publish); dest holds a verified 2-channel WAV.
    return dest


# --- dispatcher used by the adapters + CLI --------------------------------

def capture(stack: str, **kwargs) -> str:
    """Fetch/produce a two-channel WAV for ``stack`` and return its local path.

    Only the HTTP-fetch stacks (vapi, retell, twilio) capture directly here.
    LiveKit and Pipecat live capture run inside your infra (see
    ``setup``/``adapters``); for those, score the file your infra produced with
    ``score()`` / ``score_two_channel()``.
    """
    stack = stack.strip().lower()
    if stack == "vapi":
        return capture_vapi(**kwargs)
    if stack == "retell":
        return capture_retell(**kwargs)
    if stack == "twilio":
        return capture_twilio(**kwargs)
    raise ValueError(
        f"stack {stack!r} has no direct fetch. Run `hotato setup --stack {stack}` "
        "for the recording scaffold, then score the resulting WAV with "
        "`hotato capture --stack {stack} --stereo FILE`."
    )


# --- setup scaffolds (copy-paste recording config per runtime) ------------

_LIVEKIT_EGRESS_TEMPLATE = '''\
LiveKit -- capture each participant's audio on its OWN track via Egress, then
score the two tracks. RoomComposite mixes both parties into one channel and
cannot attribute overlap, so use TWO audio-only Track egresses (one per party).

    # Python (livekit-api): one audio-only Track egress per participant.
    from livekit import api

    lkapi = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

    async def egress_track(track_sid, out_name):
        req = api.TrackEgressRequest(
            room_name="my-room",
            track_id=track_sid,                       # the participant's AUDIO track sid
            file=api.DirectFileOutput(
                filepath=out_name,                    # + your s3=/gcp=/azure= storage
            ),
        )
        return await lkapi.egress.start_track_egress(req)

    await egress_track(CALLER_AUDIO_TRACK_SID, "caller.ogg")   # the human/customer
    await egress_track(AGENT_AUDIO_TRACK_SID,  "agent.ogg")    # your agent

    # Convert to PCM WAV (the scorer reads WAV) and score the two mono tracks:
    #   ffmpeg -i caller.ogg caller.wav ; ffmpeg -i agent.ogg agent.wav
    #   hotato capture --stack livekit --caller caller.wav --agent agent.wav --onset 42.18 --expect yield

What you are testing (current Agents API, verified docs.livekit.io/agents/logic/
turns/, 2026-07-06): turn taking is configured on
AgentSession(turn_handling=TurnHandlingOptions(...)) --
    turn_detection   = inference.TurnDetector() | "realtime_llm" | "vad" | "stt" | "manual"
    endpointing      = {"min_delay": ..., "max_delay": ...}
    interruption     = {"enabled": True, "mode": "adaptive" | "vad",
                        "min_duration": ..., "min_words": ...,
                        "false_interruption_timeout": ...,
                        "resume_false_interruption": ...}
(The older flat AgentSession kwargs allow_interruptions / min_interruption_duration /
min_interruption_words / min_endpointing_delay / max_endpointing_delay are not in
the current docs; use the turn_handling options above.)

Notes: this is real infra on your side (needs LIVEKIT_URL + API key/secret). The
Egress API evolves -- verify TrackEgressRequest / DirectFileOutput against your
LiveKit server version. audio_only keeps the files small. You can also enable
automatic egress at room creation. adapters/livekit_capture.py has an inline
AgentSession live-capture template with the three ADJUST points.
'''

_PIPECAT_PROCESSOR_TEMPLATE = '''\
Pipecat -- record caller + agent as a 2-channel WAV in-pipeline with a
2-channel AudioBufferProcessor, then score it. Channel 0 = user/caller (input),
channel 1 = bot/agent (output).

    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
    from hotato._engine.audio import write_wav   # stdlib WAV writer the scorer reads

    # num_channels=2 keeps the two parties on SEPARATE channels (do not mix down).
    audiobuffer = AudioBufferProcessor(sample_rate=16000, num_channels=2)

    pipeline = Pipeline([
        transport.input(),
        stt, llm, tts,          # <- your turn-taking config UNDER TEST
        transport.output(),
        audiobuffer,            # <- taps both directions
    ])

    # The knobs under test live on PipelineTask's user-turn strategies
    # (current API, verified docs.pipecat.ai, 2026-07-06). Start strategies:
    # VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy,
    # MinWordsUserTurnStartStrategy(min_words=...),
    # KrispVivaIPUserTurnStartStrategy(...)  # model-based, backchannel-aware
    # Stop strategies: SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...),
    # TurnAnalyzerUserTurnStopStrategy(turn_analyzer=...).
    # (MinWordsInterruptionStrategy is deprecated since pipecat 0.0.99; use
    # MinWordsUserTurnStartStrategy via turn_start_strategies instead.)

    caller_ch, agent_ch = [], []

    @audiobuffer.event_handler("on_audio_data")
    async def _on_audio(buf, pcm, sample_rate, num_channels):
        # pcm is interleaved int16 [caller, agent]; split into two float channels.
        import array
        frames = array.array("h"); frames.frombytes(pcm)
        caller_ch.extend(frames[0::2][i] / 32768.0 for i in range(len(frames) // 2))
        agent_ch.extend(frames[1::2][i] / 32768.0 for i in range(len(frames) // 2))

    # When the session ends, write the 2-channel WAV and score it:
    #   write_wav("captured.wav", 16000, [caller_ch, agent_ch])
    #   hotato capture --stack pipecat --stereo captured.wav --expect yield

A ready-to-copy version lives in adapters/pipecat_capture.py. Pipecat's frame /
transport APIs move -- verify AudioBufferProcessor against your installed version.
'''

_VAPI_SETUP_TEMPLATE = '''\
Vapi -- a stereo (2-channel) recording is produced for recorded calls; you only
need the call id + your private API key. Near-zero friction: no SDK, no export step.

    1. Enable recording on your assistant/call (the stereo artifact separates
       customer on channel 0 and assistant on channel 1).
    2. After the call ENDS, grab its call id (dashboard, or the end-of-call webhook).
    3. Score it:
         export VAPI_API_KEY=YOUR_API_KEY
         hotato capture --stack vapi --call-id CALL_ID --expect yield

Under the hood: GET https://api.vapi.ai/call/<id> -> artifact.recording.stereoUrl
(a 2-channel WAV; the current field since Vapi's 2025-04-29 API update, with
fallback to the deprecated artifact.stereoRecordingUrl / call.stereoRecordingUrl)
-> scored offline. The only network egress is the direct download from Vapi to
your machine; your audio is never sent anywhere else.
API basis verified against docs.vapi.ai, 2026-07-06.
'''

_TWILIO_SETUP_TEMPLATE = '''\
Twilio -- record DUAL-CHANNEL so caller and agent land on separate channels.

    1. Request dual-channel when the recording is CREATED:
         <Record recordingChannels="dual" .../>          (TwiML)
         <Dial record="record-from-answer-dual">         (TwiML Dial)
         RecordingChannels=dual                          (REST create-recording)
    2. After the recording completes, grab its Recording SID (RE...).
    3. Score it:
         export TWILIO_ACCOUNT_SID=AC...  TWILIO_AUTH_TOKEN=...
         hotato capture --stack twilio --recording-sid RE... --expect yield

Under the hood: GET .../Accounts/<sid>/Recordings/<RE...>.wav?RequestedChannels=2
(HTTP Basic auth; appending ?RequestedChannels=2 is the documented way to request
the dual-channel file) -> a 2-channel WAV, validated and scored offline. When the
dual-channel format is not available Twilio returns 400 Bad Request; Hotato then
stops with a clear message (the recording is mono and cannot attribute talk-over)
unless you opt into the degraded mono path with --allow-mono / HOTATO_ALLOW_MONO=1.

Channel order (two-party calls): first/left channel = customer/caller, second/
right channel = agent -- Hotato's default caller=ch0, agent=ch1 matches. In
CONFERENCE recordings the first channel is the first participant to join; if
caller/agent look swapped, add --caller-channel / --agent-channel.
API basis verified against twilio.com/docs/voice/api/recording, 2026-07-06.
'''

_RETELL_SETUP_TEMPLATE = '''\
Retell -- multi-channel recording export is built in; you only need the call id
plus your API key. No SDK, no export step.

    1. Enable call recording on your agent. Retell then exposes per-party
       recordings on the call object after the call ends.
    2. Grab the call id (dashboard, or the call webhook payload).
    3. Score it:
         export RETELL_API_KEY=YOUR_API_KEY
         hotato capture --stack retell --call-id CALL_ID --expect yield

Under the hood: GET https://api.retellai.com/v2/get-call/<call-id> (Bearer auth)
-> scrubbed_recording_multi_channel_url (PII scrubbed, preferred) or
recording_multi_channel_url (each party on its own channel) -> a 2-channel WAV,
validated and scored offline. The only network egress is the direct download
from Retell to your machine.

The plain recording_url is a mono mix: it cannot attribute talk-over to caller
vs agent, so Hotato rejects it by default. To score it anyway (degraded,
indicative only) pass --allow-mono (adapter) or set HOTATO_ALLOW_MONO=1.
API basis verified against docs.retellai.com/api-references/get-call, 2026-07-06.
'''

_SETUP = {
    "vapi": _VAPI_SETUP_TEMPLATE,
    "twilio": _TWILIO_SETUP_TEMPLATE,
    "livekit": _LIVEKIT_EGRESS_TEMPLATE,
    "pipecat": _PIPECAT_PROCESSOR_TEMPLATE,
    "retell": _RETELL_SETUP_TEMPLATE,
}


def setup_text(stack: str) -> str:
    """Return the copy-paste recording scaffold for ``stack``."""
    stack = stack.strip().lower()
    if stack not in _SETUP:
        raise ValueError(f"unknown stack {stack!r}; choose one of {', '.join(STACKS)}")
    return _SETUP[stack]


# --- orchestration the CLI calls ------------------------------------------

def run_setup(stack: str) -> int:
    text = setup_text(stack)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def run_capture(
    stack: str,
    *,
    demo: bool = False,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    onset: Optional[float] = None,
    expect: str = "yield",
    caller_channel: int = 0,
    agent_channel: int = 1,
    call_id: Optional[str] = None,
    api_key: Optional[str] = None,
    recording_sid: Optional[str] = None,
    account_sid: Optional[str] = None,
    auth_token: Optional[str] = None,
    allow_mono: bool = False,
    out: Optional[str] = None,
    fmt: str = "text",
) -> int:
    """Resolve a two-channel recording for ``stack`` and print its scored verdict.

    Resolution order: --demo, then an already-captured file (--stereo, or
    --caller/--agent), then a live per-stack fetch (vapi/retell/twilio). LiveKit
    and Pipecat have no direct fetch here (see ``setup``); pass the file your
    infra produced via --stereo / --caller+--agent.

    ``allow_mono`` (or env HOTATO_ALLOW_MONO=1) opts into the degraded mono path
    on retell/twilio when no 2-channel media exists; default is a clean rejection.
    """
    stack = (stack or "").strip().lower()
    if stack not in CAPTURE_STACKS:
        raise ValueError(
            f"unknown stack {stack!r}; choose one of {', '.join(CAPTURE_STACKS)}"
        )

    if demo:
        return _demo(stack, fmt)

    if stereo:
        env = score(
            stereo, stack=stack, onset_sec=onset, expect=expect,
            caller_channel=caller_channel, agent_channel=agent_channel,
        )
        return report(env, fmt)

    if caller and agent:
        env = score_two_channel(caller, agent, stack=stack, onset_sec=onset, expect=expect)
        return report(env, fmt)

    allow_mono = allow_mono or _env_allow_mono()

    if stack == "vapi":
        if not call_id:
            raise ValueError(
                "vapi capture needs --call-id (of an ended, recorded call), plus "
                "--api-key or VAPI_API_KEY. Try `hotato setup --stack vapi`, or "
                "`hotato capture --stack vapi --demo`, or score an existing "
                "2-channel WAV with --stereo."
            )
        key = api_key or os.environ.get("VAPI_API_KEY")
        if not key:
            raise ValueError(
                "vapi capture needs your private API key: pass --api-key or set "
                "VAPI_API_KEY."
            )
        path = capture_vapi(call_id=call_id, api_key=key, out_path=out)
        sys.stderr.write(f"[vapi] downloaded stereo recording -> {path}\n")
        env = score(
            path, stack="vapi", onset_sec=onset, expect=expect,
            caller_channel=caller_channel, agent_channel=agent_channel,
        )
        return report(env, fmt)

    if stack == "retell":
        if not call_id:
            raise ValueError(
                "retell capture needs --call-id (of an ended, recorded call), plus "
                "--api-key or RETELL_API_KEY. Try `hotato setup --stack retell`, or "
                "`hotato capture --stack retell --demo`, or score an existing "
                "2-channel WAV with --stereo."
            )
        key = api_key or os.environ.get("RETELL_API_KEY")
        if not key:
            raise ValueError(
                "retell capture needs your API key: pass --api-key or set "
                "RETELL_API_KEY."
            )
        path = capture_retell(
            call_id=call_id, api_key=key, out_path=out, allow_mono=allow_mono
        )
        sys.stderr.write(f"[retell] downloaded recording -> {path}\n")
        env = _score_capture(
            "retell", path, onset=onset, expect=expect,
            caller_channel=caller_channel, agent_channel=agent_channel,
        )
        return report(env, fmt)

    if stack == "twilio":
        if not recording_sid:
            raise ValueError(
                "twilio capture needs --recording-sid (RE...) of a DUAL-CHANNEL "
                "recording, plus --account-sid/--auth-token or TWILIO_ACCOUNT_SID/"
                "TWILIO_AUTH_TOKEN. Try `hotato setup --stack twilio`, or "
                "`hotato capture --stack twilio --demo`."
            )
        sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID")
        tok = auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
        if not (sid and tok):
            raise ValueError(
                "twilio capture needs credentials: pass --account-sid/--auth-token "
                "or set TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN."
            )
        path = capture_twilio(
            recording_sid=recording_sid, account_sid=sid, auth_token=tok,
            out_path=out, allow_mono=allow_mono,
        )
        sys.stderr.write(f"[twilio] downloaded recording -> {path}\n")
        env = _score_capture(
            "twilio", path, onset=onset, expect=expect,
            caller_channel=caller_channel, agent_channel=agent_channel,
        )
        return report(env, fmt)

    if stack in MONO_STACKS:
        ident = call_id or recording_sid
        if not ident:
            raise ValueError(
                f"{stack} capture needs --call-id (the recording id from "
                f"`hotato pull --stack {stack}` or your {stack} dashboard), plus "
                f"--api-key or {CONNECT_SPECS[stack]['env']['api_key']}. {stack} "
                "recordings are mono/mixed, so scoring is degraded and needs "
                "--allow-mono."
            )
        key = api_key or os.environ.get(CONNECT_SPECS[stack]["env"]["api_key"])
        if not key:
            raise ValueError(
                f"{stack} capture needs your API key: pass --api-key or set "
                f"{CONNECT_SPECS[stack]['env']['api_key']}."
            )
        if not allow_mono:
            raise ValueError(
                f"{stack} exposes only a mono/mixed recording; {_MONO_WHY}. To "
                "score it anyway (degraded, indicative only) pass --allow-mono "
                "or set HOTATO_ALLOW_MONO=1."
            )
        path = fetch_one(stack, ident, {"api_key": key}, out, allow_mono=True)
        sys.stderr.write(f"[{stack}] downloaded recording -> {path}\n")
        env = _score_capture(
            stack, path, onset=onset, expect=expect,
            caller_channel=caller_channel, agent_channel=agent_channel,
        )
        return report(env, fmt)

    # livekit / pipecat: no direct fetch -- point at setup + the file path.
    hint = {
        "livekit": (
            "LiveKit capture runs in YOUR deployment via egress. Run "
            "`hotato setup --stack livekit` for the two-track egress scaffold, then "
            "score the tracks:\n"
            "  hotato capture --stack livekit --caller caller.wav --agent agent.wav"
        ),
        "pipecat": (
            "Pipecat capture runs INSIDE your pipeline via a 2-channel "
            "AudioBufferProcessor. Run `hotato setup --stack pipecat` for the "
            "drop-in processor, then score the WAV it writes:\n"
            "  hotato capture --stack pipecat --stereo captured.wav"
        ),
    }[stack]
    raise ValueError(hint)


def _score_capture(
    stack: str,
    path: str,
    *,
    onset: Optional[float],
    expect: str,
    caller_channel: int,
    agent_channel: int,
) -> dict:
    """Score a fetched capture, honestly gated on the STACK's verified channel
    status -- not merely on how many channels the download happens to have.

    A confident dual-channel verdict is produced ONLY for stacks whose separated
    (2-channel) recording the integration spec confirms verbatim
    (``DUAL_PULL_STACKS`` = vapi / twilio / retell). For every other stack --
    the spec-confirmed mono ones AND cartesia, whose channel order the spec
    marks [unclear] and never verified -- the download is scored degraded /
    indicative only, even if it happens to arrive with 2 channels, because we
    cannot trust which channel carries which party. A mono file from ANY stack
    is likewise degraded. In every degraded case a single channel stands in for
    both parties, so talk-over is not attributable, and we say so loudly."""
    nch = _wav_channels(path)
    trusted_dual = stack in DUAL_PULL_STACKS and nch == 2
    if not trusted_dual:
        if nch == 1:
            why = "mono file"
        else:
            why = (f"{nch}-channel file, but this stack's channel separation is "
                   "not spec-verified")
        sys.stderr.write(
            f"[{stack}] degraded: {why}, scoring WITHOUT party attribution (a "
            "single channel stands in for both parties). Treat results as "
            "indicative only.\n"
        )
        return score_two_channel(path, path, stack=stack, onset_sec=onset, expect=expect)
    return score(
        path, stack=stack, onset_sec=onset, expect=expect,
        caller_channel=caller_channel, agent_channel=agent_channel,
    )


# internal alias so run_capture(demo=True) doesn't shadow the public demo()
_demo = demo


# ==========================================================================
# connect -> pull -> sweep: list recent calls, bulk-fetch, then analyze.
#
# Every list/fetch endpoint below is used EXACTLY as verified verbatim in
# hotato-launch/INTEGRATION-SPEC-2026-07-07.md. Where the spec marks a
# list-calls endpoint unconfirmed/none (Retell) or a platform as
# capture-in-your-infra (LiveKit, Pipecat) or webhook-push-only (Regal), no
# endpoint is fabricated -- the honest fallback + limitation is documented in
# docs/ADAPTER-STATUS.md and surfaced as a clean error here.
#
# Platform payloads are DATA, never instructions: parsing only reads documented
# id / URL / timestamp fields and downloads the vendor's own recording URL. A
# malformed payload raises a clean ValueError (CLI exit 2); nothing in a payload
# is ever executed or acted on beyond fetching the recording it points to.
# ==========================================================================

def _iso(epoch: float) -> str:
    """UTC epoch seconds -> an ISO8601 'Z' timestamp (Vapi createdAtGt)."""
    import datetime

    return datetime.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ymd(epoch: float) -> str:
    """UTC epoch seconds -> YYYY-MM-DD (Twilio DateCreated filter)."""
    import datetime

    return datetime.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d")


def since_epoch(spec: Optional[str]) -> Optional[float]:
    """Parse a ``--since`` window (e.g. ``7d``, ``12h``, ``30m``, ``2w``) into an
    absolute UTC epoch-seconds cutoff, or ``None`` when unset. Raises a clean
    ValueError on a malformed value."""
    if not spec:
        return None
    import re
    import time

    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", spec.lower())
    if not m:
        raise ValueError(
            f"--since {spec!r} is not a duration; use e.g. 7d, 12h, 30m, 2w "
            "(s=seconds, m=minutes, h=hours, d=days, w=weeks)."
        )
    n = int(m.group(1))
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return time.time() - n * mult


def _as_epoch(value) -> Optional[float]:
    """Best-effort parse of a vendor timestamp (unix seconds, unix millis, or an
    ISO8601 string) into epoch seconds; ``None`` when it cannot be read. Used
    only to sort/filter listings, so an unreadable value degrades to 'unknown'
    rather than failing the whole pull."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e12 else v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s) / (1000.0 if float(s) > 1e12 else 1.0)
        except ValueError:
            pass
        try:
            import datetime

            return datetime.datetime.fromisoformat(
                s.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None
    return None


def _safe_id(ident: str) -> str:
    """A filesystem-safe token for a call id, so the pull filename is stable and
    never escapes the output directory."""
    keep = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(ident))
    return keep[:120] or "call"


def _bad_payload(stack: str, what: str) -> "ValueError":
    return ValueError(
        f"{stack} list response did not contain the documented {what}; the "
        "payload shape does not match the verified API. Nothing was fetched."
    )


# --- per-platform LIST-RECENT-CALLS (verified endpoints only) --------------

def _list_vapi(creds, since, limit):
    # GET https://api.vapi.ai/call  (params: limit, createdAtGt) -> JSON array
    # of Call objects, each with `id` and `createdAt`. (spec: Vapi list_calls)
    from urllib.parse import urlencode

    params = {"limit": str(limit)}
    if since is not None:
        params["createdAtGt"] = _iso(since)
    arr = _http_get_json(
        f"https://api.vapi.ai/call?{urlencode(params)}",
        headers={"Authorization": f"Bearer {creds['api_key']}",
                 "Accept": "application/json"},
    )
    if not isinstance(arr, list):
        raise _bad_payload("vapi", "JSON array of Call objects")
    out = []
    for c in arr:
        if isinstance(c, dict) and c.get("id"):
            out.append({"id": str(c["id"]), "created": _as_epoch(c.get("createdAt"))})
    return out


def _list_twilio(creds, since, limit):
    # GET .../Accounts/{Sid}/Recordings.json  (PageSize, DateCreatedAfter) ->
    # {"recordings": [{"sid": "RE...", "date_created": ...}]}. (spec: Twilio list)
    from urllib.parse import urlencode

    token = base64.b64encode(
        f"{creds['account_sid']}:{creds['auth_token']}".encode()
    ).decode()
    params = {"PageSize": str(limit)}
    if since is not None:
        params["DateCreatedAfter"] = _ymd(since)
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{creds['account_sid']}"
        f"/Recordings.json?{urlencode(params)}"
    )
    data = _http_get_json(url, headers={"Authorization": f"Basic {token}"})
    recs = data.get("recordings") if isinstance(data, dict) else None
    if not isinstance(recs, list):
        raise _bad_payload("twilio", "recordings[] array")
    out = []
    for r in recs:
        if isinstance(r, dict) and r.get("sid"):
            out.append({"id": str(r["sid"]), "created": _as_epoch(r.get("date_created"))})
    return out


def _list_bland(creds, since, limit):
    # GET https://api.bland.ai/v1/calls -> {"calls": [{call_id, ...}]}.
    # (spec: Bland list_calls; no documented date filter, so cap + client-filter.)
    data = _http_get_json(
        "https://api.bland.ai/v1/calls",
        headers={"authorization": creds["api_key"]},
    )
    calls = data.get("calls") if isinstance(data, dict) else None
    if not isinstance(calls, list):
        raise _bad_payload("bland", "calls[] array")
    out = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        cid = c.get("call_id") or c.get("c_id")
        if cid:
            out.append({
                "id": str(cid),
                "created": _as_epoch(c.get("created_at") or c.get("started_at")),
            })
    return out


def _list_elevenlabs(creds, since, limit):
    # GET https://api.elevenlabs.io/v1/convai/conversations
    # (page_size, call_start_after_unix) -> {"conversations": [{conversation_id,
    # start_time_unix_secs}], has_more, next_cursor}. (spec: ElevenLabs list)
    from urllib.parse import urlencode

    params = {"page_size": str(min(limit, 100))}
    if since is not None:
        params["call_start_after_unix"] = str(int(since))
    data = _http_get_json(
        f"https://api.elevenlabs.io/v1/convai/conversations?{urlencode(params)}",
        headers={"xi-api-key": creds["api_key"]},
    )
    convs = data.get("conversations") if isinstance(data, dict) else None
    if not isinstance(convs, list):
        raise _bad_payload("elevenlabs", "conversations[] array")
    out = []
    for c in convs:
        if isinstance(c, dict) and c.get("conversation_id"):
            out.append({
                "id": str(c["conversation_id"]),
                "created": _as_epoch(c.get("start_time_unix_secs")),
            })
    return out


def _synthflow_body(data):
    """Navigate Synthflow's ``response.response`` envelope (the spec's verbatim
    field prefix), tolerating one or two ``response`` nestings."""
    body = data
    for _ in range(2):
        if isinstance(body, dict) and isinstance(body.get("response"), dict):
            body = body["response"]
    return body if isinstance(body, dict) else {}


def _list_synthflow(creds, since, limit):
    # GET https://api.synthflow.ai/v2/calls?model_id=&limit=&from_date= ->
    # response.response.calls[].call_id. (spec: Synthflow list_calls;
    # from_date is epoch millis; model_id is required.)
    from urllib.parse import urlencode

    model_id = creds.get("model_id")
    if not model_id:
        raise ValueError(
            "synthflow list needs a model_id (the verified list endpoint "
            "GET /v2/calls requires model_id): pass --model-id or set "
            "SYNTHFLOW_MODEL_ID, or pull explicit --call-id values."
        )
    params = {"model_id": model_id, "limit": str(limit)}
    if since is not None:
        params["from_date"] = str(int(since * 1000))
    data = _http_get_json(
        f"https://api.synthflow.ai/v2/calls?{urlencode(params)}",
        headers={"Authorization": f"Bearer {creds['api_key']}"},
    )
    calls = _synthflow_body(data).get("calls")
    if not isinstance(calls, list):
        raise _bad_payload("synthflow", "response.response.calls[] array")
    out = []
    for c in calls:
        if isinstance(c, dict) and c.get("call_id"):
            out.append({"id": str(c["call_id"]), "created": _as_epoch(c.get("start_time"))})
    return out


def _list_millis(creds, since, limit):
    # GET {base}/call-logs?limit= -> {"histories": [CallHistory], next_cursor}.
    # (spec: Millis list_calls; base default US region api-west.)
    from urllib.parse import urlencode

    base = creds.get("base_url") or "https://api-west.millis.ai"
    data = _http_get_json(
        f"{base.rstrip('/')}/call-logs?{urlencode({'limit': str(limit)})}",
        headers={"authorization": creds["api_key"]},
    )
    hist = data.get("histories") if isinstance(data, dict) else None
    if not isinstance(hist, list):
        raise _bad_payload("millis", "histories[] array")
    out = []
    for h in hist:
        if not isinstance(h, dict):
            continue
        sid = h.get("session_id") or h.get("call_id")
        if sid:
            out.append({"id": str(sid), "created": _as_epoch(h.get("ts"))})
    return out


def _list_cartesia(creds, since, limit):
    # GET https://api.cartesia.ai/agents/calls?agent_id=&limit= ->
    # {"data": [{id, start_time}], has_more, next_page}. (spec: Cartesia list;
    # agent_id required; requires the Cartesia-Version header.)
    from urllib.parse import urlencode

    agent_id = creds.get("agent_id")
    if not agent_id:
        raise ValueError(
            "cartesia list needs an agent_id (the verified list endpoint "
            "GET /agents/calls requires agent_id): pass --agent-id or set "
            "CARTESIA_AGENT_ID, or pull explicit --call-id values."
        )
    params = {"agent_id": agent_id, "limit": str(min(limit, 100))}
    data = _http_get_json(
        f"https://api.cartesia.ai/agents/calls?{urlencode(params)}",
        headers={"Authorization": f"Bearer {creds['api_key']}",
                 "Cartesia-Version": "2026-03-01"},
    )
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _bad_payload("cartesia", "data[] array")
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get("id"):
            out.append({"id": str(r["id"]), "created": _as_epoch(r.get("start_time"))})
    return out


_LIST_FUNCS = {
    "vapi": _list_vapi,
    "twilio": _list_twilio,
    "bland": _list_bland,
    "elevenlabs": _list_elevenlabs,
    "synthflow": _list_synthflow,
    "millis": _list_millis,
    "cartesia": _list_cartesia,
}


def list_calls(stack, creds, *, since=None, limit=50):
    """List up to ``limit`` recent recordings for ``stack`` using ONLY the
    spec-verified list endpoint. ``since`` is an epoch-seconds cutoff (see
    :func:`since_epoch`). Returns ``[{"id": str, "created": float|None}, ...]``
    most-recent first.

    Raises a clean ValueError for stacks the spec gives no list endpoint for
    (Retell) or that are capture-in-your-infra (LiveKit, Pipecat) -- those must
    be pulled from an explicit id list, never a fabricated endpoint."""
    stack = (stack or "").strip().lower()
    fn = _LIST_FUNCS.get(stack)
    if fn is None:
        if stack == "retell":
            raise ValueError(
                "Retell has no verified list-calls endpoint (the integration "
                "spec marks it unconfirmed), so Hotato will not guess one. Pull "
                "explicit ids instead: hotato pull --stack retell --call-id CALL_ID "
                "(repeat --call-id for more)."
            )
        if stack in ("livekit", "pipecat"):
            raise ValueError(
                f"{stack} is capture-in-your-infra: there is no vendor recording "
                "list to pull. Record with `hotato setup --stack "
                f"{stack}` and score the file your deployment writes."
            )
        raise ValueError(
            f"{stack!r} has no list-recent-calls support; connectable stacks "
            f"with a verified list endpoint are: {', '.join(LIST_STACKS)}."
        )
    items = fn(creds, since, max(1, int(limit)))
    # Newest first when the created time is known; unknown-time items keep their
    # server order but sort after timed ones. Then apply the since cutoff for
    # platforms where the server-side filter was not spec-confirmed.
    if since is not None:
        items = [it for it in items if it["created"] is None or it["created"] >= since]
    items.sort(key=lambda it: (it["created"] is not None, it["created"] or 0.0),
               reverse=True)
    return items[:limit]


# --- mono/mixed single-fetch adapters (spec-verified, --allow-mono only) ----

def capture_bland(*, call_id, api_key, out_path=None,
                  base_url="https://api.bland.ai", timeout=60):
    """Download a Bland call's MONO recording. (spec: GET /v1/calls/{id} ->
    recording_url; Bland audio has no documented per-party channel, so it is
    mono/mixed and only scorable behind --allow-mono.)"""
    call = _http_get_json(
        f"{base_url.rstrip('/')}/v1/calls/{call_id}",
        headers={"authorization": api_key, "Accept": "application/json"},
        timeout=timeout,
    )
    url = call.get("recording_url") if isinstance(call, dict) else None
    if not url:
        raise ValueError(
            f"no recording_url on Bland call {call_id!r} (only present when the "
            "call was created with record=true, after it ends)."
        )
    url = _require_url_str(url, "Bland recording_url")
    dest = _out_wav(out_path, "hotato-bland-")
    return _download(url, dest,
                     headers=_auth_headers_for(url, base_url, {"authorization": api_key}),
                     timeout=max(timeout, 120))


def capture_elevenlabs(*, conversation_id, api_key, out_path=None,
                       base_url="https://api.elevenlabs.io", timeout=60):
    """Download an ElevenLabs conversation's MONO audio. (spec: GET
    /v1/convai/conversations/{id}/audio returns the combined full-conversation
    audio with no separate caller/agent channels -> --allow-mono only.)"""
    dest = _out_wav(out_path, "hotato-elevenlabs-")
    return _download(
        f"{base_url.rstrip('/')}/v1/convai/conversations/{conversation_id}/audio",
        dest, headers={"xi-api-key": api_key}, timeout=max(timeout, 120),
    )


def capture_synthflow(*, call_id, api_key, out_path=None,
                      base_url="https://api.synthflow.ai", timeout=60):
    """Download a Synthflow call's MONO recording. (spec: GET /v2/calls/{id} ->
    response.response.calls[0].recording_url, a Twilio Recordings URL; Synthflow
    documents no dual-channel option -> --allow-mono only.)"""
    data = _http_get_json(
        f"{base_url.rstrip('/')}/v2/calls/{call_id}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=timeout,
    )
    calls = _synthflow_body(data).get("calls")
    url = None
    if isinstance(calls, list) and calls and isinstance(calls[0], dict):
        url = calls[0].get("recording_url")
    if not url:
        raise ValueError(
            f"no recording_url on Synthflow call {call_id!r} "
            "(response.response.calls[0].recording_url was empty)."
        )
    url = _require_url_str(url, "Synthflow recording_url")
    dest = _out_wav(out_path, "hotato-synthflow-")
    return _download(url, dest, timeout=max(timeout, 120))


def capture_millis(*, session_id, api_key, out_path=None,
                   base_url="https://api-west.millis.ai", timeout=60):
    """Download a Millis call's MONO recording. (spec: GET /call-logs/{id} ->
    recording.recording_url; Millis documents no channel-mode option -> mono,
    --allow-mono only.)"""
    call = _http_get_json(
        f"{base_url.rstrip('/')}/call-logs/{session_id}",
        headers={"authorization": api_key, "Accept": "application/json"},
        timeout=timeout,
    )
    rec = call.get("recording") if isinstance(call, dict) else None
    url = rec.get("recording_url") if isinstance(rec, dict) else None
    if not url:
        raise ValueError(
            f"no recording.recording_url on Millis session {session_id!r} "
            "(recording is only present when enable_recording was set)."
        )
    url = _require_url_str(url, "Millis recording.recording_url")
    dest = _out_wav(out_path, "hotato-millis-")
    return _download(url, dest,
                     headers=_auth_headers_for(url, base_url, {"authorization": api_key}),
                     timeout=max(timeout, 120))


def capture_cartesia(*, call_id, api_key, out_path=None,
                     base_url="https://api.cartesia.ai", timeout=60,
                     version="2026-03-01"):
    """Download a Cartesia call's audio. (spec: GET /agents/calls/{id}/audio
    returns audio/wav; the spec could NOT confirm whether it is dual-channel or
    mono/mixed, so Hotato treats it as mono and requires --allow-mono until a
    live channel-count check proves otherwise.)"""
    dest = _out_wav(out_path, "hotato-cartesia-")
    return _download(
        f"{base_url.rstrip('/')}/agents/calls/{call_id}/audio",
        dest,
        headers={"Authorization": f"Bearer {api_key}", "Cartesia-Version": version},
        timeout=max(timeout, 120),
    )


def fetch_one(stack, ident, creds, out_path=None, *, allow_mono=False):
    """Fetch ONE recording for ``stack`` by id/sid into ``out_path`` (or a temp
    file) and return the local WAV path. Reuses the existing single-call
    adapters; dual stacks validate 2 channels, mono stacks download the combined
    file for degraded scoring."""
    stack = (stack or "").strip().lower()
    if stack == "vapi":
        return capture_vapi(call_id=ident, api_key=creds["api_key"], out_path=out_path)
    if stack == "retell":
        return capture_retell(call_id=ident, api_key=creds["api_key"],
                              out_path=out_path, allow_mono=allow_mono)
    if stack == "twilio":
        return capture_twilio(recording_sid=ident, account_sid=creds["account_sid"],
                             auth_token=creds["auth_token"], out_path=out_path,
                             allow_mono=allow_mono)
    if stack == "bland":
        return capture_bland(call_id=ident, api_key=creds["api_key"], out_path=out_path)
    if stack == "elevenlabs":
        return capture_elevenlabs(conversation_id=ident, api_key=creds["api_key"],
                                 out_path=out_path)
    if stack == "synthflow":
        return capture_synthflow(call_id=ident, api_key=creds["api_key"], out_path=out_path)
    if stack == "millis":
        return capture_millis(session_id=ident, api_key=creds["api_key"],
                             out_path=out_path,
                             base_url=creds.get("base_url") or "https://api-west.millis.ai")
    if stack == "cartesia":
        return capture_cartesia(call_id=ident, api_key=creds["api_key"], out_path=out_path)
    raise ValueError(f"{stack!r} has no direct fetch adapter.")


# --- credential + stack resolution (flag > connections.json > env) ----------

def resolve_stack(stack: Optional[str]) -> str:
    """Resolve the stack for pull/sweep. Explicit ``--stack`` wins; otherwise, if
    exactly one stack is connected, use it; if several, ask which; if none, point
    at connect."""
    if stack:
        return stack.strip().lower()
    from . import connections

    conn = [s for s in connections.connected_stacks() if s in PULL_STACKS]
    if len(conn) == 1:
        return conn[0]
    if not conn:
        raise ValueError(
            "no --stack given and no stack is connected. Run `hotato connect "
            f"<stack>` first (one of: {', '.join(PULL_STACKS)}), or pass --stack."
        )
    raise ValueError(
        f"several stacks are connected ({', '.join(conn)}); pass --stack to pick "
        "one."
    )


def resolve_creds(stack: str, overrides: Optional[dict] = None) -> dict:
    """Resolve credentials for ``stack`` in order: explicit override (a CLI flag)
    > ~/.hotato/connections.json > environment variable. Raises a clean
    ValueError listing what is missing. Never logs any value."""
    from . import connections

    stack = stack.strip().lower()
    spec = CONNECT_SPECS.get(stack)
    if spec is None:
        raise ValueError(
            f"{stack!r} is not a connectable stack; connectable: "
            f"{', '.join(CONNECT_STACKS)}."
        )
    overrides = overrides or {}
    stored = connections.get(stack) or {}
    creds: dict = {}
    for field in list(spec["fields"]) + list(spec.get("optional", [])):
        val = (
            overrides.get(field)
            or stored.get(field)
            or os.environ.get(spec["env"].get(field, ""), None)
        )
        if val:
            creds[field] = val
    missing = [f for f in spec["fields"] if not creds.get(f)]
    if missing:
        hints = ", ".join(
            f"--{f.replace('_', '-')} / {spec['env'].get(f, '')}" for f in missing
        )
        raise ValueError(
            f"{stack} is missing credentials ({', '.join(missing)}). Provide "
            f"them ({hints}) or run `hotato connect {stack}`."
        )
    return creds


def auth_check(stack: str, creds: dict) -> None:
    """A lightweight credential probe: list one recent call. Raises the vendor's
    HTTP error (an _HTTPStatusError carrying .code) on an auth failure, or a
    ValueError when the stack has no cheap probe (e.g. Retell has no list
    endpoint; Synthflow/Cartesia need model_id/agent_id to list)."""
    stack = stack.strip().lower()
    if stack not in _LIST_FUNCS:
        raise ValueError(
            f"{stack} has no list endpoint to verify against; credentials will "
            "be validated on the first pull."
        )
    _LIST_FUNCS[stack](creds, None, 1)


# --- pull: bulk-fetch recent recordings into a local directory --------------

def pull(stack, creds, *, out_dir, ids=None, since=None, limit=50,
         allow_mono=False, log=None):
    """Bulk-fetch recent recordings for ``stack`` into ``out_dir`` by looping the
    existing single-call fetch over the list results (or an explicit ``ids``
    list). Returns a summary dict ``{stack, out_dir, listed, pulled[], skipped[]}``.

    Honest per-file behaviour: a recording that cannot be fetched (missing URL,
    HTTP error, wrong channel count) is recorded in ``skipped`` with its reason
    and the loop continues -- one bad call never aborts the pull or crashes.

    Mono/mixed stacks require ``allow_mono=True``; dual stacks fetch stereo and
    validate 2 channels."""
    stack = (stack or "").strip().lower()
    if stack not in PULL_STACKS:
        if stack in ("livekit", "pipecat"):
            raise ValueError(
                f"{stack} is capture-in-your-infra (no vendor recording list to "
                f"pull). Run `hotato setup --stack {stack}` and score the file "
                "your deployment writes."
            )
        raise ValueError(
            f"{stack!r} does not support pull. Pullable stacks: "
            f"{', '.join(PULL_STACKS)}."
        )
    mode = STACK_CHANNELS.get(stack)
    if mode == "mono" and not allow_mono:
        raise ValueError(
            f"{stack} exposes only a mono/mixed recording; {_MONO_WHY}. Separated "
            "turn-taking analysis is not possible from mono. Pass --allow-mono to "
            "pull it anyway (degraded, indicative only)."
        )
    limit = max(1, int(limit))
    os.makedirs(out_dir, exist_ok=True)

    if ids:
        items = [{"id": str(i), "created": None} for i in ids][:limit]
    else:
        cutoff = since_epoch(since) if isinstance(since, str) else since
        items = list_calls(stack, creds, since=cutoff, limit=limit)

    pulled, skipped = [], []
    for it in items:
        ident = it["id"]
        dest = os.path.join(out_dir, f"{stack}__{_safe_id(ident)}.wav")
        # Remember whether dest already existed so the failure backstop below
        # only ever removes a file THIS pull wrote -- never a pre-existing user
        # file that happens to share the deterministic name.
        preexisting = os.path.exists(dest)
        try:
            path = fetch_one(stack, ident, creds, dest, allow_mono=allow_mono)
            pulled.append({"id": ident, "path": path})
            if log:
                log(f"[{stack}] pulled {ident} -> {path}")
        except Exception as exc:
            # Backstop for validate-before-publish (the adapters delete rejected
            # downloads before they reach dest): if a skipped call nonetheless
            # left a file at dest that was NOT there before, remove it so a
            # skipped/unscorable call never leaves unvalidated audio in out_dir.
            if not preexisting and os.path.exists(dest):
                try:
                    os.unlink(dest)
                except OSError:
                    pass
            # pull()'s contract is "one bad call never aborts the pull": a single
            # unscorable/failed call is skipped honestly and the batch continues.
            # ValueError/_HTTPStatusError/OSError are the expected failures, but
            # ANY adapter (current or future) that raises something else must not
            # take down the whole run and every other id with it -- so catch
            # broadly and record the type in the reason for diagnosis.
            reason = _errors.sanitize_urls_in_text(str(exc)) or f"{type(exc).__name__}"
            if not isinstance(exc, (ValueError, OSError)):
                reason = f"{type(exc).__name__}: {reason}"
            skipped.append({"id": ident, "reason": reason})
            if log:
                log(f"[{stack}] skipped {ident}: {reason}")
    return {
        "stack": stack,
        "out_dir": out_dir,
        "listed": len(items),
        "pulled": pulled,
        "skipped": skipped,
    }


# --- CLI orchestration: connect / pull / sweep ------------------------------

def _overrides_from(api_key=None, account_sid=None, auth_token=None,
                    model_id=None, agent_id=None, base_url=None) -> dict:
    return {k: v for k, v in {
        "api_key": api_key, "account_sid": account_sid, "auth_token": auth_token,
        "model_id": model_id, "agent_id": agent_id, "base_url": base_url,
    }.items() if v}


def run_connect(stack, *, api_key=None, account_sid=None, auth_token=None,
                model_id=None, agent_id=None, base_url=None, no_verify=False,
                fmt="text") -> int:
    """`hotato connect <stack>`: capture credentials once, do a lightweight live
    auth-check (unless --no-verify), and store them in ~/.hotato/connections.json
    (mode 0600). The credentials are never printed and never sent anywhere but
    the vendor's own API."""
    from . import connections

    stack = (stack or "").strip().lower()
    if stack not in CONNECT_STACKS:
        raise ValueError(
            f"{stack!r} is not a connectable stack. Connectable (vendor-hosted "
            f"recordings): {', '.join(CONNECT_STACKS)}. LiveKit/Pipecat are "
            "capture-in-your-infra (use `hotato setup`)."
        )
    overrides = _overrides_from(api_key, account_sid, auth_token, model_id,
                                agent_id, base_url)
    creds = resolve_creds(stack, overrides)

    verified = None
    note = ""
    if not no_verify:
        try:
            auth_check(stack, creds)
            verified = True
        except _HTTPStatusError as exc:
            if exc.code in (401, 403):
                raise ValueError(
                    f"authentication failed for {stack} (HTTP {exc.code}); the "
                    "credentials were NOT stored. Check the key and try again."
                ) from exc
            verified = False
            note = f"auth check inconclusive (HTTP {exc.code}); stored anyway"
        except ValueError as exc:
            # No cheap probe (Retell, or Synthflow/Cartesia without model/agent
            # id): store and validate on first pull. Not a failure.
            verified = None
            note = _errors.sanitize_urls_in_text(str(exc))

    path = connections.save(stack, creds)
    fields = ", ".join(sorted(creds.keys()))
    if fmt == "json":
        print(_errors.safe_json_dumps({
            "tool": "hotato", "kind": "connect", "stack": stack,
            "stored_fields": sorted(creds.keys()), "path": path,
            "verified": verified, "note": note,
        }, indent=2))
        return 0
    print(f"connected {stack}: stored {fields} in {path} (mode 0600).")
    print("  credentials stay on this machine; they are sent only to "
          f"{stack}'s own API, never to Hotato.")
    if verified is True:
        print("  auth check: OK (listed one recent call).")
    elif note:
        print(f"  auth check: {note}")
    print(f"  next: hotato pull --stack {stack}   (or omit --stack if this is "
          "your only connection)")
    return 0


def _resolve_for_pull(stack, overrides):
    stack = resolve_stack(stack)
    creds = resolve_creds(stack, overrides)
    return stack, creds


def _score_pulled(res: dict, *, stack: str) -> dict:
    """Score every pulled dual-channel recording OFFLINE with the standard
    scorer (``core.run_single``, the same scoring `hotato run --stereo` does,
    expected behavior yield) and aggregate the run exit contract: exit 1 when
    a scorable event failed anywhere in the set, else 0.

    Refuses (ValueError -> the CLI's standard exit-2 error envelope) when the
    set holds no dual-channel recording to score: nothing was pulled, or every
    pulled recording is mono (--allow-mono stacks; separated scoring needs one
    channel per party). One unscoreable file inside an otherwise scoreable set
    is reported as a skip with its reason, mirroring the pull loop."""
    rows = []
    worst = 0
    scored = 0
    for item in res["pulled"]:
        path = item["path"]
        try:
            env = run_single(stereo=path, stack=stack, expect="yield")
        except (ValueError, OSError) as exc:
            rows.append({"id": item["id"], "path": path, "scored": False,
                         "reason": _errors.sanitize_urls_in_text(str(exc))})
            continue
        scored += 1
        summary = env["summary"]
        rows.append({
            "id": item["id"], "path": path, "scored": True,
            "events": summary.get("events", 0),
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "not_scorable": summary.get("not_scorable", 0),
            "exit_code": int(env["exit_code"]),
        })
        worst = max(worst, int(env["exit_code"]))
    if not scored:
        raise ValueError(
            "--score needs at least one dual-channel recording in the pulled "
            "set, and this pull produced none (nothing pulled, or mono-only "
            "recordings). Pull a dual-channel stack (vapi, twilio, or retell "
            "with --call-id) and re-run, or score one file directly: "
            "hotato run --stereo <file>."
        )
    return {
        "expect": "yield",
        "recordings": rows,
        "scored": scored,
        "skipped": len(rows) - scored,
        "exit_code": worst,
    }


def run_pull(stack=None, *, ids=None, since=None, limit=50, out=None,
             allow_mono=False, score=False, api_key=None, account_sid=None,
             auth_token=None, model_id=None, agent_id=None, base_url=None,
             fmt="text") -> int:
    """`hotato pull`: bulk-fetch recent recordings into a local directory.

    With ``score=True`` (`--score`) the pulled dual-channel set is then scored
    offline in the same invocation (see :func:`_score_pulled`), and the exit
    code follows the run contract: 1 when a scorable event failed, 2 when the
    set holds nothing scoreable."""
    overrides = _overrides_from(api_key, account_sid, auth_token, model_id,
                                agent_id, base_url)
    stack, creds = _resolve_for_pull(stack, overrides)
    out_dir = out or f"hotato-pull-{stack}"
    res = pull(stack, creds, out_dir=out_dir, ids=ids, since=since, limit=limit,
               allow_mono=allow_mono, log=lambda m: sys.stderr.write(m + "\n"))
    score_res = _score_pulled(res, stack=stack) if score else None
    if score_res is not None:
        res["score"] = score_res
    if fmt == "json":
        print(_errors.safe_json_dumps(res, indent=2))
    else:
        print(f"hotato pull: {stack} -> {res['out_dir']}")
        print(f"  listed {res['listed']}, pulled {len(res['pulled'])}, "
              f"skipped {len(res['skipped'])}")
        for s in res["skipped"]:
            print(f"  [skip] {s['id']}: {s['reason']}")
        if score_res is not None:
            print(f"  scored {score_res['scored']} of {len(res['pulled'])} "
                  "pulled recordings offline (expected behavior: yield)")
            for r in score_res["recordings"]:
                name = os.path.basename(r["path"])
                if not r["scored"]:
                    print(f"  [skip] {name}: {r['reason']}")
                elif r["events"] and r["not_scorable"] == r["events"]:
                    print(f"  [NOT SCORABLE] {name}")
                else:
                    mark = "FAIL" if r["exit_code"] else "PASS"
                    line = f"  [{mark}] {name}: {r['passed']} passed, {r['failed']} failed"
                    if r["not_scorable"]:
                        line += f", {r['not_scorable']} not scorable"
                    print(line)
        if res["pulled"]:
            print(f"  next: hotato analyze {res['out_dir']}  "
                  f"(or use `hotato sweep --stack {stack}` to pull + analyze in one)")
    return score_res["exit_code"] if score_res is not None else 0


def _atomic_write_text(path: str, text: str) -> None:
    """Write ``text`` to ``path`` atomically: a temp file in the SAME directory,
    then ``os.replace`` (mirroring cli._atomic_write_text / loop.save_state).
    ``open(path, "w")`` truncates the target the instant it is opened, so a crash
    / full disk / kill mid-write leaves a previously-good file truncated in
    place; writing a temp file first and renaming means the destination is only
    ever the old bytes or the complete new bytes."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".hotato-tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_sweep(stack=None, *, ids=None, since=None, limit=50, dir=None, out=None,
              allow_mono=False, top=25, audio_top=8, pre=2.0, post=4.0,
              min_gap=2.0, no_open=False, demo=False, api_key=None,
              account_sid=None, auth_token=None, model_id=None, agent_id=None,
              base_url=None, caller_channel=0, agent_channel=1,
              fmt="html", notify=None) -> int:
    """`hotato sweep`: pull recent recordings, then run the P1 analyze over them
    -- the 'connect once, see every turn-taking problem across your real calls'
    flow. The analyze step is reused wholesale. With ``demo=True`` the pull is
    replaced by the two bundled real demo calls (the same recordings `hotato
    demo` scores), so the first sweep works with no account, no credentials and
    no network; everything from analyze onward is the identical code path.

    ``notify`` is an optional list of webhook URLs (``--notify``, repeatable):
    off by default. When given, a JSON summary (counts, top candidate timing,
    local artifact paths -- no audio, no credentials, no transcript) is POSTed
    to each once the sweep finishes; see ``hotato.notify``."""
    from . import analyze as _analyze
    from . import notify as _notify

    # Validate the global scan flags BEFORE the (slow, network) pull, so a typo'd
    # --min-gap / bad channel is an immediate exit-2 usage error, never a pull
    # followed by a false clean 'found nothing'. The --notify URLs get the same
    # treatment: a bad scheme is a usage mistake, caught here, not after the
    # sweep already ran.
    _analyze.validate_scan_args(
        caller_channel=caller_channel, agent_channel=agent_channel,
        min_gap_sec=min_gap,
    )
    notify_urls = _notify.validate_notify_urls(notify)

    overrides = _overrides_from(api_key, account_sid, auth_token, model_id,
                                agent_id, base_url)
    if demo:
        # --demo is a source of calls, not a different sweep: it must not be
        # combined with the flags that describe a real stack. Naming the exact
        # flags to remove keeps this a one-edit fix.
        conflicts = [flag for flag, val in (
            ("--stack", stack), ("--call-id", ids), ("--since", since),
            ("--allow-mono", allow_mono), ("--dir", dir),
        ) if val]
        conflicts += sorted("--" + k.replace("_", "-") for k in overrides)
        if conflicts:
            raise ValueError(
                "--demo sweeps the two bundled demo calls on this machine and "
                "takes no stack, credential, or pull flags. Remove "
                + ", ".join(conflicts) + ", or remove --demo to sweep a real "
                "stack."
            )
        from importlib import resources

        stack = "demo"
        pull_dir = str(resources.files("hotato").joinpath(
            "data", "demo", "failing", "audio"))
        wavs = sorted(n for n in os.listdir(pull_dir)
                      if n.lower().endswith(".wav"))
        # The same result shape pull() returns, so the summary lines and the
        # JSON envelope's pull block are identical to a real sweep's.
        res = {
            "stack": stack, "out_dir": pull_dir, "listed": len(wavs),
            "pulled": [{"id": os.path.splitext(n)[0],
                        "path": os.path.join(pull_dir, n)} for n in wavs],
            "skipped": [],
        }
        sys.stderr.write(
            f"[sweep] demo: {len(wavs)} bundled real calls, analyzed in "
            "place; no credentials, no network\n"
        )
    else:
        stack, creds = _resolve_for_pull(stack, overrides)
        pull_dir = dir or f"hotato-sweep-{stack}"
        res = pull(stack, creds, out_dir=pull_dir, ids=ids, since=since,
                   limit=limit, allow_mono=allow_mono,
                   log=lambda m: sys.stderr.write(m + "\n"))
        sys.stderr.write(
            f"[sweep] {stack}: pulled {len(res['pulled'])} of {res['listed']} "
            f"listed ({len(res['skipped'])} skipped) into {res['out_dir']}\n"
        )

    aggregate, per_file = _analyze.analyze_folder(
        pull_dir, caller_channel=caller_channel, agent_channel=agent_channel,
        min_gap_sec=min_gap, pre_sec=pre, post_sec=post,
    )
    if fmt == "json":
        capped = dict(aggregate)
        if top > 0:
            capped["candidates"] = aggregate["candidates"][:top]
        capped["shown"] = len(capped["candidates"])
        capped["pull"] = {
            "stack": stack, "listed": res["listed"],
            "pulled": len(res["pulled"]), "skipped": len(res["skipped"]),
        }
        print(_errors.safe_json_dumps(capped, indent=2))
        if notify_urls:
            payload = _notify.sweep_payload(stack=stack, aggregate=aggregate,
                                            pull_dir=pull_dir)
            _notify.notify_all(notify_urls, payload)
        return 0

    out_file = out or f"hotato-sweep-{stack}.html"
    # The promote buttons name this sweep's DEFAULT json result file (the one
    # `hotato sweep ... --format json > hotato-sweep-STACK.json` writes), never
    # the --out path, so the dashboard bytes stay identical whatever the page
    # was saved as.
    html_str = _analyze.build_dashboard_html(
        aggregate, per_file, top=top, audio_top=audio_top,
        report_json=f"hotato-sweep-{stack}.json",
    )
    # Atomic write, like every other --out writer: a kill / disk-full mid-write
    # must not destroy a previously-good report at this path (sweep can run long
    # -- it downloads real recordings first -- so it is exactly the command most
    # likely to be interrupted).
    _atomic_write_text(out_file, html_str)
    size = os.path.getsize(out_file)
    print(
        f"hotato sweep: {stack} -> {out_file}  "
        f"[pulled {len(res['pulled'])}, {aggregate['calls_scanned']} scanned, "
        f"{aggregate['calls_skipped']} skipped, "
        f"{aggregate['total_candidates']} candidate moments, {size / 1048576.0:.1f} MB]",
        file=sys.stderr,
    )
    if notify_urls:
        payload = _notify.sweep_payload(stack=stack, aggregate=aggregate,
                                        out_file=out_file, pull_dir=pull_dir)
        _notify.notify_all(notify_urls, payload)
    if not no_open:
        try:
            from .cli import _try_open

            _try_open(out_file)
        except Exception:  # pragma: no cover - opening is a nicety only
            pass
    return 0
