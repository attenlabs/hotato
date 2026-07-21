"""``hotato simulate --chat URL``: drive the EXISTING scripted deterministic
caller turn plan (``hotato.scenario.v1``) against YOUR chat agent over HTTP,
and write a timestamped transcript ``hotato investigate --transcript`` scores.

THE TINY HTTP CONTRACT (the whole wire protocol):

  * hotato POSTs one JSON object per scripted caller turn::

        {"conversation_id": "<id>", "turn_index": 0, "text": "<caller turn>"}

  * the agent answers ``200`` with a JSON object carrying the reply text::

        {"text": "<agent reply>"}

    Extra response keys are ignored. Anything else -- a non-200 status, a
    redirect, a non-JSON body, or a missing/non-string ``text`` -- is a
    contract violation raised as ``ValueError`` (the CLI's exit-2 path).

EGRESS: local by default. The URL must be http(s); a host other than
``localhost``/``127.0.0.1``/``::1`` is refused BEFORE any request is sent
unless ``--egress-opt-in`` is passed -- the same explicit gate the hosted
diarizer and the hosted judge carry. Redirects are refused outright, so a
local URL can never silently bounce the conversation off-box.

WHAT IS MODELED vs WHAT IS MEASURED (each labelled, never conflated):

  * turn SPANS are nominal pacing from the scenario's deterministic timing
    model -- the SAME word-count/speaking-rate constants
    :func:`hotato.simulate.render` uses -- so the caller side of the timeline
    derives from ``(scenario, seed)``, not from a wall clock;
  * agent reply LATENCY is the measured HTTP round trip of each turn, and it
    is what places each agent reply on the timeline (the response gap
    ``investigate --transcript`` scores IS that measured latency).

The produced transcript is EXACTLY the shape
:func:`hotato.transcript_input.load_transcript_segments` consumes (an object
with a ``segments`` list of ``{role, text, start, end}`` turns), plus
``origin.kind = "simulated"`` provenance: the caller side is the scripted
simulator's, never a real caller, and the agent ``text`` is your agent's own
reply recorded verbatim.

The driver only ever speaks as the CALLER (the scripted turns; it never
answers for the agent), and it drives the scripted plan ONCE -- backchannels
are an audio-timeline behavior, so none are injected as chat messages into
your agent's conversation.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from . import scenario as _scn
from .errors import TOOL as _TOOL
from .simulate import (
    _GAP_SEC,
    _LEAD_IN_SEC,
    _MIN_TURN_SEC,
    _SEC_PER_WORD,
    MODEL_ID,
    _canonical_json,
    _write_owned,
)

__all__ = [
    "CHAT_KIND",
    "TRANSCRIPT_FILENAME",
    "DEFAULT_TIMEOUT_SEC",
    "LOCAL_HOSTS",
    "check_chat_url",
    "drive_chat",
    "transcript_doc",
    "write_transcript",
]

# The envelope kind the CLI stamps on a chat-drive result.
CHAT_KIND = "simulate-chat"

# The transcript file written under --out (default .hotato/), named so the
# printed `hotato investigate --transcript <path>` next step reads as what it
# is.
TRANSCRIPT_FILENAME = "chat-transcript.json"

# Per-request timeout. A chat agent that takes longer than this per reply is
# unreachable for this driver's purposes; the timeout error is the usual
# handled OSError (exit 2), never a hang.
DEFAULT_TIMEOUT_SEC = 30.0

# The hosts a --chat URL may name WITHOUT --egress-opt-in (mirrors
# rubric._is_local_endpoint's loopback set for the local judge).
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def check_chat_url(url: str, *, egress_opt_in: bool = False) -> None:
    """Validate a ``--chat`` URL BEFORE any request is sent. Raises
    ``ValueError`` (the CLI's exit-2 usage path) when the scheme is not
    http(s), or when the host is off :data:`LOCAL_HOSTS` without an explicit
    ``egress_opt_in`` -- the same gate shape ``--diarizer pyannoteai`` and the
    hosted judge carry, checked before a single byte leaves the machine."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"--chat URL must be http(s), got {url!r}; the chat driver "
            "speaks plain HTTP to your agent"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"--chat URL has no host: {url!r}")
    if host not in LOCAL_HOSTS and not egress_opt_in:
        raise ValueError(
            f"--chat URL {url!r} is not local "
            f"({'/'.join(sorted(LOCAL_HOSTS))}). Driving a non-local chat "
            "agent sends your scenario's caller turns off this machine, so "
            "it needs an explicit --egress-opt-in. Refused before any "
            "request was sent."
        )


def _turn_duration_sec(text: str, rate: float) -> float:
    """Nominal duration of one turn: the SAME deterministic word-count pacing
    :func:`hotato.simulate.render` uses (never a wall clock), so the caller
    timeline derives from the scenario alone."""
    words = max(1, len(text.split()))
    return round(max(_MIN_TURN_SEC, words * _SEC_PER_WORD / rate), 3)


def _post_turn(url: str, payload: Dict[str, Any], timeout_sec: float) -> tuple:
    """POST one caller turn to the agent and return ``(reply_text,
    latency_ms)``. ``latency_ms`` is the MEASURED HTTP round trip (request
    sent to reply read), an integer of milliseconds. A non-200, a redirect, a
    non-JSON body, or a missing/non-string ``text`` raises ``ValueError``
    naming the contract; an unreachable agent raises the usual handled
    ``OSError`` (both the CLI's exit-2 path)."""
    import urllib.error
    import urllib.request

    contract = (
        'the --chat contract is: POST {"conversation_id", "turn_index", '
        '"text"} -> 200 with JSON {"text": "<reply>"}'
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
            return None  # a 3xx becomes an HTTPError below, never a hop off-box

    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"chat agent at {url} answered HTTP {exc.code} for turn "
            f"{payload['turn_index']}; {contract}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(
            f"could not reach the chat agent at {url}: {exc.reason}; start "
            f"your agent there, or point --chat at it. ({contract})"
        ) from exc
    latency_ms = max(0, int(round((time.perf_counter() - t0) * 1000)))

    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"chat agent at {url} answered a non-JSON body for turn "
            f"{payload['turn_index']}; {contract}"
        ) from exc
    text = doc.get("text") if isinstance(doc, dict) else None
    if not isinstance(text, str):
        raise ValueError(
            f"chat agent at {url} answered without a string 'text' for turn "
            f"{payload['turn_index']}; {contract}"
        )
    return text, latency_ms


def drive_chat(
    scenario: Dict[str, Any],
    seed: int,
    url: str,
    *,
    egress_opt_in: bool = False,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Drive ``scenario``'s scripted caller turn plan against the chat agent
    at ``url`` and return the timestamped result.

    One POST per scripted turn, in script order; each agent reply is recorded
    VERBATIM. The timeline is monotonic by construction: caller turn ``i``
    spans its nominal pacing duration, the agent reply starts after the
    MEASURED reply latency and spans the same nominal pacing over the reply
    text, and the next caller turn starts one gap later. Returns::

        {"scenario_id", "seed", "url", "conversation_id", "origin",
         "segments": [...], "turns": [...]}

    where ``segments`` is exactly the transcript shape ``hotato investigate
    --transcript`` consumes and ``turns`` carries the per-turn measured
    ``latency_ms``."""
    check_chat_url(url, egress_opt_in=egress_opt_in)
    doc = _scn.validate_scenario_doc(scenario)
    sid = doc["id"]
    seed = int(seed)
    behavior = doc["caller"].get("behavior") or {}
    rate = float(behavior.get("speaking_rate", _scn.DEFAULT_SPEAKING_RATE))
    conversation_id = f"{sid}-seed{seed}"

    segments: List[Dict[str, Any]] = []
    turns: List[Dict[str, Any]] = []
    cursor = _LEAD_IN_SEC
    for idx, turn in enumerate(doc["caller"]["script"]):
        text = turn["say"]
        c_start = round(cursor, 3)
        c_end = round(c_start + _turn_duration_sec(text, rate), 3)
        segments.append({"role": "caller", "text": text, "start": c_start,
                         "end": c_end, "kind": "scripted"})

        reply, latency_ms = _post_turn(
            url,
            {"conversation_id": conversation_id, "turn_index": idx,
             "text": text},
            timeout_sec,
        )
        a_start = round(c_end + latency_ms / 1000.0, 3)
        a_end = round(a_start + _turn_duration_sec(reply, rate), 3)
        segments.append({"role": "agent", "text": reply, "start": a_start,
                         "end": a_end, "kind": "chat-reply"})
        turns.append({
            "turn_index": idx,
            "sent": text,
            "reply": reply,
            "latency_ms": latency_ms,
            "caller_start": c_start, "caller_end": c_end,
            "agent_start": a_start, "agent_end": a_end,
        })
        cursor = round(a_end + _GAP_SEC, 3)

    return {
        "scenario_id": sid,
        "seed": seed,
        "url": url,
        "conversation_id": conversation_id,
        # The caller side is the scripted simulator's -- never a real caller;
        # the agent text is YOUR agent's own replies, recorded verbatim over
        # HTTP, named as such right here in the provenance.
        "origin": {
            "kind": "simulated",
            "simulator": {"model_id": MODEL_ID, "scenario_id": sid,
                          "seed": seed},
            "agent_replies": "live-chat-http",
        },
        "segments": segments,
        "turns": turns,
    }


def transcript_doc(result: Dict[str, Any]) -> Dict[str, Any]:
    """The transcript FILE document for a :func:`drive_chat` result: an object
    with a ``segments`` list -- exactly what
    :func:`hotato.transcript_input.load_transcript_segments` reads -- plus the
    ``origin=simulated`` provenance and the per-turn measured latencies."""
    return {
        "tool": _TOOL,
        "kind": "chat-transcript",
        "schema_version": "1",
        "origin": result["origin"],
        "chat": {
            "url": result["url"],
            "transport": "http",
            "conversation_id": result["conversation_id"],
            # measured HTTP round trip per scripted turn, in ms
            "agent_latency_ms": [t["latency_ms"] for t in result["turns"]],
        },
        "segments": result["segments"],
    }


def write_transcript(result: Dict[str, Any], out_dir: str) -> str:
    """Write the transcript for a :func:`drive_chat` result into
    ``out_dir/chat-transcript.json`` (creating ``out_dir``) and return the
    path -- the file ``hotato investigate --transcript`` consumes as-is."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, TRANSCRIPT_FILENAME)
    _write_owned(path, _canonical_json(transcript_doc(result)))
    return path
