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
from importlib import resources
from typing import List, Optional, Tuple

from ._engine.audio import write_wav  # noqa: F401  (used by the pipecat scaffold)
from .core import run_single

__all__ = [
    "STACKS",
    "score",
    "score_two_channel",
    "report",
    "demo",
    "capture",
    "capture_vapi",
    "capture_twilio",
    "capture_retell",
    "setup_text",
    "run_capture",
    "run_setup",
]

STACKS = ("vapi", "twilio", "livekit", "pipecat", "retell")

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
    return resources.files("hotato").joinpath("data", "audio", name)


def _scenario_meta(scenario_id: str) -> Tuple[Optional[float], str, str]:
    """Read a bundled scenario label -> (caller_onset_sec, expect, title)."""
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
    and return the envelope exit code. ``fmt`` is 'text' or 'json'."""
    if fmt == "json":
        print(json.dumps(env, indent=2))
        return env["exit_code"]
    ev = env["events"][0]
    v = ev["verdict"]
    tty = v["seconds_to_yield"]
    tty_s = "-" if tty is None else f"{tty:.2f}s"
    mark = "PASS" if v["passed"] else "FAIL"
    print(f"hotato [capture] stack={env['stack']} offline={env['offline']}")
    print(
        f"  [{mark}] {ev['event_id']}: did_yield={v['did_yield']} "
        f"seconds_to_yield={tty_s} talk_over={v['talk_over_sec']:.2f}s"
    )
    if not v["passed"] and ev.get("fix"):
        fx = ev["fix"]
        print(f"         fix[{fx['fix_class']}]: {fx['title']}")
        if fx["fix_class"] == "config" and fx.get("knob"):
            print(f"            knob: {fx['knob']['parameter']}")
            print(f"            move: {fx['knob']['direction']}")
        elif fx["fix_class"] == "engagement-control" and fx.get("pointer"):
            print(f"            -> {fx['pointer']['layer']}")
    print(f"  exit_code={env['exit_code']}")
    return env["exit_code"]


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


def _download(url: str, dest: str, headers: Optional[dict] = None, timeout: int = 120) -> str:
    data = _http_get(url, headers=headers, timeout=timeout)
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


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
    call = _http_get_json(
        f"{base_url.rstrip('/')}/call/{call_id}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=timeout,
    )
    artifact = call.get("artifact") or {}
    recording = artifact.get("recording") or {}
    # Current shape first, then the two deprecated legacy shapes.
    url = (
        recording.get("stereoUrl")
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
    call = _http_get_json(
        f"{base_url.rstrip('/')}/v2/get-call/{call_id}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=timeout,
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
    if stack not in STACKS:
        raise ValueError(f"unknown stack {stack!r}; choose one of {', '.join(STACKS)}")

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
