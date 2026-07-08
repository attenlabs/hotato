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
from typing import List, Optional, Tuple

from ._engine.audio import write_wav  # noqa: F401  (used by the pipecat scaffold)
from .core import process_exit_code, run_single

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

# Mono/mixed-only stacks, all verified verbatim in
# hotato-launch/INTEGRATION-SPEC-2026-07-07.md as producing a single combined
# recording with no per-party channel separation. Every one is scored ONLY
# behind an explicit --allow-mono / HOTATO_ALLOW_MONO=1 opt-in, labelled
# indicative only.
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
        print(json.dumps(env, indent=2))
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
    print(f"[demo] {stack}: bundled two-channel reference '{scenario_id}' ({title})")
    print(f"[demo] wrote two-channel capture -> {out}")
    env = score(out, stack=stack, onset_sec=onset, expect=expect)
    return report(env, fmt)


# --- tiny stdlib HTTP (keeps the core zero-dependency) --------------------

class _HTTPStatusError(ValueError):
    """HTTP error that keeps the status code so callers can branch on it
    (e.g. Twilio returns 400 when dual-channel media is unavailable)."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _http_get(url: str, headers: Optional[dict] = None, timeout: int = 60) -> bytes:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user-supplied API
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise _HTTPStatusError(
            f"HTTP {exc.code} from {url}: {exc.reason}. {body}".strip(), exc.code
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - live path
        raise ValueError(f"network error fetching {url}: {exc.reason}") from exc


def _http_get_json(url: str, headers: Optional[dict] = None, timeout: int = 60) -> dict:
    return json.loads(_http_get(url, headers=headers, timeout=timeout).decode("utf-8"))


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


_ALLOWED_DOWNLOAD_SCHEMES = ("http", "https")


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
        hosts = {h.strip().lower() for h in allow.split(",") if h.strip()}
        if parsed.hostname.lower() not in hosts:
            raise ValueError(
                f"recording download host {parsed.hostname!r} is not in "
                "HOTATO_INGEST_ALLOWED_HOSTS; refusing to fetch it."
            )
    return url


def _download(url: str, dest: str, headers: Optional[dict] = None, timeout: int = 120) -> str:
    _validate_download_url(url)
    data = _http_get(url, headers=headers, timeout=timeout)
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def _same_host(url: str, base_url: str) -> bool:
    """True only if ``url`` and ``base_url`` resolve to the same host."""
    from urllib.parse import urlparse

    u = urlparse(url).hostname
    b = urlparse(base_url).hostname
    return bool(u) and bool(b) and u.lower() == b.lower()


def _auth_headers_for(url: str, base_url: str, headers: Optional[dict]) -> Optional[dict]:
    """Return ``headers`` (the credential) ONLY when ``url`` is on the vendor's
    own host (``base_url``). A ``recording_url`` comes from the vendor's JSON
    RESPONSE, not from something the operator typed, so if it points off-domain
    (a compromised account, tampered metadata, a redirect, or a mis-set
    ``--base-url``) attaching the API key would exfiltrate the credential to that
    host. Vendor download URLs are pre-signed and need no auth, so dropping the
    header when the host does not match keeps the download working while never
    sending the secret anywhere but the vendor's own API host."""
    if headers and _same_host(url, base_url):
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
    import wave

    try:
        with wave.open(path, "rb") as wf:
            return wf.getnchannels()
    except (wave.Error, EOFError, OSError):
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
    artifact = call.get("artifact") or {}
    recording = artifact.get("recording") or {}
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
    dest = _out_wav(out_path, "hotato-vapi-")
    _download(url, dest, timeout=max(timeout, 120))
    ch = _wav_channels(dest)
    if ch is not None and ch != 2:
        raise ValueError(
            f"Vapi stereo recording download has {ch} channel(s), expected 2. "
            f"Scoring needs a 2-channel file: {_MONO_WHY}."
        )
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
        dest = _out_wav(out_path, "hotato-retell-")
        _download(url, dest, timeout=max(timeout, 120))
        _require_two_channels(dest, "Retell (multi-channel recording)")
        return dest
    mono_url = call.get("recording_url")
    if mono_url:
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
        _download(
            media_base + "?RequestedChannels=2", dest, headers=auth,
            timeout=max(timeout, 120),
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
    _require_two_channels(dest, "Twilio (RequestedChannels=2 media)")
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
        "`hotato capture --stack {stack} --stereo <file>`."
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
    #   hotato capture --stack livekit --caller caller.wav --agent agent.wav \\
    #                  --onset <sec> --expect yield

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
         export VAPI_API_KEY=<your private key>
         hotato capture --stack vapi --call-id <call-id> --expect yield

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
         export RETELL_API_KEY=<your api key>
         hotato capture --stack retell --call-id <call-id> --expect yield

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
    """Score a fetched capture. When an explicit --allow-mono download produced
    a 1-channel file, score it degraded (the single channel stands in for both
    parties, so talk-over is not attributable) and say so loudly."""
    if _wav_channels(path) == 1:
        sys.stderr.write(
            f"[{stack}] degraded: mono file, scoring WITHOUT party attribution "
            "(the single channel stands in for both parties). Treat results as "
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
                "explicit ids instead: hotato pull --stack retell --call-id <id> "
                "[--call-id <id> ...]."
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
        try:
            path = fetch_one(stack, ident, creds, dest, allow_mono=allow_mono)
            pulled.append({"id": ident, "path": path})
            if log:
                log(f"[{stack}] pulled {ident} -> {path}")
        except Exception as exc:
            # pull()'s contract is "one bad call never aborts the pull": a single
            # unscorable/failed call is skipped honestly and the batch continues.
            # ValueError/_HTTPStatusError/OSError are the expected failures, but
            # ANY adapter (current or future) that raises something else must not
            # take down the whole run and every other id with it -- so catch
            # broadly and record the type in the reason for diagnosis.
            reason = str(exc) or f"{type(exc).__name__}"
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
            note = str(exc)

    path = connections.save(stack, creds)
    fields = ", ".join(sorted(creds.keys()))
    if fmt == "json":
        print(json.dumps({
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


def run_pull(stack=None, *, ids=None, since=None, limit=50, out=None,
             allow_mono=False, api_key=None, account_sid=None, auth_token=None,
             model_id=None, agent_id=None, base_url=None, fmt="text") -> int:
    """`hotato pull`: bulk-fetch recent recordings into a local directory."""
    overrides = _overrides_from(api_key, account_sid, auth_token, model_id,
                                agent_id, base_url)
    stack, creds = _resolve_for_pull(stack, overrides)
    out_dir = out or f"hotato-pull-{stack}"
    res = pull(stack, creds, out_dir=out_dir, ids=ids, since=since, limit=limit,
               allow_mono=allow_mono, log=lambda m: sys.stderr.write(m + "\n"))
    if fmt == "json":
        print(json.dumps(res, indent=2))
    else:
        print(f"hotato pull: {stack} -> {res['out_dir']}")
        print(f"  listed {res['listed']}, pulled {len(res['pulled'])}, "
              f"skipped {len(res['skipped'])}")
        for s in res["skipped"]:
            print(f"  [skip] {s['id']}: {s['reason']}")
        if res["pulled"]:
            print(f"  next: hotato analyze {res['out_dir']}  "
                  f"(or use `hotato sweep --stack {stack}` to pull + analyze in one)")
    return 0


def run_sweep(stack=None, *, ids=None, since=None, limit=50, dir=None, out=None,
              allow_mono=False, top=25, audio_top=8, pre=2.0, post=4.0,
              min_gap=2.0, no_open=False, api_key=None, account_sid=None,
              auth_token=None, model_id=None, agent_id=None, base_url=None,
              caller_channel=0, agent_channel=1, fmt="html") -> int:
    """`hotato sweep`: pull recent recordings, then run the P1 analyze over them
    -- the 'connect once, see every turn-taking problem across your real calls'
    flow. The analyze step is reused wholesale."""
    from . import analyze as _analyze

    overrides = _overrides_from(api_key, account_sid, auth_token, model_id,
                                agent_id, base_url)
    stack, creds = _resolve_for_pull(stack, overrides)
    pull_dir = dir or f"hotato-sweep-{stack}"
    res = pull(stack, creds, out_dir=pull_dir, ids=ids, since=since, limit=limit,
               allow_mono=allow_mono, log=lambda m: sys.stderr.write(m + "\n"))
    sys.stderr.write(
        f"[sweep] {stack}: pulled {len(res['pulled'])} of {res['listed']} listed "
        f"({len(res['skipped'])} skipped) into {res['out_dir']}\n"
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
        print(json.dumps(capped, indent=2))
        return 0

    out_file = out or f"hotato-sweep-{stack}.html"
    html_str = _analyze.build_dashboard_html(
        aggregate, per_file, top=top, audio_top=audio_top,
    )
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write(html_str)
    size = os.path.getsize(out_file)
    print(
        f"hotato sweep: {stack} -> {out_file}  "
        f"[pulled {len(res['pulled'])}, {aggregate['calls_scanned']} scanned, "
        f"{aggregate['calls_skipped']} skipped, "
        f"{aggregate['total_candidates']} candidate moments, {size / 1048576.0:.1f} MB]",
        file=sys.stderr,
    )
    if not no_open:
        try:
            from .cli import _try_open

            _try_open(out_file)
        except Exception:  # pragma: no cover - opening is a nicety only
            pass
    return 0
