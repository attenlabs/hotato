"""``hotato.drive``: originate a REAL call against a live voice agent, then feed
the resulting recording straight into the existing capture -> score pipeline.

Capture (``hotato.capture``) scores a call you ALREADY ran. Drive-a-call closes
the other half: it PLACES the call, waits for it to finish, and hands the
recording to the same validated pull path. The produced conversation is a REAL
agent conversation -- ``origin.kind == "real"`` with the provider and the
provider's own call id -- so it flows through the normal scoring/artifact
pipeline unchanged. What was SCRIPTED is only the caller side, and the origin
records that honestly (``origin.caller``) without ever claiming a human placed
the call.

Two providers, two honest directions:

* **Twilio** (:func:`place_call_twilio`) -- a FIXED-TIMELINE scripted caller.
  The scenario.v1 caller script is rendered to TwiML: one ``<Say>`` per
  ``say``-turn, ``<Pause>`` between them. The call is placed FROM your Twilio
  number TO the agent's number, recorded dual-channel. Because TwiML ``<Say>``
  speaks at fixed offsets and CANNOT react to what the agent says, this is a
  regression driver -- deterministic and useful for catching a regression on a
  scripted turn sequence -- not a reactive caller. A caller that barges in when
  the agent actually starts talking is later work; this one speaks on a clock.

* **Vapi** (:func:`place_call_vapi`) -- originates a call FROM the assistant
  (the agent under test, e.g. a staging clone) TO a customer number. The
  direction is outbound-from-assistant: the assistant is the party we are
  measuring, and whoever/whatever answers the customer number is the other side.
  There is no scripted-TwiML caller on this path; the origin records
  ``caller: "assistant-originated"`` so the provenance is not overstated.

EGRESS + gating: placing a call reaches the provider's REST API and costs a real
phone call. Nothing here runs on an import; every network call needs real
credentials AND an explicit egress opt-in (see :mod:`hotato.fleet.adapters`'s
``run_scenario`` gate). The recording download reuses ``capture``'s validated
path (scheme allowlist, default-deny SSRF, cross-host credential strip, atomic
write); a local test recording server on ``127.0.0.1`` requires the documented
``HOTATO_ALLOW_PRIVATE_URLS=1`` opt-out, same as every other download here.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, Optional

from . import capture as _cap
from . import scenario as _scn

__all__ = [
    "render_twiml",
    "place_call_twilio",
    "place_call_vapi",
    "TWILIO_TERMINAL_STATES",
    "VAPI_TERMINAL_STATES",
]

# --- fixed-timeline pacing (mirrors hotato.simulate's deterministic model) ---
# TwiML <Pause length> is whole seconds only, so these are the coarse, honest
# counterpart of the simulator's sub-second timeline -- a fixed clock, never a
# reaction to the agent.
_LEAD_IN_SEC = 0.5   # a short lead-in before the first caller turn
_GAP_SEC = 0.5       # silence between consecutive caller turns

# Twilio Call.status values that mean the call is over (Twilio REST voice docs).
# Only "completed" yields a recording to score; the rest are honest dead-ends.
TWILIO_TERMINAL_STATES = ("completed", "busy", "failed", "no-answer", "canceled")
# Vapi Call.status values that mean the call has finished (Vapi call lifecycle).
VAPI_TERMINAL_STATES = ("ended",)


# =========================================================================
# TwiML rendering (the fixed-timeline scripted caller)
# =========================================================================

def _xml_escape(text: str) -> str:
    """Escape text for an XML element body. The caller ``say`` text is
    operator-authored, but it can still contain ``&``, ``<`` or quotes that would
    otherwise produce malformed TwiML Twilio rejects -- escape the full set so any
    scenario string renders as valid TwiML."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _pause_len(seconds: float) -> int:
    """Whole-second TwiML ``<Pause length>`` for a sub-second gap, rounded UP so a
    0.5s gap still yields a real 1s pause (a 0-length pause would be dropped)."""
    return max(0, math.ceil(seconds))


def render_twiml(
    scenario: Dict[str, Any],
    *,
    voice: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """Render a ``scenario.v1`` caller script into TwiML for a FIXED-TIMELINE
    scripted caller.

    Each ``caller.script`` ``say``-turn becomes one ``<Say>`` element; a
    ``<Pause>`` is inserted as a lead-in and between consecutive turns. A turn may
    carry an explicit ``pause_before_ms`` (whole-second-rounded) to override the
    default inter-turn gap for coarse pacing control.

    HONESTY: this is a FIXED-TIMELINE caller. ``<Say>`` speaks at fixed offsets
    and cannot react to the agent, so a turn's ``when_agent_asks`` / ``after``
    label triggers -- which are reactive by definition -- are NOT honored here;
    every turn is spoken unconditionally in order. That makes this a deterministic
    regression driver, not a reactive caller. The scenario is validated
    (``ValueError`` on anything malformed, exit-2 usage-error path) BEFORE any
    TwiML is built, mirroring the rest of the scenario contract."""
    doc = _scn.validate_scenario_doc(scenario)
    caller = doc["caller"]
    script = caller["script"]

    say_attrs = ""
    if voice:
        say_attrs += f' voice="{_xml_escape(voice)}"'
    if language:
        say_attrs += f' language="{_xml_escape(language)}"'

    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    lead = _pause_len(_LEAD_IN_SEC)
    if lead:
        parts.append(f'<Pause length="{lead}"/>')
    for idx, turn in enumerate(script):
        if idx > 0:
            explicit = turn.get("pause_before_ms")
            gap_sec = (
                int(explicit) / 1000.0
                if isinstance(explicit, int) and not isinstance(explicit, bool)
                and explicit >= 0
                else _GAP_SEC
            )
            gap = _pause_len(gap_sec)
            if gap:
                parts.append(f'<Pause length="{gap}"/>')
        parts.append(f"<Say{say_attrs}>{_xml_escape(turn['say'])}</Say>")
    parts.append("</Response>")
    return "".join(parts)


# =========================================================================
# origin provenance (invariant 5: real is never conflated with simulated)
# =========================================================================

def _real_origin(provider: str, provider_call_id: str, caller: str,
                 **extra: Any) -> Dict[str, Any]:
    """The ``origin`` block for a driven call: ``kind == "real"`` (a real agent
    conversation), the ``provider`` and its ``provider_call_id``, and a ``caller``
    field recording WHAT drove the caller side -- ``"scripted-twiml"`` for the
    fixed-timeline Twilio caller, ``"assistant-originated"`` for a Vapi call the
    assistant placed. It never claims the caller was human. Suitable to pass as
    ``origin=`` to :func:`hotato.conversation.build_manifest` (kind ``"real"``
    needs no simulator block)."""
    origin = {
        "kind": "real",
        "provider": provider,
        "provider_call_id": str(provider_call_id),
        "caller": caller,
    }
    origin.update({k: v for k, v in extra.items() if v is not None})
    return origin


# =========================================================================
# Twilio: place a scripted call and pull its dual-channel recording
# =========================================================================

def _basic_auth(account_sid: str, auth_token: str) -> Dict[str, str]:
    import base64

    token = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _poll(fetch, is_done, *, poll_interval: float, max_wait: float, what: str):
    """Poll ``fetch`` until ``is_done`` returns True or ``max_wait`` elapses,
    sleeping ``poll_interval`` between attempts. Returns the last fetched value.
    Raises a clean ``TimeoutError`` (never hangs unbounded) when the deadline is
    hit -- a real call that never finishes must not block the runner forever."""
    deadline = time.monotonic() + max_wait
    value = fetch()
    while not is_done(value):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {max_wait:.0f}s waiting for {what}; the last "
                "observed value did not reach a terminal state"
            )
        if poll_interval > 0:
            time.sleep(poll_interval)
        value = fetch()
    return value


def place_call_twilio(
    scenario: Dict[str, Any],
    *,
    to_number: str,
    from_number: str,
    sid: str,
    token: str,
    base_url: str = "https://api.twilio.com",
    voice: Optional[str] = None,
    language: Optional[str] = None,
    timeout: int = 30,
    poll_interval: float = 3.0,
    max_wait: float = 300.0,
    out_path: Optional[str] = None,
    allow_mono: bool = False,
) -> Dict[str, Any]:
    """Originate a Twilio call that plays the scenario's FIXED-TIMELINE scripted
    caller AT the agent, record it dual-channel, and return the pulled recording.

    Flow (all against ``base_url``, Twilio Basic auth ``AccountSid:AuthToken``):
      1. Render the ``scenario.v1`` caller script to TwiML (:func:`render_twiml`).
      2. ``POST /2010-04-01/Accounts/{sid}/Calls.json`` with ``To`` (the agent's
         number), ``From`` (your Twilio number), ``Twiml`` (the rendered script),
         and dual-channel recording enabled -- ``Record=true``,
         ``RecordingChannels=dual`` (the REST-origination equivalent of
         ``<Dial record="record-from-answer-dual">``), so a 2-channel file exists
         to score, caller on one channel and agent on the other.
      3. Poll ``GET .../Calls/{CallSid}.json`` until the call reaches a terminal
         state; only ``completed`` yields a recording (a busy/failed/no-answer/
         canceled call is an honest dead-end, not a score).
      4. Poll ``GET .../Recordings.json?CallSid={CallSid}`` until the recording is
         available, then feed its ``RecordingSid`` into the EXISTING
         :func:`hotato.capture.capture_twilio` pull path (which fetches the
         ``?RequestedChannels=2`` media through the validated download).

    Returns ``{recording, provider, provider_call_id, recording_sid, status,
    origin}`` where ``origin.kind == "real"`` and ``origin.caller ==
    "scripted-twiml"``. Placing the call reaches Twilio and costs a real call --
    this is credential- and egress-gated at the ``run_scenario`` layer."""
    twiml = render_twiml(scenario, voice=voice, language=language)
    auth = _basic_auth(sid, token)
    root = f"{base_url.rstrip('/')}/2010-04-01/Accounts/{sid}"

    form = {
        "To": to_number,
        "From": from_number,
        "Twiml": twiml,
        "Record": "true",
        "RecordingChannels": "dual",
        "RecordingTrack": "both",
    }
    created = _cap._require_json_object(
        _cap._http_post_form(f"{root}/Calls.json", form, headers=auth, timeout=timeout),
        "Twilio create-call response",
    )
    call_sid = created.get("sid")
    if not call_sid:
        raise ValueError(
            "Twilio create-call response carried no call 'sid'; cannot track the "
            "call to completion"
        )

    def _fetch_call():
        return _cap._require_json_object(
            _cap._http_get_json(f"{root}/Calls/{call_sid}.json", headers=auth,
                                timeout=timeout),
            f"Twilio call {call_sid!r}",
        )

    call = _poll(
        _fetch_call,
        lambda c: c.get("status") in TWILIO_TERMINAL_STATES,
        poll_interval=poll_interval, max_wait=max_wait,
        what=f"Twilio call {call_sid} to finish",
    )
    status = call.get("status")
    if status != "completed":
        raise ValueError(
            f"Twilio call {call_sid} ended with status {status!r}, not "
            "'completed'; there is no recording to score. (busy/failed/no-answer/"
            "canceled are real outcomes, not agent verdicts.)"
        )

    def _fetch_recordings():
        data = _cap._require_json_object(
            _cap._http_get_json(f"{root}/Recordings.json?CallSid={call_sid}",
                                headers=auth, timeout=timeout),
            f"Twilio recordings for call {call_sid!r}",
        )
        recs = data.get("recordings")
        return recs if isinstance(recs, list) else []

    recs = _poll(
        _fetch_recordings, lambda r: bool(r),
        poll_interval=poll_interval, max_wait=max_wait,
        what=f"Twilio recording for call {call_sid} to be available",
    )
    recording_sid = recs[0].get("sid") if isinstance(recs[0], dict) else None
    if not recording_sid:
        raise ValueError(
            f"Twilio call {call_sid} completed but its recording carried no 'sid'"
        )

    recording = _cap.capture_twilio(
        recording_sid=recording_sid, account_sid=sid, auth_token=token,
        out_path=out_path, base_url=base_url, timeout=timeout, allow_mono=allow_mono,
    )
    return {
        "recording": recording,
        "provider": "twilio",
        "provider_call_id": call_sid,
        "recording_sid": recording_sid,
        "status": status,
        "origin": _real_origin(
            "twilio", call_sid, "scripted-twiml", recording_sid=recording_sid,
            direction="inbound-to-agent",
        ),
    }


# =========================================================================
# Vapi: originate a call FROM the assistant and pull its stereo recording
# =========================================================================

def _assistant_id(scenario_or_assistant: Any) -> str:
    """Resolve the assistant/clone id to drive. Accepts a plain id string or a
    clone dict from ``apply_variant`` (``clone_id`` / ``id`` / ``assistantId``)."""
    if isinstance(scenario_or_assistant, str):
        return scenario_or_assistant
    if isinstance(scenario_or_assistant, dict):
        for key in ("clone_id", "assistantId", "assistant_id", "id"):
            val = scenario_or_assistant.get(key)
            if val:
                return str(val)
    raise ValueError(
        "place_call_vapi needs an assistant id (a string) or a clone dict "
        "carrying one (clone_id/assistantId/id)"
    )


def place_call_vapi(
    scenario_or_assistant: Any,
    *,
    phone_number_id: str,
    customer_number: str,
    api_key: str,
    base_url: str = "https://api.vapi.ai",
    timeout: int = 30,
    poll_interval: float = 3.0,
    max_wait: float = 300.0,
    out_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Originate a Vapi call FROM the assistant (the agent under test -- typically
    a staging clone) TO ``customer_number``, wait for it to end, and return the
    pulled stereo recording.

    Flow (Vapi Bearer auth, against ``base_url``):
      1. ``POST /call`` with ``{assistantId, phoneNumberId, customer:{number}}``
         -> the created Call object with its ``id``.
      2. Poll ``GET /call/{id}`` until ``status == "ended"``.
      3. Feed the call id into the EXISTING
         :func:`hotato.capture.capture_vapi` download, which reads
         ``artifact.recording.stereoUrl`` and fetches it through the validated
         download path.

    DIRECTION (documented plainly): this is an OUTBOUND call the assistant places
    -- the assistant is the measured party, and whoever/whatever answers
    ``customer_number`` is the other side. There is no scripted-TwiML caller on
    this path, so the returned ``origin.caller`` is ``"assistant-originated"``,
    not ``"scripted-twiml"``. ``origin.kind == "real"``.

    Returns ``{recording, provider, provider_call_id, status, origin}``. Placing
    the call reaches Vapi and costs a real call -- credential- and egress-gated at
    the ``run_scenario`` layer."""
    assistant_id = _assistant_id(scenario_or_assistant)
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    root = base_url.rstrip("/")

    created = _cap._require_json_object(
        _cap._http_post_json(
            f"{root}/call",
            {"assistantId": assistant_id, "phoneNumberId": phone_number_id,
             "customer": {"number": customer_number}},
            headers=headers, timeout=timeout,
        ),
        "Vapi create-call response",
    )
    call_id = created.get("id")
    if not call_id:
        raise ValueError(
            "Vapi create-call response carried no call 'id'; cannot track the "
            "call to completion"
        )

    def _fetch_call():
        return _cap._require_json_object(
            _cap._http_get_json(f"{root}/call/{call_id}", headers=headers,
                                timeout=timeout),
            f"Vapi call {call_id!r}",
        )

    call = _poll(
        _fetch_call,
        lambda c: c.get("status") in VAPI_TERMINAL_STATES,
        poll_interval=poll_interval, max_wait=max_wait,
        what=f"Vapi call {call_id} to end",
    )
    # Reuse the verified capture path: it re-reads the call and downloads the
    # stereo artifact through the validated (scheme/SSRF/atomic) download.
    recording = _cap.capture_vapi(
        call_id=call_id, api_key=api_key, out_path=out_path, base_url=base_url,
        timeout=max(timeout, 60),
    )
    return {
        "recording": recording,
        "provider": "vapi",
        "provider_call_id": call_id,
        "status": call.get("status"),
        "origin": _real_origin(
            "vapi", call_id, "assistant-originated",
            direction="outbound-from-assistant", assistant_id=assistant_id,
        ),
    }
