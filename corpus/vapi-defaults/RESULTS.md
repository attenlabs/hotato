# Results: 12 real calls vs a Vapi assistant on DEFAULT interruption settings

One human caller (the operator, working from a written script), one production
Vapi assistant (hotato-probe, model openai/gpt-4o, voice vapi/Elliot),
interruption settings left at the Vapi defaults, recorded 2026-07-06 as
dual-channel audio (caller ch0, agent ch1). Hotato scored every scripted
moment from the audio alone. Every number below is a timing measured by the
energy reference at its default configuration; nothing was tuned to match the
operator's notes, and the two places where the measurement and the field
notes disagree are investigated below, not smoothed over.

## How each onset was chosen

A small helper (the derivation logic is embedded in
`build_vapi_defaults.py`) ran the engine's own energy VAD over both channels
of each full recording and listed every sustained caller-active run that
starts while the agent channel is active (the engine's 0.10 s lookback). The
caller also asks scripted questions during agent silence, so the scored
moment is the caller activity that begins DURING agent speech, matched to the
script notes. The chosen onset, the candidate list, and the reasoning are
recorded per clip in `manifest.json` (`onset_derivation`).

## The table

Bounds, stated once and applied uniformly to every should_yield moment:
`max_time_to_yield_sec 1.0`, `max_talk_over_sec 1.0`. Hold moments carry no
bounds (the failure is yielding at all). All measurements below reproduce
identically on the committed 16 kHz clips and on the full 44.1 kHz
recordings (`manifest.json` records both).

| script | moment (onset in full call) | expected | did_yield | seconds_to_yield | talk_over_sec | verdict | matches field note |
|---|---|---|---|---|---|---|---|
| 1 hard-interruption | loud cut-in (21.41 s) | yield | true | 0.35 | 0.35 | PASS | yes |
| 2 one-word-stop | "Stop." (47.60 s) | yield | true | 1.46 | 0.45 | FAIL, slow yield | investigated below |
| 3 backchannel-single | soft "mhm" (22.74 s) | hold | false | - | 0.35 | PASS | investigated below |
| 4 backchannel-repeated | backchannel 1 (20.57 s) | hold | true | 0.34 | 0.32 | FAIL, false yield | yes (agent restarted its answer) |
| 4 backchannel-repeated | backchannel 2 (36.49 s) | hold | true | 0.34 | 0.32 | FAIL, false yield | yes (agent halted for good) |
| 5 dual-moment | soft "yeah" (17.64 s) | hold | true | 0.37 | 0.37 | FAIL, false yield | yes |
| 5 dual-moment | "wait, actually, hold on" (26.27 s) | yield | true | 0.36 | 0.33 | PASS | yes |
| 6 double-talk | overlapping sentence (16.14 s) | yield | true | 0.40 | 0.40 | PASS | yes |
| 7 correction | correction attempt (21.32 s) | yield | true | 1.16 | 1.16 | FAIL, slow yield + talk-over | partial, see below |
| 8 rapid-turns | interrupt 1 (19.75 s) | yield | true | 0.34 | 0.34 | PASS | yes, on timing |
| 8 rapid-turns | interrupt 2 (36.62 s) | yield | true | 0.33 | 0.33 | PASS | yes, on timing |
| 8 rapid-turns | interrupt 3 (66.18 s) | yield | true | 0.00 | 0.00 | PASS | yes, on timing |
| 9 pause-jump-in | agent enters caller's pause | n/a (analysis) | n/a | n/a | n/a | response_gap_sec 3.44 | yes, violation measured |
| 10 quiet-interruption | half-volume interrupt (13.18 s) | yield | false | - | 0.25 | FAIL, missed interruption | yes |
| 10 quiet-interruption | louder retry (19.27 s) | yield | true | 0.60 | 0.59 | PASS | yes |
| 11 silent-listen | none (baseline) | no event | - | - | - | no overlap event exists | yes |
| 12 immediate-overlap | barge over greeting (6.48 s) | yield | true | 0.37 | 0.37 | PASS | yes, on timing |

Battery totals: 15 scenarios, 9 pass, 6 fail (exit_code 1). The six failures
land on BOTH axes: one missed real interruption (script 10), three false
yields on backchannels (scripts 4 and 5), two slow yields (scripts 2 and 7).

Script 9 is committed as an analysis clip, not a battery scenario: the agent
was not talking at the caller onset, so a yield/hold verdict does not apply
(the event is not scorable by design and the scorer says so). The measured
fact is the latency signal: the caller went quiet mid-sentence at 4.66 s
after "Can I ask you something about..." and the agent took the floor at
8.10 s, a measured `response_gap_sec` of 3.44, well inside the scripted 4 s
pause. Script 11 has no committed clip because there is nothing to cut: the
full-call VAD tracks show exactly one caller-active run (the opening
question, during agent silence) and zero caller onsets during agent
activity.

## Disagreement 1, script 2 ("Stop.")

Field note: the agent did NOT yield to the "Stop."; it kept talking and then
restarted its paragraph.

Measured timeline (energy VAD, default config, times in the full call):

- caller "Stop." runs 47.60 to 48.05 s, peak -26.7 dBFS, during the agent's
  45.46 to 48.86 s speech;
- the agent talks 1.26 s past the "Stop." to its own sentence end at
  48.86 s;
- a second caller burst runs 49.48 to 49.86 s (peak -25.9 dBFS), landing in
  the agent's inter-sentence gap;
- the agent speaks again 49.49 to 50.19 s, pauses, then restarts the whole
  paragraph at 51.24 s with an 8.72 s run.

Hotato reports `did_yield=true` at 1.46 s because the agent's quiet stretch
starting at 49.06 s satisfies the yield rule (quiet for 0.20 s or more with
caller activity within 0.5 s: the second burst at 49.48 s). So the binary
disagrees with the field note, and the cause is visible frame by frame: the
agent's own inter-sentence gap coincided with the caller's second attempt.
The verdict does not disagree: 1.46 s fails the 1.0 s bound, so the event is
an honest FAIL (slow yield) either way. What the current signal set cannot
express is the restart: the agent came back at 51.24 s and re-answered from
the top. A resume/restart signal is a documented reserved dimension in the
engine (`ScoreResult.signals`), and this clip is the real-data case for it.

## Disagreement 2, script 3 (soft "mhm")

Field note: the agent YIELDED to the single soft "mhm": it stopped at "so we
can quickly locate", never resumed, and the call died on Vapi's silence
timeout.

Measured timeline:

- caller "mhm" runs 22.74 to 23.12 s (two tiny runs, peaks -55.4 and -43.5
  dBFS), during the agent's 20.61 to 23.61 s speech;
- the agent keeps talking through the "mhm", ends that run at 23.61 s,
  speaks again 23.77 to 26.93 s, and then never speaks again for the
  remaining 31.1 s of the 58.0 s call.

Hotato reports `did_yield=false`: the agent's final stop comes 4.19 s after
the "mhm", outside the 3.0 s search window, and with no caller activity
within 0.5 s of it. That is the scorer's stated semantics: an agent that
finishes its own phrase seconds after an isolated backchannel has not
yielded TO the caller, and the same proximity rule is what keeps the scorer
from inventing yields elsewhere. The field failure is real and the timeline
above measures it (last agent speech at 26.93 s, then silence to the end),
but it is a delayed pipeline stall, not a barge-in yield, and the per-event
hold verdict honestly PASSES by measurement. The false-yield evidence for
the battery comes from scripts 4 and 5, where the stop is immediate (0.34
and 0.37 s) and proximate to the backchannel.

## Script 7, partial agreement (the dead-air correction)

The agent went quiet 1.16 s after the correction onset, having talked over
the entire correction (talk_over 1.16 s): FAIL against both 1.0 s bounds.
The field note's "correction not picked up" then shows up in the latency
signal: the agent's next speech starts 21.38 s after the caller's turn end
(`response_gap_sec` 21.38, measured on the full call; the operator's
"Hello?" attempts at 33.7 and 34.3 s fall inside that gap). The committed
8 s clip preserves the FAIL verdict; the 21.38 s gap is a full-call number
recorded in `manifest.json`.

## The flagship: the funnel fires on real calls at default settings

`hotato run` over the battery
(`PYTHONPATH=src python3 -m hotato.cli run --suite barge-in --scenarios
corpus/vapi-defaults/scenarios --audio corpus/vapi-defaults/audio --stack
vapi --format json`, committed as `battery-result.json`) reports, verbatim:

```json
{
  "reason": "This battery fails on BOTH axes at once: it missed a genuine interruption AND it false-triggered on a backchannel. No single sensitivity threshold can fix both - turning it up to catch the interruption makes the backchannel worse, and vice versa. That is the signal that the agent needs a discriminating layer, not a different threshold.",
  "pointer": {
    "layer": "a learned engagement-control / addressee-detection layer",
    "what": "This is a discrimination problem, not a threshold problem: telling a genuine bid for the floor apart from a backchannel or speech that was not addressed to the agent. No single timing threshold separates them - you can raise a words-to-interrupt threshold, but the same threshold that ignores 'mhm' also ignores 'stop'. Separating them needs a signal for 'is this speech addressed to me, and is it a real bid for the floor' - not a config knob.",
    "honest_scope": "No single timing threshold separates them. Where your stack provides an interruption/backchannel classifier, use it; the general case calls for a learned engagement-control / addressee-detection layer. The audio-only turn-taking case shown here is the hardest modality for it. Treat this as a pointer to the KIND of fix the failure needs, not a benchmarked claim: bring your own recordings and measure."
  }
}
```

`PYTHONPATH=src python3 -m hotato.cli diagnose
corpus/vapi-defaults/battery-result.json` reports, verbatim:

```
hotato diagnose [suite] stack=vapi
  9/15 events pass  (failed=6, not_scorable=0)
  [slow_yield] vapi-default-02-one-word-stop  layer=endpointing config_only_safe=true
    Slow yield: the agent stopped, but late. Likely endpointing layer. Try lowering the endpointing or voice-window setting one step. Tradeoff: may clip the agent on noisy lines; verify against the passing backchannel fixture.
  [false_stop_on_backchannel] vapi-default-04-backchannel-halt  layer=interruption_detection config_only_safe=false
    False stop, but tuning a single threshold is not safe here. See the notes for this event and the battery decision.
  [false_stop_on_backchannel] vapi-default-04-backchannel-restart  layer=interruption_detection config_only_safe=false
    False stop, but tuning a single threshold is not safe here. See the notes for this event and the battery decision.
  [false_stop_on_backchannel] vapi-default-05-backchannel-yeah  layer=interruption_detection config_only_safe=false
    False stop, but tuning a single threshold is not safe here. See the notes for this event and the battery decision.
  [slow_yield] vapi-default-07-correction  layer=endpointing config_only_safe=true
    Slow yield: the agent stopped, but late. Likely endpointing layer. Try lowering the endpointing or voice-window setting one step. Tradeoff: may clip the agent on noisy lines; verify against the passing backchannel fixture.
  [missed_real_interruption] vapi-default-10-quiet-interrupt  layer=interruption_detection config_only_safe=false
    Missed real interruption, but this battery also false-stops on a backchannel. Do not tune a single threshold: fixing one axis worsens the other. See the battery decision.
  battery decision: do_not_tune_single_threshold
    This battery fails on both axes at once: it missed a genuine interruption AND it false-stopped on a backchannel. No single sensitivity threshold can fix both; turning it up for one makes the other worse. The fix class is discrimination (engagement control), not a threshold.
```

That is the point of this directory: a real agent, on real calls, at the
vendor's default settings, fails in both directions at once, and the tool
identifies from measurement alone that no single threshold move can fix it.

## What hotato does and does not measure here

Hotato measures timing from the two audio channels. It confirmed, with
numbers, the operator's field observations on 14 of 17 scored moments; on
script 7 it measured both halves of the "partial" note (the 1.16 s talk-over
AND the 21.38 s dead air); on scripts 2 and 3 the binary `did_yield`
disagreed with the field note for reasons the frame evidence explains
(above), while the failures themselves remain visible in the measured
timelines. Things the field notes describe that are NOT timing and are
outside this tool: which words the STT dropped (scripts 8 and 12), why the
LLM restarted its answer, and Vapi's silence-timeout hangup policy.
