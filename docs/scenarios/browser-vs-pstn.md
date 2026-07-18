# Why does my voice agent pass in the browser but fail on real phone calls?

Because the browser session and the phone leg are different audio: the phone
line pushes the same conversation through an 8 kHz codec and packet loss, and
hotato ships a telephony-degraded scenario class that scores the same
conversation timings through exactly that degradation, offline, so you can
measure what the line changes before your callers do.

The failure mode this page walks through: the agent's turn-taking is tuned
against clean, wideband browser audio. On the phone leg the same moment
arrives companded and clipped by lost packets, onsets smear, and the timing
your gate measured no longer matches the timing your agent hears.

## Score the same moment clean and degraded

The `telephony-degraded` class in [`corpus/classes/`](../../corpus/classes/README.md)
re-renders the exact `reference_render` timings of the `gl-8k-hard-interrupt`
gold scenario through a degraded 8 kHz telephony line: ITU-T G.711 mu-law
companding plus a fixed, mild, non-random packet-loss schedule (20 ms zeroed
out of every 650 ms, starting 140 ms in; the whole transform is
[`corpus/classes/telephony_codec.py`](../../corpus/classes/telephony_codec.py)).
The class ships one PASS pair and one deliberate FAIL pair, so the verdict is
exercised through the codec in both directions.

From a repo checkout, render the class audio (deterministic, byte-identical on
any machine), then score the clean gold suite and the degraded class:

```console
$ python3 corpus/classes/build_classes.py
$ hotato run --scenarios corpus/suites/gold/scenarios --audio corpus/suites/gold/audio
  ...
  [PASS] gl-8k-hard-interrupt: did_yield=True seconds_to_yield=0.63s talk_over=0.63s
  ...
  exit_code=0

$ hotato run --scenarios corpus/classes/telephony-degraded/scenarios --audio corpus/classes/telephony-degraded/audio
hotato [suite] stack=generic offline=True
  1/2 events pass  (failed=1)
  [PASS] td-8k-hard-interrupt-degraded: did_yield=True seconds_to_yield=0.65s talk_over=0.65s
  [FAIL] td-8k-missed-degraded: did_yield=False seconds_to_yield=n/a talk_over=2.35s
         fix[config]: Missed interruption: the agent kept talking over the caller
            knob: interruption sensitivity (VAD min-silence, min-interruption-duration, min-words-to-interrupt)
            move: lower the min-silence and min-duration thresholds so a real interruption registers sooner
  exit_code=1
```

Read the pair against the clean run: the same underlying interruption measures
`seconds_to_yield=0.63s` clean and `0.65s` through the codec, and both
verdicts hold. The PASS stays a pass, the planted defect stays a fail, and the
timing shift the line introduces is measured, not guessed. That is the
property the class pins for your CI gate: verdict stability across codec
degradation, with the drift in the open.

## Triage a phone-quality recording

[`hotato investigate`](../INVESTIGATE.md) runs the same discovery path on a
degraded file that it runs on a clean one: capture origin, input health, then
ranked candidate moments.

```console
$ hotato investigate corpus/classes/telephony-degraded/audio/td-8k-missed-degraded.example.wav
hotato investigate [run 1]: td-8k-missed-degraded.example.wav
  capture origin: frozen regression clip (corpus/classes/telephony-degraded/scenarios/td-8k-missed-degraded.json)
    this recording is a previously-created hotato fixture clip (td-8k-missed-degraded.json), not a live call: a pinned regression, not fresh evidence
  input health: eligible for scan
  verdict path: eligible (a labeled event here can carry a real yield/hold verdict)
  most likely failure (top-ranked candidate):
    [1] t=1.99s overlap_while_agent_talking  overlap_sec=2.36
  next: label it (use --expect hold instead if the agent was right to keep talking):
    hotato investigate label '.hotato/investigate-state.json#1' --expect yield
```

The 8 kHz mu-law file clears the input-health gate on its own merits, and
[`hotato trust`](../TRUST.md) shows you why:

```console
$ hotato trust --stereo corpus/classes/telephony-degraded/audio/td-8k-hard-interrupt-degraded.example.wav
hotato trust: td-8k-hard-interrupt-degraded.example.wav
  recording: 5.0s, 8000 Hz, 2 channels
  caller (ch0): 2.36s speech, first at 1.99s, peak -4.6 dBFS
  agent  (ch1): 2.46s speech, first at 0.19s, peak -4.6 dBFS
  leading silence: 0.19s
  crosstalk: coherence 0.383 (low) at 0.5s lag
  scorability: separated tracks yes, caller activity yes, agent activity yes
  => eligible for scan
```

## Then score your own phone leg

Your telephony stack already records the phone side dual-channel:
[`hotato pull`](../CONNECT.md) fetches the separated two-channel recording,
and the same `investigate`, `trust`, and `run` path scores it. Score a
browser-path recording and a phone recording of the same flow, and the timing
difference between the two is a number in the envelope, not an impression.

## Scope

The class applies G.711 mu-law companding plus the fixed packet-loss schedule
above, and its audio is synthetic shaped noise rendered from the scenario's
own segment timings (no recorded speech, no accuracy claim; see
[`corpus/classes/README.md`](../../corpus/classes/README.md)). It proves the
scorer's verdict is stable under that degradation. The evidence about your
agent's phone behavior comes from your own pulled recordings.
