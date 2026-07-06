#!/usr/bin/env python3
"""Build the vapi-defaults real-call example set (corpus/vapi-defaults/).

Source material: 12 dual-channel WAV recordings (44.1 kHz, caller on channel
0, agent on channel 1) of the operator calling a scripted Vapi assistant
("hotato-probe", model openai/gpt-4o, voice vapi/Elliot) whose interruption
settings were left at the Vapi DEFAULTS (stopSpeakingPlan and
startSpeakingPlan unset). One human caller working from a written script, one
production voice agent, recorded 2026-07-06 via Vapi's own dual-channel
artifact recording. The full recordings are NOT distributed with this
repository (about 109 MB); this script cuts the scored moments into small
committed clips. Pass --data DIR to point at the recordings.

What this script does, deterministically:
  1. sha256-verifies every source recording it reads (pinned below);
  2. for each scored moment, cuts a window around the caller onset from BOTH
     channels, linearly resamples 44100 -> 16000 Hz, and writes one
     two-channel 16-bit WAV per moment into audio/;
  3. scores the moment twice with the public API (hotato.core.run_single):
     once on the FULL recording at the derived onset, once on the committed
     clip at the rebased onset, and records both in manifest.json;
  4. writes one label JSON per battery scenario into scenarios/;
  5. --check rebuilds everything into a temp dir and byte-compares.

Onset provenance: every caller onset below was derived from the engine's own
energy-VAD tracks (the same public ScoreConfig defaults the scorer uses): the
helper listed every sustained caller-active run whose start falls inside the
engine's agent-activity lookback window (caller onsets DURING agent speech),
and the moment matching the script notes was chosen. The chosen time, the
candidates, and the reasoning are recorded per clip in manifest.json. These
are timing facts from the audio, not model judgements.

Usage:
    python3 corpus/vapi-defaults/build_vapi_defaults.py --data /path/to/recordings
    python3 corpus/vapi-defaults/build_vapi_defaults.py --data /path/to/recordings --check
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import shutil
import sys
import tempfile
import wave

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

SOURCE_RATE = 44100
TARGET_RATE = 16000
DEFAULT_DATA = os.environ.get(
    "HOTATO_VAPI_RECORDINGS", os.path.expanduser("~/Projects/hotato-recordings/data")
)

# Uniform caller-experience bounds for should_yield scenarios, stated once and
# applied to every yield scenario identically (no per-clip tuning): after a
# genuine attempt to take the turn, the agent should be quiet within 1.0 s and
# should not talk over the caller for more than 1.0 s.
MAX_TIME_TO_YIELD_SEC = 1.0
MAX_TALK_OVER_SEC = 1.0

PROVENANCE = {
    "assistant_name": "hotato-probe",
    "assistant_id": "37995a11-3b7e-41e7-87b5-198f08b6161a",
    "model": "openai/gpt-4o",
    "voice": "vapi/Elliot",
    "recorded": "2026-07-06",
    "interruption_settings": "Vapi defaults (stopSpeakingPlan and startSpeakingPlan unset)",
    "recording": "Vapi artifactPlan.recordingEnabled=true, dual-channel stereo, 44.1 kHz",
    "channel_map": {"caller": 0, "agent": 1},
}

# Pinned sha256 of every source recording this script reads (from the
# recording session's own manifest, re-verified at build time).
SOURCES = {
    "01-hard-interruption.wav": {
        "sha256": "a763be90faf4fbebff4e5228f42eb58dba23d0bee2c6bc03b4a153c994816bfe",
        "call_id": "019f3905-f5cb-7991-a4b3-e45b8606b631",
        "duration_sec": 32.0,
    },
    "02-one-word-stop.wav": {
        "sha256": "fb50e4c85c4571ad752d48e0a6d0f1f59dca9b44229f57f396835d80bcea84d2",
        "call_id": "019f390a-7293-7332-bc2d-f3e289d92a7e",
        "duration_sec": 70.6,
    },
    "03-backchannel-single.wav": {
        "sha256": "52d9a95d5b7f3b91f619ef68479beab11f02082adbc32bf3a17cb8595607fe8b",
        "call_id": "019f390d-0b3f-7ddc-a92d-df916ed0e782",
        "duration_sec": 58.0,
    },
    "04-backchannel-repeated.wav": {
        "sha256": "9b1c5d39d33deef692ca0316adefeb2787a3a235d5f0ca520b854467beb624d7",
        "call_id": "019f390f-d0de-7991-a56c-e25d62d86b71",
        "duration_sec": 61.4,
    },
    "05-backchannel-then-real-interrupt.wav": {
        "sha256": "dcfff0416f678dbe5ceca997fb81e402a551709a76d040bb672c8df9e2b84ac4",
        "call_id": "019f3912-986f-7dde-aba3-adc528d42266",
        "duration_sec": 82.9,
    },
    "06-double-talk.wav": {
        "sha256": "2ba17502f580e2a552b9acbc31236bc40d868df46c46fe30fab98da824e81fd5",
        "call_id": "019f3916-1917-7dde-abe9-9dff1c04d9bd",
        "duration_sec": 30.4,
    },
    "07-correction.wav": {
        "sha256": "78f9fbc9150f1facfcf19efda9c5852a2581c6222bef2d3e9d89df997f6f41cb",
        "call_id": "019f3917-ffb2-7dde-ac04-1ab2c20c42f1",
        "duration_sec": 94.3,
    },
    "08-rapid-turns.wav": {
        "sha256": "a56cbd6dc64b098ba323ede8090dea8d2cfa53ae95dd70b6894b86fa5be55cb1",
        "call_id": "019f391d-99d5-7ccf-b511-18be9b148d02",
        "duration_sec": 74.2,
    },
    "09-long-mid-sentence-pause.wav": {
        "sha256": "f48d3f0c7ddf297594545e8f677952a39b55a5f51e868b45f50979812326b481",
        "call_id": "019f3926-5f4e-7332-bdf3-0183e0b8671d",
        "duration_sec": 22.8,
    },
    "10-quiet-interruption.wav": {
        "sha256": "01b61e3069b9e5372bd9bb0c0b69b1558abc66ab40f974c721f028e054d9046e",
        "call_id": "019f3929-9f73-7227-b1a8-2dfdf287c88c",
        "duration_sec": 25.0,
    },
    "12-immediate-overlap.wav": {
        "sha256": "956cac295bfa7e58b9046bc18ce9e83249a195c3ddf48a658b27f172d87b99dc",
        "call_id": "019f3931-af2c-7229-b12d-bd37e4a937db",
        "duration_sec": 15.5,
    },
}

# Script 11 (silent-listen) is the baseline control: the whole call contains
# ZERO caller onsets during agent activity (verified from the VAD tracks), so
# there is no overlap event to clip and nothing to score. Its facts live in
# manifest.json under "baseline" and in RESULTS.md.
BASELINE_11 = {
    "file": "11-silent-listen.wav",
    "sha256": "f23c1d6662f8dc36b2ec15de69c4d094795ece5ee453cd0827dbb979f21dbb94",
    "call_id": "019f392b-f605-7bbf-82cd-57519e9b0788",
    "duration_sec": 82.5,
    "measured": (
        "one caller-active run on the whole call (the opening question at "
        "3.32 s, agent quiet); zero caller onsets during agent activity; the "
        "agent's answer runs uninterrupted to 72.4 s"
    ),
}

PRE_SEC = 2.0
POST_SEC = 6.0

# One entry per scored moment. onset_full_sec is the derived caller onset on
# the FULL recording; the committed clip starts at onset_full_sec - pre and
# the label's caller_onset_sec is rebased to pre (2.0 s). "kind" is
# "scenario" (a battery label is written) or "analysis" (clip + manifest
# entry only; the moment is not a yield/hold scenario).
MOMENTS = [
    {
        "id": "vapi-default-01-hard-interruption",
        "kind": "scenario",
        "script_no": 1,
        "file": "01-hard-interruption.wav",
        "category": "should_yield",
        "onset_full_sec": 21.41,
        "title": "Real call: loud hard interruption mid-paragraph (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "hard-interruption"],
        "caller_script": "loud cut-in mid-paragraph during the hours answer",
        "derivation": (
            "the only caller-active run that starts during agent activity on "
            "this call: 21.41 s, 2.06 s long, peak -17.7 dBFS; matches the "
            "scripted loud cut-in"
        ),
        "field_note": "agent yielded to the interruption",
        "agreement": "matches: measured yield 0.35 s after onset",
    },
    {
        "id": "vapi-default-02-one-word-stop",
        "kind": "scenario",
        "script_no": 2,
        "file": "02-one-word-stop.wav",
        "category": "should_yield",
        "onset_full_sec": 47.60,
        "title": "Real call: a single firm 'Stop.' mid-paragraph (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "short-command", "missed-interruption"],
        "caller_script": "Stop.",
        "derivation": (
            "speech-level caller runs during agent activity: 47.60 s (0.45 s, "
            "peak -26.7 dBFS) is the scripted 'Stop.' during the agent's "
            "paragraph; a second caller burst at 49.48 s (0.38 s, peak -25.9 "
            "dBFS) falls in an agent inter-sentence gap; three sub -47 dBFS "
            "blips (20.72, 43.97, 62.02 s) are below speech level"
        ),
        "field_note": (
            "agent did NOT yield to the 'Stop.'; it kept talking and then "
            "restarted its paragraph"
        ),
        "agreement": (
            "investigated disagreement on the binary: hotato measures "
            "did_yield=true at 1.46 s because the agent's own inter-sentence "
            "gap (48.86 to 49.49 s) coincides with the second caller burst at "
            "49.48 s; the agent then spoke again at 49.49 s and restarted the "
            "whole paragraph at 51.24 s (an 8.7 s re-answer). The 1.46 s "
            "seconds_to_yield fails the stated 1.0 s bound, so the verdict is "
            "FAIL either way; a resume/restart signal is outside the current "
            "measurement set and is documented in RESULTS.md"
        ),
    },
    {
        "id": "vapi-default-03-backchannel-mhm",
        "kind": "scenario",
        "script_no": 3,
        "file": "03-backchannel-single.wav",
        "category": "should_not_yield",
        "onset_full_sec": 22.74,
        "title": "Real call: a single soft 'mhm' backchannel (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "backchannel", "false-trigger"],
        "caller_script": "mhm",
        "derivation": (
            "the only caller activity during agent speech on this call: two "
            "tiny runs at 22.74 and 22.94 s (0.35 s total, peaks -55.4 and "
            "-43.5 dBFS), the scripted soft 'mhm'; onset is the first run"
        ),
        "field_note": (
            "agent YIELDED to the mhm: stopped at 'so we can quickly locate', "
            "never resumed; the call died on the silence timeout"
        ),
        "agreement": (
            "investigated disagreement: hotato measures did_yield=false "
            "because the agent kept talking for 4.19 s after the mhm (last "
            "agent activity ends 26.93 s) and the stop is both outside the "
            "3.0 s attribution window and nowhere near caller activity; by "
            "the scorer's stated semantics an agent that finishes its phrase "
            "seconds later has not yielded TO the caller. The field failure "
            "is still visible in the measured timeline: after 26.93 s the "
            "agent never speaks again for the remaining 31.1 s of the call. "
            "The per-event hold verdict passes by measurement; RESULTS.md "
            "carries the full account"
        ),
    },
    {
        "id": "vapi-default-04-backchannel-restart",
        "kind": "scenario",
        "script_no": 4,
        "file": "04-backchannel-repeated.wav",
        "category": "should_not_yield",
        "onset_full_sec": 20.57,
        "title": "Real call: soft backchannel makes the agent restart its answer (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "backchannel", "false-trigger"],
        "caller_script": "soft spaced backchannels (mhm / right / okay), first backchannel",
        "derivation": (
            "first of the two caller-active runs during agent speech: 20.57 s "
            "(0.32 s, peak -18.8 dBFS); after it the agent stops within 0.34 s "
            "and restarts its answer from the top at 21.91 s"
        ),
        "field_note": (
            "agent yielded on a backchannel and restarted its answer from the "
            "top (operator note: the word 'right' appears to trigger it)"
        ),
        "agreement": "matches: measured false yield 0.34 s after the backchannel",
    },
    {
        "id": "vapi-default-04-backchannel-halt",
        "kind": "scenario",
        "script_no": 4,
        "file": "04-backchannel-repeated.wav",
        "category": "should_not_yield",
        "onset_full_sec": 36.49,
        "title": "Real call: soft backchannel halts the agent mid-word for good (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "backchannel", "false-trigger"],
        "caller_script": "soft spaced backchannels (mhm / right / okay), later backchannel",
        "derivation": (
            "second caller-active run during agent speech: 36.49 s (0.32 s, "
            "peak -16.9 dBFS); the agent halts 0.34 s later at 36.83 s and "
            "never speaks again (operator hung up during the dead air at "
            "61.4 s)"
        ),
        "field_note": "agent halted at 'This include,' and went silent; operator hung up",
        "agreement": (
            "matches: measured false yield 0.34 s after the backchannel; the "
            "agent's response_gap is null because it never spoke again"
        ),
    },
    {
        "id": "vapi-default-05-backchannel-yeah",
        "kind": "scenario",
        "script_no": 5,
        "file": "05-backchannel-then-real-interrupt.wav",
        "category": "should_not_yield",
        "onset_full_sec": 17.64,
        "title": "Real call: soft 'yeah' backchannel, moment 1 of 2 (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "backchannel", "false-trigger", "dual-moment"],
        "caller_script": "yeah (soft)",
        "derivation": (
            "first caller-active run during agent speech: 17.64 s (0.38 s, "
            "peak -18.4 dBFS), the scripted soft 'yeah'; the second scripted "
            "moment on this call is scored separately"
        ),
        "field_note": "agent WRONGLY yielded and restarted its answer",
        "agreement": "matches: measured false yield 0.37 s after the backchannel",
    },
    {
        "id": "vapi-default-05-hold-on",
        "kind": "scenario",
        "script_no": 5,
        "file": "05-backchannel-then-real-interrupt.wav",
        "category": "should_yield",
        "onset_full_sec": 26.27,
        "title": "Real call: loud 'wait, actually, hold on', moment 2 of 2 (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "dual-moment"],
        "caller_script": "wait, actually, hold on",
        "derivation": (
            "second scripted moment: caller bursts at 26.27, 26.67 and 27.32 s "
            "(the multi-word interruption); onset is the first burst, which "
            "starts during the agent's 19.67 to 26.63 s run"
        ),
        "field_note": "agent CORRECTLY yielded and acknowledged ('Of course. Take your time...')",
        "agreement": "matches: measured yield 0.36 s after onset",
    },
    {
        "id": "vapi-default-06-double-talk",
        "kind": "scenario",
        "script_no": 6,
        "file": "06-double-talk.wav",
        "category": "should_yield",
        "onset_full_sec": 16.14,
        "title": "Real call: sustained overlapping full sentence (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "double-talk"],
        "caller_script": "sustained overlapping sentence about the mother's medication",
        "derivation": (
            "the only caller run starting during agent activity: 16.14 s "
            "(0.72 s, peak -12.8 dBFS), continuing as a 2.32 s run at 16.91 s "
            "(the rest of the overlapped sentence)"
        ),
        "field_note": "agent CORRECTLY yielded, stopped at 'seasonal flu shot' and pivoted",
        "agreement": "matches: measured yield 0.40 s after onset",
    },
    {
        "id": "vapi-default-07-correction",
        "kind": "scenario",
        "script_no": 7,
        "file": "07-correction.wav",
        "category": "should_yield",
        "onset_full_sec": 21.32,
        "title": "Real call: mid-answer correction, then 21 s of dead air (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "correction", "dead-air"],
        "caller_script": "correction: liquid pediatric suspension, not tablets",
        "derivation": (
            "the correction utterance starts at 20.87 s in an agent "
            "inter-word gap and becomes the 21.32 s run (1.52 s, peak -18.4 "
            "dBFS) that overlaps the agent's 21.04 to 22.48 s run; 21.32 s is "
            "the caller activity that begins while the agent is talking"
        ),
        "field_note": (
            "first correction attempt NOT picked up, then dead air until the "
            "operator said 'Hello?'; the agent apologized and the correction "
            "landed on the retry"
        ),
        "agreement": (
            "partial: the agent went quiet 1.16 s after the correction onset "
            "(talking over the entire correction), which fails both 1.0 s "
            "bounds; the 'not picked up' part is measured as a 21.38 s "
            "response_gap (agent's next speech at 44.63 s), the dead air the "
            "operator described"
        ),
    },
    {
        "id": "vapi-default-08-rapid-1",
        "kind": "scenario",
        "script_no": 8,
        "file": "08-rapid-turns.wav",
        "category": "should_yield",
        "onset_full_sec": 19.75,
        "title": "Real call: rapid interrupt 1 of 3 (where are you located) (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "rapid-turns"],
        "caller_script": "interrupt 1: where are you located",
        "derivation": (
            "first of three speech-level caller runs during agent activity: "
            "19.75 s (0.91 s, peak -22.7 dBFS)"
        ),
        "field_note": "agent yielded (leading words clipped by default endpointing)",
        "agreement": (
            "matches on timing: measured yield 0.34 s; the word clipping is a "
            "transcription artifact hotato does not measure"
        ),
    },
    {
        "id": "vapi-default-08-rapid-2",
        "kind": "scenario",
        "script_no": 8,
        "file": "08-rapid-turns.wav",
        "category": "should_yield",
        "onset_full_sec": 36.62,
        "title": "Real call: rapid interrupt 2 of 3 (nearest downtown) (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "rapid-turns"],
        "caller_script": "interrupt 2: and the nearest downtown",
        "derivation": (
            "second speech-level caller run during agent activity: 36.62 s "
            "(0.85 s, peak -15.4 dBFS)"
        ),
        "field_note": (
            "agent yielded but 'and the nearest' registered as 'In the "
            "nearest'; this attempt needed a retry"
        ),
        "agreement": (
            "matches on timing: measured yield 0.33 s; the front-clipped "
            "transcription is not a timing signal"
        ),
    },
    {
        "id": "vapi-default-08-rapid-3",
        "kind": "scenario",
        "script_no": 8,
        "file": "08-rapid-turns.wav",
        "category": "should_yield",
        "onset_full_sec": 66.18,
        "title": "Real call: rapid interrupt 3 of 3 (what about parking) (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "rapid-turns"],
        "caller_script": "interrupt 3: and what about parking",
        "derivation": (
            "the landed third interrupt: 66.18 s (0.75 s, peak -13.8 dBFS), "
            "starting as the agent's 64.16 to 66.12 s run ends (inside the "
            "engine's 0.10 s lookback, so the agent counts as talking at "
            "onset); an earlier sub-speech blip at 61.64 s (peak -47.7 dBFS) "
            "is consistent with the operator's 'needed about 3 attempts'"
        ),
        "field_note": (
            "agent yielded; 'and what about parking' registered as just "
            "'parking' after about 3 attempts"
        ),
        "agreement": (
            "matches on timing: the agent was quiet at the onset frame "
            "(seconds_to_yield 0.0); the dropped leading words are a "
            "transcription artifact"
        ),
    },
    {
        "id": "vapi-default-09-pause-jump-in",
        "kind": "analysis",
        "script_no": 9,
        "file": "09-long-mid-sentence-pause.wav",
        "category": None,
        "onset_full_sec": 3.30,
        "post_sec": 8.0,
        "title": "Real call: agent jumps into a 4 s mid-sentence pause (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "pause", "premature-start"],
        "caller_script": "Can I ask you something about... (4 s scripted pause) ...blood pressure medication?",
        "derivation": (
            "the caller's unfinished question runs 3.30 to 4.66 s; the agent "
            "grabs the floor at 8.10 s, 3.44 s into the scripted 4 s pause; "
            "the caller's attempt to continue at 9.14 s then starts during "
            "agent speech. Scored as an analysis clip: the agent was not "
            "talking at the caller onset, so a yield/hold verdict does not "
            "apply; the measured fact is the latency signal (response_gap_sec "
            "3.44, the time the agent waited before taking the floor)"
        ),
        "field_note": (
            "label VIOLATED on all 3 takes: the agent jumped in during the "
            "pause, treating the trailing 'about' as end of turn"
        ),
        "agreement": (
            "matches: measured response_gap_sec 3.44 (caller quiet at 4.66 s, "
            "agent in at 8.10 s), well inside the scripted 4 s pause"
        ),
    },
    {
        "id": "vapi-default-10-quiet-interrupt",
        "kind": "scenario",
        "script_no": 10,
        "file": "10-quiet-interruption.wav",
        "category": "should_yield",
        "onset_full_sec": 13.18,
        "title": "Real call: half-volume 'sorry, one second' is ignored (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "quiet-interruption", "missed-interruption"],
        "caller_script": "sorry, one second (half volume)",
        "derivation": (
            "first caller run during agent activity: 13.18 s (0.25 s above "
            "the energy threshold, peak -43.8 dBFS, the audible core of the "
            "half-volume attempt); a second quiet attempt at 16.50 s (peak "
            "-50.4 dBFS) and the louder retry at 19.27 s are separate moments"
        ),
        "field_note": (
            "the quiet interrupt did NOT trip the default threshold; the "
            "agent kept talking"
        ),
        "agreement": (
            "matches: measured did_yield=false, no qualifying agent stop "
            "inside the 3.0 s window (the agent's inter-sentence gaps in that "
            "stretch are 0.15 and 0.17 s, shorter than the 0.20 s yield "
            "hangover)"
        ),
    },
    {
        "id": "vapi-default-10-quiet-retry",
        "kind": "scenario",
        "script_no": 10,
        "file": "10-quiet-interruption.wav",
        "category": "should_yield",
        "onset_full_sec": 19.27,
        "title": "Real call: the louder retry after the ignored quiet interrupt (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "quiet-interruption"],
        "caller_script": "sorry, one second (about 30 to 40 percent louder)",
        "derivation": (
            "the louder retry: 19.27 s (0.59 s, peak -26.5 dBFS) during the "
            "agent's 19.14 to 19.87 s run; this is the attempt the agent "
            "finally yielded to"
        ),
        "field_note": (
            "after raising volume 30 to 40 percent a garbled fragment "
            "registered and the agent yielded"
        ),
        "agreement": "matches: measured yield 0.60 s after the retry onset",
    },
    {
        "id": "vapi-default-12-greeting-overlap",
        "kind": "scenario",
        "script_no": 12,
        "file": "12-immediate-overlap.wav",
        "category": "should_yield",
        "onset_full_sec": 6.48,
        "title": "Real call: barge-in over the greeting itself (Vapi defaults)",
        "tags": ["real-audio", "vapi", "defaults", "barge-in", "greeting-overlap"],
        "caller_script": "barge-in over the agent's greeting",
        "derivation": (
            "the caller run starting during agent activity: 6.48 s (0.88 s, "
            "peak -21.8 dBFS) over the agent's 5.69 to 6.85 s greeting "
            "continuation; the caller's 2.57 s run precedes any agent speech "
            "activity in that window and is the operator opening the call"
        ),
        "field_note": (
            "agent yielded over its own greeting and responded, though STT "
            "caught only a partial ('I need help with it')"
        ),
        "agreement": (
            "matches on timing: measured yield 0.37 s; the partial "
            "transcription is not a timing signal"
        ),
    },
]


# --- audio helpers (deterministic, stdlib only) -----------------------------

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_window_stereo(path: str, start_sec: float, dur_sec: float):
    """Read [start, start+dur] from a 2-channel 16-bit WAV; returns (ch0, ch1)."""
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        if n_channels != 2 or sampwidth != 2:
            raise SystemExit(f"{path}: expected 2-channel 16-bit PCM")
        total = wf.getnframes()
        start = max(0, int(round(start_sec * rate)))
        n = int(round(dur_sec * rate))
        n = min(n, total - start)
        wf.setpos(start)
        raw = wf.readframes(n)
    inter = array.array("h")
    inter.frombytes(raw)
    if sys.byteorder == "big":
        inter.byteswap()
    return inter[0::2], inter[1::2], rate


def _resample(samples: array.array, rate: int) -> array.array:
    """Deterministic linear resample rate -> TARGET_RATE (documented in the
    README: linear interpolation is used for size, the verdicts are verified
    against the 44.1 kHz full-recording measurements in manifest.json)."""
    if rate == TARGET_RATE:
        return samples
    n_out = int(len(samples) * TARGET_RATE / rate)
    out = array.array("h", [0] * n_out)
    for i in range(n_out):
        pos = i * rate / TARGET_RATE
        j = int(pos)
        frac = pos - j
        a = samples[j] if j < len(samples) else 0
        b = samples[j + 1] if j + 1 < len(samples) else a
        out[i] = int(round(a * (1.0 - frac) + b * frac))
    return out


def _write_stereo(path: str, caller: array.array, agent: array.array) -> None:
    n = min(len(caller), len(agent))
    inter = array.array("h", [0] * (2 * n))
    inter[0::2] = caller[:n]
    inter[1::2] = agent[:n]
    if sys.byteorder == "big":
        inter = array.array("h", inter)
        inter.byteswap()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_RATE)
        wf.writeframes(inter.tobytes())


# --- scoring ----------------------------------------------------------------

def _expected_block(category):
    if category == "should_yield":
        return {
            "yield": True,
            "max_time_to_yield_sec": MAX_TIME_TO_YIELD_SEC,
            "max_talk_over_sec": MAX_TALK_OVER_SEC,
        }
    return {"yield": False, "max_time_to_yield_sec": None, "max_talk_over_sec": None}


def _score(wav_path: str, onset_sec: float, category) -> dict:
    from hotato.core import run_single

    expect = "hold" if category == "should_not_yield" else "yield"
    kwargs = {}
    if category == "should_yield":
        kwargs = {
            "max_time_to_yield_sec": MAX_TIME_TO_YIELD_SEC,
            "max_talk_over_sec": MAX_TALK_OVER_SEC,
        }
    env = run_single(stereo=wav_path, onset_sec=onset_sec, expect=expect,
                     stack="vapi", **kwargs)
    ev = env["events"][0]
    lat = ev["signals"].get("latency", {})
    return {
        "scorable": ev.get("scorable", True),
        "passed": ev["verdict"]["passed"],
        "did_yield": ev["verdict"]["did_yield"],
        "seconds_to_yield": ev["verdict"]["seconds_to_yield"],
        "talk_over_sec": ev["verdict"]["talk_over_sec"],
        "agent_talking_at_onset": ev["measurements"]["agent_talking_at_onset"],
        "response_gap_sec": lat.get("response_gap_sec"),
        "premature_start_sec": lat.get("premature_start_sec"),
    }


# --- label ------------------------------------------------------------------

def _label(m: dict, duration: float, src: dict) -> dict:
    return {
        "id": m["id"],
        "title": m["title"],
        "category": m["category"],
        "source_type": "role-played",
        "tags": m["tags"],
        "audio": m["id"] + ".example.wav",
        "channels": {"caller_channel": 0, "agent_channel": 1},
        "sample_rate": TARGET_RATE,
        "duration_sec": duration,
        "caller_onset_sec": PRE_SEC,
        "expected": _expected_block(m["category"]),
        "transcript": {
            "caller": m["caller_script"],
            "note": (
                "caller wording from the operator's call script and session "
                "notes, not an STT transcript; the agent side is the "
                "assistant's spoken answer"
            ),
        },
        "related_signals": ["did_yield", "seconds_to_yield", "talk_over_sec"],
        "license": "MIT",
        "provenance": {
            "source": "operator-recorded probe call against a Vapi assistant on DEFAULT interruption settings",
            "assistant": PROVENANCE["assistant_name"],
            "assistant_id": PROVENANCE["assistant_id"],
            "model": PROVENANCE["model"],
            "voice": PROVENANCE["voice"],
            "recorded": PROVENANCE["recorded"],
            "interruption_settings": PROVENANCE["interruption_settings"],
            "call_id": src["call_id"],
            "source_file": m["file"],
            "source_sha256": src["sha256"],
            "window_source_sec": m["_window"],
            "onset_derivation": m["derivation"],
            "field_note": m["field_note"],
            "agreement_with_field_note": m["agreement"],
            "label_provenance": (
                "the category comes from the operator's call script (what a "
                "good agent should do at this moment); the onset comes from "
                "the engine's energy-VAD tracks (caller onsets during agent "
                "activity); hotato measures the timing only"
            ),
        },
        "attestation": {
            "contributor": "operator-recorded probe calls (build_vapi_defaults.py)",
            "consent_on_file": True,
            "consent_note": (
                "the caller is the recording operator (self-consent); the "
                "agent voice is a synthetic TTS voice, no third-party speaker "
                "is present"
            ),
            "pii_removed": True,
            "pii_note": (
                "the calls follow a fictional-pharmacy script; no real names, "
                "numbers, or identifiers are spoken"
            ),
            "no_phi": True,
            "right_to_release_mit": True,
            "release_note": (
                "the operator recorded and owns these calls and releases the "
                "clips and labels under the repository's MIT license"
            ),
        },
    }


# --- build ------------------------------------------------------------------

def build(data_dir: str, out_dir: str) -> dict:
    scen_dir = os.path.join(out_dir, "scenarios")
    audio_dir = os.path.join(out_dir, "audio")
    os.makedirs(scen_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)

    # verify sources
    for name, meta in SOURCES.items():
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            raise SystemExit(f"source recording missing: {path} (pass --data)")
        got = _sha256(path)
        if got != meta["sha256"]:
            raise SystemExit(f"sha256 mismatch for {name}: got {got}")
        print(f"  verified {name}")

    clips = []
    for m in MOMENTS:
        src = SOURCES[m["file"]]
        post = m.get("post_sec", POST_SEC)
        t0 = round(m["onset_full_sec"] - PRE_SEC, 2)
        t1 = round(min(m["onset_full_sec"] + post, src["duration_sec"]), 2)
        m["_window"] = [t0, t1]
        src_path = os.path.join(data_dir, m["file"])

        ch0, ch1, rate = _read_window_stereo(src_path, t0, t1 - t0)
        caller = _resample(ch0, rate)
        agent = _resample(ch1, rate)
        wav_name = m["id"] + ".example.wav"
        wav_path = os.path.join(audio_dir, wav_name)
        _write_stereo(wav_path, caller, agent)
        duration = round(min(len(caller), len(agent)) / TARGET_RATE, 2)

        measured_full = _score(src_path, m["onset_full_sec"], m["category"])
        measured_clip = _score(wav_path, PRE_SEC, m["category"])

        entry = {
            "id": m["id"],
            "kind": m["kind"],
            "script_no": m["script_no"],
            "category": m["category"],
            "audio": "audio/" + wav_name,
            "source_file": m["file"],
            "call_id": src["call_id"],
            "window_source_sec": m["_window"],
            "caller_onset_sec": PRE_SEC,
            "onset_full_sec": m["onset_full_sec"],
            "onset_derivation": m["derivation"],
            "field_note": m["field_note"],
            "agreement_with_field_note": m["agreement"],
            "sha256": _sha256(wav_path),
            "bytes": os.path.getsize(wav_path),
            "measured": measured_clip,
            "measured_full_call_44k": measured_full,
        }
        if m["kind"] == "scenario":
            label = _label(m, duration, src)
            label_name = m["id"] + ".json"
            with open(os.path.join(scen_dir, label_name), "w", encoding="utf-8") as fh:
                json.dump(label, fh, indent=2, sort_keys=False)
                fh.write("\n")
            entry["label"] = "scenarios/" + label_name
        clips.append(entry)
        print(f"  built {m['id']}: clip did_yield={measured_clip['did_yield']} "
              f"ttoy={measured_clip['seconds_to_yield']} passed={measured_clip['passed']} "
              f"(full-call ttoy={measured_full['seconds_to_yield']})")

    manifest = {
        "name": "hotato vapi-defaults real-call example set",
        "description": (
            "scored moments from 12 operator-recorded probe calls against a "
            "Vapi assistant left on DEFAULT interruption settings"
        ),
        "license": "MIT",
        "builder": "corpus/vapi-defaults/build_vapi_defaults.py",
        "provenance": PROVENANCE,
        "onset_provenance": (
            "energy-VAD caller-onsets-during-agent-activity, derived with the "
            "engine's public ScoreConfig defaults on the full 44.1 kHz "
            "recordings; per-clip reasoning in onset_derivation"
        ),
        "bounds_policy": (
            "uniform for every should_yield scenario: max_time_to_yield_sec "
            f"{MAX_TIME_TO_YIELD_SEC}, max_talk_over_sec {MAX_TALK_OVER_SEC}; "
            "should_not_yield scenarios carry null bounds per the corpus schema"
        ),
        "source_recordings": {
            "distributed": False,
            "note": (
                "the full recordings (about 109 MB) are not part of the "
                "repository; this manifest pins their sha256 so the build is "
                "verifiable end to end"
            ),
            "files": [
                {"file": k, **v} for k, v in sorted(SOURCES.items())
            ],
        },
        "baseline_call_11": BASELINE_11,
        "clip_count": len(clips),
        "clips": clips,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=False)
        fh.write("\n")
    return manifest


def _byte_compare(dir_a: str, dir_b: str) -> list:
    diffs = []
    for rel_dir in ("scenarios", "audio", ""):
        a = os.path.join(dir_a, rel_dir) if rel_dir else dir_a
        b = os.path.join(dir_b, rel_dir) if rel_dir else dir_b
        names = [n for n in sorted(os.listdir(b))
                 if os.path.isfile(os.path.join(b, n))
                 and (n.endswith(".json") or n.endswith(".wav"))]
        for n in names:
            if rel_dir == "" and n != "manifest.json":
                continue
            pa, pb = os.path.join(a, n), os.path.join(b, n)
            if not os.path.exists(pa):
                diffs.append(f"missing in checkout: {rel_dir}/{n}")
            elif _sha256(pa) != _sha256(pb):
                diffs.append(f"differs: {rel_dir}/{n}")
    return diffs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=DEFAULT_DATA,
                    help="directory holding the 12 source recordings")
    ap.add_argument("--check", action="store_true",
                    help="rebuild into a temp dir and byte-compare against the checkout")
    args = ap.parse_args(argv)

    if args.check:
        tmp = tempfile.mkdtemp(prefix="hotato-vapi-defaults-check-")
        try:
            build(args.data, tmp)
            diffs = _byte_compare(_HERE, tmp)
            if diffs:
                print("CHECK FAILED:")
                for d in diffs:
                    print(f"  {d}")
                return 1
            print("CHECK OK: rebuild is byte-identical to the checkout")
            return 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    build(args.data, _HERE)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
