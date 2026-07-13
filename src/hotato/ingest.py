"""``hotato ingest``: the composable passive on-ramp.

Wire a webhook to invoke ``hotato ingest`` once, and every completed call gets
scanned for CANDIDATE turn-taking moments automatically. This is the on-ramp the
reviewers asked for ("I don't want to remember to run a CLI after every bad
call"), built by COMPOSITION -- it adds only a per-stack webhook parser and reuses
the primitives that already exist:

    parse the webhook payload  ->  extract the call id / recording locator
    fetch the recording        ->  hotato.capture (the SAME fetch the adapters use)
    scan for candidates        ->  hotato.scan (discovery, no labels, no verdict)
    write a candidate report   ->  JSON always; --out HTML optional

What ingest is NOT:
  * It is not a verdict. Ingest is DISCOVERY: it surfaces TIMING candidates, never
    a pass/fail and never an intent claim. Exit 0 means "ran" (zero or more
    candidates); exit 2 means the payload could not be parsed, the recording could
    not be fetched/read, or the input was not scorable.
  * It is not a daemon. Ingest ships the command; YOU own the trigger (a webhook
    handler, a serverless function, a cron over your call log). There is no hosted
    service and no long-running process, so the offline/self-host wedge stays
    intact -- the only network is the SAME recording fetch ``capture`` already
    does, and everything else is offline.
  * It never auto-labels, auto-creates a fixture, or auto-tunes. The human label
    step stays human: review the candidates, then promote ONE with
    ``hotato fixture create``. Hotato never infers intent from energy.

Untrusted-input law: a webhook payload is DATA, never instructions. The parsers
below read named fields defensively and never execute, scaffold, or act on
anything the payload contains.

Verified webhook field paths (against live vendor docs, 2026-07-07), and where a
field could not be confirmed from the live docs it is parsed DEFENSIVELY:

  * Vapi     end-of-call-report webhook: the call id is ``message.call.id``
             (confirmed, docs.vapi.ai/server-url/events). Ingest extracts the id
             and delegates the stereo-recording fetch to ``capture.capture_vapi``
             (which already resolved ``artifact.recording.stereoUrl`` and its
             deprecated fallbacks, verified 2026-07-06), so the recording URL is
             never read from the untrusted payload.
  * Retell   call webhook: the event type is top-level ``event`` and the call id
             is ``call.call_id`` (confirmed, docs.retellai.com/features/webhook).
             The recording fetch delegates to ``capture.capture_retell``.
  * Twilio   recordingStatusCallback: ``RecordingSid`` (plus ``CallSid``,
             ``RecordingStatus``, ``RecordingChannels``) are the documented
             callback parameters (confirmed, twilio.com/docs/voice/api/recording).
             Sent form-encoded; ingest parses JSON or form-encoded bodies. The
             fetch delegates to ``capture.capture_twilio``.
  * LiveKit  egress webhook: the file results live under
             ``egressInfo.fileResults[].location`` / ``.filename`` -- parsed
             DEFENSIVELY (the exact JSON casing was not confirmable from the live
             docs in this build; verify against your LiveKit server version).
             LiveKit egress lands in YOUR storage, so ingest also accepts a plain
             ``recording_url`` / ``recording_path`` locator you supply.
  * Pipecat  has no vendor webhook (the pipeline is your own infra). Supply a
             minimal event, e.g. ``{"recording_path": "captured.wav"}`` or
             ``{"recording_url": "https://.../captured.wav"}``. Parsed defensively.
"""

from __future__ import annotations

from .errors import open_regular as _open_regular

import json

from . import errors as _errors
import os
import sys
from typing import Optional

from . import capture as _capture
from . import scan as _scan

__all__ = [
    "STACKS",
    "IngestError",
    "parse_event",
    "run_ingest",
    "render_candidates_html",
]

# Same stacks capture/adapters support.
STACKS = ("vapi", "retell", "twilio", "livekit", "pipecat")


class IngestError(ValueError):
    """A parse / fetch / IO / not-scorable problem. Subclasses ``ValueError`` so
    the CLI maps it to exit 2 (never a crash, never a pass/fail verdict)."""


# --- reading the payload (untrusted DATA) ----------------------------------

def _read_payload(event_path: str) -> dict:
    """Read a webhook payload file as a dict. Accepts JSON (the common case) and
    falls back to form-encoded bodies (Twilio's recordingStatusCallback posts
    ``application/x-www-form-urlencoded``). Never executes anything in the file."""
    try:
        with _open_regular(event_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise IngestError(f"cannot read --event {event_path!r}: {exc}") from exc
    raw = raw.strip()
    if not raw:
        raise IngestError(
            f"--event {event_path!r} is empty; expected a webhook payload "
            "(JSON, or a form-encoded body for Twilio)."
        )
    try:
        payload = json.loads(raw)
    except RecursionError as exc:
        # A pathologically deeply nested payload (thousands of nested JSON
        # arrays/objects): CPython's json decoder recurses once per nesting
        # level and raises a bare RecursionError, not a JSONDecodeError.
        # Caught right here (closest to the source, before any further work
        # runs on a possibly-ragged interpreter stack) and turned into a
        # clean IngestError instead of falling into the form-encoded
        # fallback below, which would not help and is not what happened.
        raise IngestError(
            f"--event {event_path!r} is too deeply nested to parse safely."
        ) from exc
    except json.JSONDecodeError as exc:
        # Form-encoded fallback (Twilio posts application/x-www-form-urlencoded).
        # Only attempt it on a body that actually looks form-encoded (has a
        # key=value pair); otherwise the input is simply malformed.
        if "=" not in raw:
            raise IngestError(
                f"--event {event_path!r} is not valid JSON and is not a "
                f"form-encoded webhook body ({exc})."
            ) from exc
        from urllib.parse import parse_qs

        parsed = parse_qs(raw, keep_blank_values=True)
        if not parsed:
            raise IngestError(
                f"--event {event_path!r} is neither JSON nor a form-encoded "
                "webhook body."
            )
        payload = {k: (v[0] if isinstance(v, list) and v else v)
                   for k, v in parsed.items()}
    if not isinstance(payload, dict):
        raise IngestError(
            f"--event {event_path!r} parsed to {type(payload).__name__}, not an "
            "object; expected a webhook payload object."
        )
    return payload


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _first_str(*values) -> Optional[str]:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# --- per-stack parsers: payload -> normalized locator ----------------------
# Each returns a dict with some of: call_id, recording_sid, recording_url,
# recording_path. Everything is read defensively; a missing field is simply
# absent, never fabricated.

def _parse_vapi(payload: dict) -> dict:
    # The end-of-call-report webhook wraps the event under "message"; a bare
    # call object is also accepted. Confirmed: message.call.id.
    msg = _as_dict(payload.get("message")) or payload
    call = _as_dict(msg.get("call")) or _as_dict(payload.get("call"))
    call_id = _first_str(
        call.get("id"), msg.get("call_id"), payload.get("call_id"),
        payload.get("id"),
    )
    return {"call_id": call_id}


def _parse_retell(payload: dict) -> dict:
    # Confirmed: top-level event, call.call_id. "data" is accepted as a
    # defensive alias for the call container.
    call = _as_dict(payload.get("call")) or _as_dict(payload.get("data")) or payload
    call_id = _first_str(call.get("call_id"), payload.get("call_id"))
    return {"call_id": call_id, "event": _first_str(payload.get("event"))}


def _parse_twilio(payload: dict) -> dict:
    # Confirmed recordingStatusCallback params: RecordingSid, CallSid,
    # RecordingStatus, RecordingChannels. Lowercase aliases parsed defensively.
    sid = _first_str(payload.get("RecordingSid"), payload.get("recording_sid"))
    return {
        "recording_sid": sid,
        "call_sid": _first_str(payload.get("CallSid"), payload.get("call_sid")),
        "recording_channels": _first_str(
            payload.get("RecordingChannels"), payload.get("recording_channels")
        ),
    }


def _parse_livekit(payload: dict) -> dict:
    # Egress webhook. egressInfo.fileResults[].location / .filename -- parsed
    # DEFENSIVELY (casing unconfirmed from live docs in this build). LiveKit
    # egress writes to YOUR storage, so a plain recording_url / recording_path
    # locator you supply is also accepted (and preferred when present).
    info = (_as_dict(payload.get("egressInfo")) or _as_dict(payload.get("egress_info"))
            or _as_dict(payload.get("egress")) or payload)
    files = (info.get("fileResults") or info.get("file_results")
             or info.get("file") or [])
    loc = None
    fname = None
    if isinstance(files, list) and files and isinstance(files[0], dict):
        f0 = files[0]
        loc = _first_str(f0.get("location"), f0.get("downloadUrl"),
                         f0.get("download_url"), f0.get("url"))
        fname = _first_str(f0.get("filename"), f0.get("filepath"))
    rec_url = _first_str(payload.get("recording_url"), loc)
    rec_path = _first_str(payload.get("recording_path"), payload.get("path"))
    if rec_url is None and rec_path is None and fname is not None:
        # A bare filename from egress is a local/storage path, not a URL.
        rec_path = fname
    return {"recording_url": rec_url, "recording_path": rec_path}


def _parse_pipecat(payload: dict) -> dict:
    # No vendor webhook: the pipeline is your infra. A minimal, user-defined
    # event carries the locator directly. Parsed defensively.
    return {
        "recording_url": _first_str(payload.get("recording_url"), payload.get("url")),
        "recording_path": _first_str(
            payload.get("recording_path"), payload.get("stereo"),
            payload.get("path"),
        ),
        "call_id": _first_str(payload.get("call_id")),
    }


_PARSERS = {
    "vapi": _parse_vapi,
    "retell": _parse_retell,
    "twilio": _parse_twilio,
    "livekit": _parse_livekit,
    "pipecat": _parse_pipecat,
}


def parse_event(stack: str, payload: dict) -> dict:
    """Extract the normalized recording locator for ``stack`` from a webhook
    payload (untrusted DATA). Public for testing."""
    stack = (stack or "").strip().lower()
    if stack not in _PARSERS:
        raise IngestError(f"unknown stack {stack!r}; choose one of {', '.join(STACKS)}")
    return _PARSERS[stack](payload if isinstance(payload, dict) else {})


# --- fetch: reuse capture's fetch logic ------------------------------------

_ALLOWED_RECORDING_URL_SCHEMES = ("http", "https")


def _validate_recording_url(url: str, stack: str) -> str:
    """A recording_url arrives from an UNTRUSTED webhook payload. Restrict it to
    an http(s) URL with a host before it is fetched, so a spoofed event cannot
    make ingest read a local file (``file://``), inline data (``data:``), or an
    arbitrary non-web endpoint. When ``HOTATO_INGEST_ALLOWED_HOSTS`` is set (a
    comma-separated host list) the URL's host must be on it -- the operator's
    lever to also close internal-network / cloud-metadata SSRF."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_RECORDING_URL_SCHEMES:
        raise IngestError(
            f"recording_url in the {stack} event uses the unsupported scheme "
            f"{scheme or '(none)'!r}; ingest only downloads http(s) recording "
            "URLs. file://, data:, ftp:// and similar are refused so a webhook "
            "payload cannot make ingest read a local file or an internal endpoint."
        )
    if not parsed.hostname:
        raise IngestError(
            f"recording_url in the {stack} event has no host; refusing to fetch it."
        )
    allow = os.environ.get("HOTATO_INGEST_ALLOWED_HOSTS", "").strip()
    if allow:
        # Canonicalize both sides (via capture's helper) so textually-different
        # but equal IPv6 forms (``[::1]`` vs ``[0:0:0:0:0:0:0:1]``) compare
        # equal instead of spuriously failing the allowlist. See
        # capture._canonical_host for the full rationale; the fail-closed
        # SSRF guard below is unaffected either way.
        hosts = {_capture._canonical_host(h) for h in allow.split(",") if h.strip()}
        if _capture._canonical_host(parsed.hostname) not in hosts:
            raise IngestError(
                f"recording_url host {parsed.hostname!r} is not in "
                "HOTATO_INGEST_ALLOWED_HOSTS; refusing to fetch it."
            )
    # Default-deny SSRF: a spoofed webhook must not make ingest fetch an internal
    # service or cloud-metadata endpoint (169.254.169.254, 127.0.0.1, RFC1918,
    # ...). Reuse capture's guard; it raises ValueError, which we surface as the
    # ingest-native IngestError.
    try:
        _capture._reject_private_host(parsed.hostname, "a recording_url")
    except ValueError as exc:
        raise IngestError(str(exc)) from exc
    return url


def _validate_recording_path(path: str, stack: str) -> str:
    """A recording_path arrives from an UNTRUSTED webhook payload, so reading it
    is a local-file-read primitive triggerable by anyone who can forge a
    LiveKit/Pipecat-shaped event. The sandbox is therefore MANDATORY, not opt-in:
    ``HOTATO_INGEST_DIR`` must be set, and the resolved real path MUST stay inside
    that egress directory. A spoofed event can then never point ingest at an
    arbitrary local file (``/etc/...``, ``~/.hotato/connections.json``) or escape
    via ``..``. With no base configured ingest fails CLOSED (it reads nothing)
    rather than trusting the payload's path -- the operator opts IN to local-path
    ingest by naming the directory their own infra writes to."""
    base = os.environ.get("HOTATO_INGEST_DIR", "").strip()
    if not base:
        raise IngestError(
            "reading a local recording_path from a webhook payload is disabled "
            "until you set HOTATO_INGEST_DIR to the egress directory your "
            f"{stack} infra writes to. ingest will then read ONLY files inside "
            "that directory. This fails closed so a forged/untrusted webhook "
            "event cannot make ingest read an arbitrary local file (for example "
            "~/.hotato/connections.json or /etc/passwd)."
        )
    real = os.path.realpath(os.path.expanduser(path))
    base_real = os.path.realpath(os.path.expanduser(base))
    try:
        inside = os.path.commonpath([base_real, real]) == base_real
    except ValueError:  # different drives (Windows) -> never inside
        inside = False
    if not inside:
        raise IngestError(
            f"recording_path {path!r} resolves outside HOTATO_INGEST_DIR "
            f"({base}); refusing to read it. Point ingest at a file inside "
            "your configured egress directory."
        )
    return real


def _require_env(name: str, stack: str, what: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise IngestError(
            f"{stack} ingest needs {what}: set {name}. (This is the only network "
            "step; everything else is offline.)"
        )
    return val


def _resolve_recording(
    stack: str,
    locator: dict,
    *,
    out: Optional[str],
    allow_mono: bool,
) -> str:
    """Turn a normalized locator into a local WAV path by REUSING the capture
    fetch adapters. Network happens ONLY here (same posture as ``capture``)."""
    if stack == "vapi":
        call_id = locator.get("call_id")
        if not call_id:
            raise IngestError(
                "no call id in the Vapi payload (looked for message.call.id). "
                "Pass --call-id, or point --event at an end-of-call-report webhook."
            )
        key = _require_env("VAPI_API_KEY", "vapi", "your private API key")
        return _capture.capture_vapi(call_id=call_id, api_key=key, out_path=out)

    if stack == "retell":
        call_id = locator.get("call_id")
        if not call_id:
            raise IngestError(
                "no call id in the Retell payload (looked for call.call_id). "
                "Pass --call-id, or point --event at a Retell call webhook."
            )
        key = _require_env("RETELL_API_KEY", "retell", "your API key")
        return _capture.capture_retell(
            call_id=call_id, api_key=key, out_path=out, allow_mono=allow_mono
        )

    if stack == "twilio":
        sid = locator.get("recording_sid")
        if not sid:
            raise IngestError(
                "no recording sid in the Twilio payload (looked for RecordingSid). "
                "Pass --recording-sid, or point --event at a recordingStatusCallback."
            )
        acct = _require_env("TWILIO_ACCOUNT_SID", "twilio", "your Account SID")
        token = _require_env("TWILIO_AUTH_TOKEN", "twilio", "your Auth Token")
        return _capture.capture_twilio(
            recording_sid=sid, account_sid=acct, auth_token=token,
            out_path=out, allow_mono=allow_mono,
        )

    # livekit / pipecat: capture has no direct fetch (recording lands in YOUR
    # infra). Use the locator from the event: a local path directly, or a URL
    # downloaded with capture's stdlib downloader.
    path = locator.get("recording_path")
    if path:
        safe_path = _validate_recording_path(path, stack)
        if not os.path.exists(safe_path):
            raise IngestError(
                f"recording_path {path!r} from the {stack} event does not exist "
                "on this machine. LiveKit/Pipecat recordings live in your own "
                "storage; point ingest at the file your infra produced."
            )
        return safe_path
    url = locator.get("recording_url")
    if url:
        safe_url = _validate_recording_url(url, stack)
        dest = _capture._out_wav(out, f"hotato-{stack}-")
        return _capture._download(safe_url, dest)
    raise IngestError(
        f"no recording locator in the {stack} event: supply recording_path (a "
        "local 2-channel WAV) or recording_url. LiveKit egress and Pipecat "
        "recording both write to YOUR infra, so ingest reads the file they wrote."
    )


# --- candidate HTML (optional --out; reuses report.py house style) ---------

def render_candidates_html(scan: dict, *, top: int = 0) -> str:
    """Render the scan candidate result as a self-contained HTML page, reusing
    ``report.py``'s escaping and honesty footer so it matches the house style.
    ``top`` caps the listing (0 = all)."""
    from . import report as _report

    esc = _report._esc
    total = scan.get("total_candidates", 0)
    rows = scan.get("candidates", [])
    shown = rows if top <= 0 else rows[:top]
    dur = "{:.1f}".format(scan.get("duration_sec", 0) or 0)
    plural = "s" if total != 1 else ""

    body = [
        "<main class='wrap'>",
        "<header class='top'><div class='logo'></div><div>",
        "<h1 class='h1'>hotato ingest - candidate moments</h1>",
        f"<div class='tagline'>{esc(scan.get('source', ''))} &middot; "
        f"{esc(dur)}s &middot; {total} candidate moment{plural}</div>",
        f"<div class='subtle'>{esc(scan.get('note', ''))}</div>",
        "</div></header>",
    ]
    if total == 0:
        body.append(
            "<p class='subtle'>No candidate moments found. Nothing to review; "
            "this call did not surface an overlap onset or a long response gap.</p>"
        )
    else:
        if len(shown) < total:
            body.append(
                f"<p class='subtle'>Showing {len(shown)} of {total} by salience "
                "(longest overlap or gap first).</p>"
            )
        body.append("<table class='cand'><thead><tr>"
                    "<th>#</th><th>t (s)</th><th>kind</th><th>detail</th>"
                    "</tr></thead><tbody>")
        for i, c in enumerate(shown, 1):
            detail = _scan._line(i, c).split(c["kind"], 1)[-1].strip()
            t_str = "{:.2f}".format(c["t_sec"])
            body.append(
                f"<tr><td>{i}</td><td class='mono'>{esc(t_str)}</td>"
                f"<td class='mono'>{esc(c['kind'])}</td>"
                f"<td>{esc(detail)}</td></tr>"
            )
        body.append("</tbody></table>")
    body.append(
        "<p class='subtle'>These are timing candidates, not verdicts. Review one, "
        "then promote it to a permanent regression test with "
        "<span class='mono'>hotato fixture create --onset &lt;t&gt; "
        "--expect yield|hold</span>.</p>"
    )
    body.append(_report._footer())
    body.append("</main>")

    css = _report._CSS % _report._C
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>hotato ingest - candidates</title>"
        f"<style>{css}\n"
        "table.cand{border-collapse:collapse;width:100%;margin:14px 0;font-size:13.5px}"
        f"table.cand th,table.cand td{{border-bottom:1px solid {_report._C['line']};"
        "padding:6px 8px;text-align:left;vertical-align:top}"
        f"table.cand th{{color:{_report._C['muted']};font-weight:600}}"
        "</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )


# --- orchestration the CLI calls -------------------------------------------

def run_ingest(
    stack: str,
    *,
    event: Optional[str] = None,
    call_id: Optional[str] = None,
    recording_sid: Optional[str] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    allow_mono: bool = False,
    out: Optional[str] = None,
    fmt: str = "text",
    top: int = 20,
    min_gap: float = _scan.DEFAULT_MIN_GAP_SEC,
) -> int:
    """Parse -> fetch -> scan -> report. Returns 0 on a completed run (zero or
    more candidates) and raises ``IngestError`` (-> exit 2) on any parse / fetch
    / IO / not-scorable problem. Discovery only: never a pass/fail."""
    stack = (stack or "").strip().lower()
    if stack not in STACKS:
        raise IngestError(f"unknown stack {stack!r}; choose one of {', '.join(STACKS)}")

    # 1. locator: an explicit id short-circuits the parser; otherwise parse the
    #    (untrusted) webhook payload.
    if event and (call_id or recording_sid):
        raise IngestError(
            "pass EITHER --event (a webhook payload) OR --call-id/--recording-sid "
            "(a direct id), not both."
        )
    if event:
        locator = parse_event(stack, _read_payload(event))
    elif call_id or recording_sid:
        # A direct id. For Twilio the identifier is the RecordingSid.
        if stack == "twilio":
            locator = {"recording_sid": recording_sid or call_id}
        else:
            locator = {"call_id": call_id, "recording_sid": recording_sid}
    else:
        raise IngestError(
            "ingest needs a source: --event <webhook payload> or --call-id <id> "
            "(--recording-sid <RE...> for twilio)."
        )

    allow_mono = allow_mono or _capture._env_allow_mono()

    # 2. fetch (the ONLY network step; reuses capture's adapters).
    path = _resolve_recording(stack, locator, out=out, allow_mono=allow_mono)
    sys.stderr.write(f"[{stack}] recording -> {path}\n")

    # Discovery needs one party per channel. A mono recording cannot attribute
    # overlap to caller vs agent, so it is NOT SCORABLE for discovery (exit 2),
    # even under --allow-mono (which only lets the fetch itself proceed).
    ch = _capture._wav_channels(path)
    if ch is None:
        raise IngestError(
            f"the recording at {path} is not a readable PCM WAV; ingest scans "
            "2-channel PCM WAV (one party per channel)."
        )
    if ch != 2:
        raise IngestError(
            f"the recording at {path} has {ch} channel(s); ingest discovery needs "
            "2 (one party per channel) to attribute overlap. A mono mix is not "
            "scorable for discovery."
        )

    # 3. scan for candidate moments (offline; no labels; no verdict).
    try:
        result = _scan.scan_recording(
            path,
            caller_channel=caller_channel,
            agent_channel=agent_channel,
            min_gap_sec=min_gap,
        )
    except ValueError as exc:
        raise IngestError(f"could not scan {path}: {exc}") from exc

    # 4. write the candidate report. JSON always to stdout (capped by --top when
    #    fmt=json); --out writes an HTML candidate report (all candidates).
    if out and out.lower().endswith((".html", ".htm")):
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(render_candidates_html(result, top=top))
        sys.stderr.write(
            f"[{stack}] wrote candidate report -> {out} "
            f"({result['total_candidates']} candidate(s))\n"
        )

    if fmt == "json":
        capped = dict(result)
        if top > 0:
            capped["candidates"] = result["candidates"][:top]
        capped["shown"] = len(capped["candidates"])
        print(_errors.safe_json_dumps(capped, indent=2))
    else:
        print(_scan.render_text(result, top=top))
        print(
            "\nNext: review a candidate, then promote it with "
            "`hotato fixture create --onset 42.18 --expect yield` (use "
            "--expect hold when the agent was right to keep talking). "
            "ingest never labels for you."
        )
    return 0
