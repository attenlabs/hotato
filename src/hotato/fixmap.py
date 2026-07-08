"""Turn a failing barge-in event into an actionable, honest fix.

Every failing event is labelled with exactly one ``fix_class``:

* ``config``            - a concrete, vendor-specific knob you can turn today.
                          No upsell. The knob is named and the direction to
                          move it is stated, with an honest note about the
                          trade-off it makes.
* ``engagement-control``- the failure is a *discrimination* problem (telling a
                          genuine bid for the floor apart from a backchannel or
                          side-speech that is not addressed to the agent). No
                          single timing threshold separates them, so this points
                          at the *kind* of fix the failure needs: where the stack
                          provides an interruption/backchannel classifier, use
                          it; the general case calls for a learned
                          engagement-control / addressee-detection layer. The
                          pointer is VENDOR-NEUTRAL and deliberately high level:
                          it names the problem class and the kind of fix, names
                          no product, vendor, or component you can adopt, and
                          carries no numbers or accuracy claims.

The routing is deterministic and inspectable. Nothing here fabricates a metric.
"""

from __future__ import annotations

from typing import Optional


# --- vendor-specific knob catalogue --------------------------------------
#
# These parameter names are the knobs each stack exposes for interruption /
# turn-taking behaviour. Names and defaults drift between versions, so every
# suggestion is framed as "verify against your installed version". We never
# claim a specific value fixes a specific recording; we name the dial and the
# direction, and we state the trade-off honestly.

_KNOBS = {
    "livekit": {
        "more_sensitive": {
            "parameter": "AgentSession(turn_handling=TurnHandlingOptions(...)): interruption.min_duration / interruption.min_words (prior-generation flat kwargs: min_interruption_duration / min_interruption_words)",
            "direction": "lower interruption.min_duration and interruption.min_words so a real interruption registers sooner",
            "note": "makes the agent cut off sooner, but a lower words threshold also lets short backchannels trigger a yield.",
        },
        "faster_yield": {
            "parameter": "turn_handling: interruption.min_duration + endpointing.min_delay / endpointing.max_delay",
            "direction": "lower interruption.min_duration and endpointing.min_delay so the agent stops speaking sooner after the caller takes the floor",
            "note": "reducing endpointing latency can increase spurious cut-offs on noisy lines.",
        },
        "less_talk_over": {
            "parameter": "turn_handling: interruption.min_duration",
            "direction": "lower it so overlapping speech ends the agent turn faster",
            "note": "too low and normal listener noise will clip the agent mid-word.",
        },
        "suppress_false_trigger": {
            "parameter": "turn_handling: interruption.min_words + interruption.false_interruption_timeout / interruption.mode",
            "direction": "raise interruption.min_words so a lone 'mhm'/'okay' leaves the agent holding the floor; set interruption.false_interruption_timeout so a false trigger resumes the agent",
            "note": "HONEST TRADE-OFF: raising this also delays or drops genuine one-word interruptions ('stop', 'no'). One dial cannot win both cases.",
        },
        "echo": {
            "parameter": "input echo cancellation / track routing",
            "direction": "confirm agent TTS is not mixed into the input track; enable AEC or keep caller and agent on separate channels",
            "note": "phantom self-interruption is usually an audio-routing bug, not a policy bug.",
        },
    },
    "pipecat": {
        "more_sensitive": {
            "parameter": "PipelineTask(turn_start_strategies=[VADUserTurnStartStrategy(...)]) + VADParams(start_secs, stop_secs, confidence)",
            "direction": "lower stop_secs / start_secs and the VAD confidence so speech is detected sooner",
            "note": "lower confidence catches more real interruptions but also more noise-triggered ones.",
        },
        "faster_yield": {
            "parameter": "SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...) + VADParams(stop_secs)",
            "direction": "lower user_speech_timeout and stop_secs so the turn boundary is detected sooner",
            "note": "very low values fragment a single utterance into several turns.",
        },
        "less_talk_over": {
            "parameter": "VADParams(stop_secs) + turn_start_strategies interruption handling",
            "direction": "lower stop_secs and keep an interruption-capable turn start strategy (VADUserTurnStartStrategy) active",
            "note": "aggressive settings can clip the agent on brief overlaps.",
        },
        "suppress_false_trigger": {
            "parameter": "PipelineTask(turn_start_strategies=[MinWordsUserTurnStartStrategy(min_words=N)]) (replaces MinWordsInterruptionStrategy, deprecated since Pipecat 0.0.99)",
            "direction": "require N words before a turn start counts, so backchannels leave the agent speaking",
            "note": "HONEST TRADE-OFF: a higher min_words also swallows short but real interruptions. One threshold cannot separate 'mhm' from 'stop'.",
        },
        "echo": {
            "parameter": "input/output track separation + AEC",
            "direction": "run caller and agent on separate audio tracks and enable echo cancellation on input",
            "note": "self-interruption from bot audio bleed is an I/O routing fix, not a turn-taking policy fix.",
        },
    },
    "vapi": {
        "more_sensitive": {
            "parameter": "startSpeakingPlan.smartEndpointingPlan + stopSpeakingPlan.numWords",
            "direction": "lower stopSpeakingPlan.numWords and stopSpeakingPlan.backoffSeconds",
            "note": "lower numWords reacts faster but also lets a single backchannel word interrupt.",
        },
        "faster_yield": {
            "parameter": "stopSpeakingPlan.backoffSeconds / smartEndpointingPlan",
            "direction": "lower backoffSeconds so the agent stops sooner once the caller speaks",
            "note": "very low backoff can cut the agent off on filler noise.",
        },
        "less_talk_over": {
            "parameter": "stopSpeakingPlan.numWords / voiceSeconds",
            "direction": "lower voiceSeconds so overlap ends the agent turn sooner",
            "note": "aggressive values clip the agent during normal double-talk.",
        },
        "suppress_false_trigger": {
            "parameter": "stopSpeakingPlan.numWords (raise) / acknowledgementPhrases",
            "direction": "raise numWords and/or list backchannel words as acknowledgementPhrases so they do not interrupt",
            "note": "HONEST TRADE-OFF: a hand-maintained phrase list and a higher word count also miss genuine short interruptions and never generalise to new phrasings.",
        },
        "echo": {
            "parameter": "backgroundDenoisingEnabled / stopSpeakingPlan",
            "direction": "enable input denoising and confirm the model output is not fed back into the input",
            "note": "phantom interruptions from bot audio are a routing/denoising fix.",
        },
    },
    "generic": {
        "more_sensitive": {
            "parameter": "interruption sensitivity (VAD min-silence, min-interruption-duration, min-words-to-interrupt)",
            "direction": "lower the min-silence and min-duration thresholds so a real interruption registers sooner",
            "note": "sliding sensitivity up catches more real interruptions and more false ones together.",
        },
        "faster_yield": {
            "parameter": "endpointing / VAD min-silence-duration",
            "direction": "lower the min-silence and hangover so the agent goes quiet sooner after the caller takes the floor",
            "note": "reducing latency here trades against stability on noisy audio.",
        },
        "less_talk_over": {
            "parameter": "min-interruption-duration / overlap debounce",
            "direction": "lower it so sustained overlap cuts the agent turn faster",
            "note": "too low clips the agent on ordinary listener noise.",
        },
        "suppress_false_trigger": {
            "parameter": "min-words-to-interrupt / backchannel filter",
            "direction": "raise the words-to-interrupt threshold so short acknowledgements do not take the floor",
            "note": "HONEST TRADE-OFF: the same threshold that ignores 'mhm' also ignores 'stop'. A single dial cannot separate a backchannel from a one-word interruption.",
        },
        "echo": {
            "parameter": "echo cancellation / channel isolation",
            "direction": "enable AEC on the input and keep caller and agent audio on separate channels",
            "note": "self-interruption from bot audio bleed is an audio-routing fix.",
        },
    },
}


# High-level, numbers-free, VENDOR-NEUTRAL pointer for the engagement-control
# fix class. This text ships in the machine output, so it names the PROBLEM CLASS
# (discriminating a genuine floor-bid from a backchannel / speech not addressed
# to the agent) and the KIND of fix that class needs: where the stack provides
# an interruption/backchannel classifier, use it; the general case calls for a
# learned engagement-control / addressee-detection layer. It deliberately names
# NO vendor, NO product, and nothing you can adopt, license, or buy. No accuracy
# figures, no numbers, no learn-more link - so the pointer can never read as
# lead-gen.
ENGAGEMENT_CONTROL_POINTER = {
    "layer": "a learned engagement-control / addressee-detection layer",
    "what": (
        "This is a discrimination problem, not a threshold problem: telling a "
        "genuine bid for the floor apart from a backchannel or speech that was "
        "not addressed to the agent. No single timing threshold separates them "
        "- you can raise a words-to-interrupt threshold, but the same threshold "
        "that ignores 'mhm' also ignores 'stop'. Separating them needs a signal "
        "for 'is this speech addressed to me, and is it a real bid for the "
        "floor' - not a config knob."
    ),
    "honest_scope": (
        "No single timing threshold separates them. Where your stack provides "
        "an interruption/backchannel classifier, use it; the general case calls "
        "for a learned engagement-control / addressee-detection layer. The "
        "audio-only turn-taking case shown here is the hardest modality for it. "
        "Treat this as a pointer to the KIND of fix the failure needs, not a "
        "benchmarked claim: bring your own recordings and measure."
    ),
}


def _stack_knobs(stack: Optional[str]) -> dict:
    key = (stack or "generic").strip().lower()
    return _KNOBS.get(key, _KNOBS["generic"])


def _config_fix(stack: Optional[str], intent: str, title: str, detail: str) -> dict:
    knob = _stack_knobs(stack)[intent]
    return {
        "fix_class": "config",
        "title": title,
        "detail": detail,
        "knob": {
            "stack": (stack or "generic").strip().lower(),
            "parameter": knob["parameter"],
            "direction": knob["direction"],
            "trade_off": knob["note"],
        },
        "pointer": None,
    }


def _engagement_fix(title: str, detail: str) -> dict:
    return {
        "fix_class": "engagement-control",
        "title": title,
        "detail": detail,
        "knob": None,
        "pointer": ENGAGEMENT_CONTROL_POINTER,
    }


def classify_event(
    *,
    expected_yield: bool,
    did_yield: bool,
    reasons: list,
    stack: Optional[str] = None,
    tags: Optional[list] = None,
    category: Optional[str] = None,
    scenario_id: Optional[str] = None,
    echo_suspected: bool = False,
) -> Optional[dict]:
    """Return a fix dict for a failing event, or None if the event passed.

    Routing (deterministic):

    * missed a real interruption (should yield, did not)      -> config: more sensitive
    * yielded but too slowly                                  -> config: faster yield
    * yielded but talked over too long                        -> config: less talk-over
    * false / phantom barge-in from bot audio bleed (echo)    -> config: fix audio routing
    * false barge-in on a backchannel / not-addressed speech  -> engagement-control
    """
    if not reasons:
        return None

    tags = tags or []
    joined = " ".join(reasons).lower()
    # The MEASURED cross-channel echo signal is authoritative and comes first: on
    # the flagship `hotato run --stereo` / `hotato capture` paths there is no
    # scenario_id and no tags, so without this a 100%-certain measured self-echo
    # was always mis-routed to the engagement-control pointer. Curator id/tag
    # substrings remain a fallback for label-only scenarios that carry no audio.
    is_echo = (
        bool(echo_suspected)
        or "echo" in (scenario_id or "").lower()
        or "echo" in tags
        or "aec" in tags
    )

    # Case A: the agent should have kept the floor but yielded.
    if not expected_yield and did_yield:
        if is_echo:
            return _config_fix(
                stack,
                "echo",
                "Phantom self-interruption: the agent yielded to its own audio",
                "The agent gave up the floor when no caller actually took it. This is "
                "almost always the bot's own output bleeding into the input track, not a "
                "turn-taking policy problem. Fix the audio path first.",
            )
        # Backchannel / not-addressed speech treated as a floor bid.
        return _engagement_fix(
            "False barge-in: a backchannel was treated as a bid for the floor",
            "The caller only signalled 'I'm listening' (mhm / right / okay) but the agent "
            "stopped mid-thought. You can raise a words-to-interrupt threshold, but the "
            "same threshold that ignores 'mhm' will also ignore 'stop'. This is a "
            "discrimination problem, not a threshold problem.",
        )

    # Case B: the agent should have yielded.
    if expected_yield and not did_yield:
        return _config_fix(
            stack,
            "more_sensitive",
            "Missed interruption: the agent kept talking over the caller",
            "The caller took the floor and the agent never stopped within the search "
            "window. Increase interruption sensitivity so a genuine floor-taking event "
            "registers.",
        )

    # Case C: yielded, but out of bounds.
    if "slower" in joined or "time_to_yield" in joined or "yielded in" in joined:
        return _config_fix(
            stack,
            "faster_yield",
            "Slow yield: the agent stopped, but too late",
            "The agent did yield, but the latency from the caller's onset to the agent "
            "going quiet exceeded the bound. Reduce endpointing / min-silence latency.",
        )
    if "talked over" in joined or "talk_over" in joined or "talk-over" in joined:
        return _config_fix(
            stack,
            "less_talk_over",
            "Excess talk-over: too many overlapping seconds before the agent yielded",
            "The agent eventually yielded but spoke over the caller for longer than the "
            "bound. Tighten the overlap/interruption debounce so it cuts sooner.",
        )

    # Fallback: unclassified failure -> most conservative config nudge.
    return _config_fix(
        stack,
        "more_sensitive",
        "Barge-in bound not met",
        "The event did not meet its expected turn-taking behaviour. Review interruption "
        "sensitivity for this stack.",
    )


def event_is_echo(event: dict) -> bool:
    """Whether an event is a self-echo / phantom self-interruption rather than a
    genuine backchannel false-barge. The MEASURED cross-channel coherence signal
    (``signals.echo.echo_suspected``) is authoritative; a curator ``scenario_id``
    substring is only a fallback for label-only scenarios with no audio. Keeping
    the two axes on the same rule is what stops the both-axes funnel from firing
    on a proven self-echo (which is an audio-routing bug, not a discrimination
    problem)."""
    echo = (event.get("signals") or {}).get("echo") or {}
    if echo.get("echo_suspected"):
        return True
    return "echo" in (event.get("scenario_id") or "").lower()


def systemic_pointer(events: list) -> Optional[dict]:
    """Battery-level funnel signal.

    If the battery contains BOTH a missed real interruption (should-yield that
    did not) AND a false barge-in on a backchannel (should-not-yield that did),
    then no single sensitivity setting can satisfy both cases at once. That is
    the honest, strongest case for a discriminating engagement-control layer.
    Returns a high-level, numbers-free pointer, or None.
    """
    missed_real = any(
        (not e["verdict"]["passed"])
        and e["expected_yield"]
        and not e["verdict"]["did_yield"]
        for e in events
    )
    false_barge = any(
        (not e["verdict"]["passed"])
        and (not e["expected_yield"])
        and e["verdict"]["did_yield"]
        and not event_is_echo(e)
        for e in events
    )
    if missed_real and false_barge:
        return {
            "reason": (
                "This battery fails on BOTH axes at once: it missed a genuine "
                "interruption AND it false-triggered on a backchannel. No single "
                "sensitivity threshold can fix both - turning it up to catch the "
                "interruption makes the backchannel worse, and vice versa. That is "
                "the signal that the agent needs a discriminating layer, not a "
                "different threshold."
            ),
            "pointer": ENGAGEMENT_CONTROL_POINTER,
        }
    return None
