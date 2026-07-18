# Why does my voice agent keep interrupting itself?

Because its own TTS is bleeding into the caller input and getting heard as a
barge-in: hotato measures this on every scored event as `signals.echo`
(cross-channel coherence at a lag), names echo cancellation as the fix knob
when a phantom yield fires, and `--echo-gate` opts such a yield out of the
verdict instead of letting it pass as clean turn-taking.

## The signature

A self-interruption looks like a yield with no caller behind it: the agent
stops mid-sentence, and the "caller" activity that triggered it is the
agent's own audio arriving back on the caller channel. The corpus ships two
phantom-self-interruption defect fixtures in the `gold-defects` suite (built
deterministically from a repo checkout; see [`docs/SUITES.md`](../SUITES.md)):

```console
$ hotato run --scenarios corpus/suites/gold-defects/scenarios --audio corpus/suites/gold-defects/audio
  ...
  [FAIL] gld-echo-phantom-fast: did_yield=True seconds_to_yield=0.75s talk_over=0.75s
         fix[config]: Phantom self-interruption: the agent yielded to its own audio
            knob: echo cancellation / channel isolation
            move: enable AEC on the input and keep caller and agent audio on separate channels
  ...
  2/16 events pass  (failed=14)
  exit_code=1
```

The measurement behind that fix class is in the event's `signals` block
(`--format json`), for every event, every run:

```json
{
  "signals": {
    "echo": {
      "coherence": 1.0,
      "lag_sec": 0.12,
      "echo_suspected": true
    }
  }
}
```

`coherence` is the similarity between the caller channel's energy envelope
and the agent channel's envelope shifted by `lag_sec`; a caller channel that
is a delayed copy of the agent is suspected TTS bleed. The methodology,
thresholds, and ceiling are in [`METHODOLOGY.md`](../../METHODOLOGY.md).

`hotato scan` surfaces the same evidence label-free when you are triaging a
recording:

```console
$ hotato scan --stereo corpus/suites/gold-defects/audio/gld-echo-phantom-fast.example.wav
hotato scan: gld-echo-phantom-fast.example.wav  (6.0s, 3 candidate moments)
  ...
  [ 2] t=0.31s  overlap_while_agent_talking  overlap=2.44s  agent went silent after 2.44s
  [ 3] t=0.31s  echo_correlated_activity     WARNING likely agent echo: coherence=1.00 at lag 0.12s  (caller channel looks like leaked TTS; a yield here may be the agent hearing itself)
```

## Gate it out of the verdict

An echo-coincident yield is not evidence about your agent's turn-taking, so
`--echo-gate` opts in to holding it out of the verdict entirely:

```console
$ hotato run --scenarios corpus/suites/gold-defects/scenarios --audio corpus/suites/gold-defects/audio --echo-gate
  ...
  [NOT SCORABLE] gld-echo-phantom-fast
         reason: the yield coincides with high cross-channel echo coherence (coherence 1.0 at lag 0.12s), so it is most likely the agent hearing its own audio bleed rather than a real caller. Fix the audio path (echo cancellation, channel separation) before treating this as a yield.
  ...
  2/14 events pass  (failed=12, not_scorable=2)
  exit_code=1
```

Compared with the ungated run, the two phantom events move from `failed` to
`not_scorable` with the reason in the open. The refusal is the point: fix the
audio path first (echo cancellation, channel separation), then score the
turn-taking, so an AEC bug never masquerades as a turn-taking verdict in
either direction.

The input-health side of the same defense is always on:
[`hotato trust`](../TRUST.md) reports crosstalk coherence on every pre-scan,
and [`hotato investigate`](../INVESTIGATE.md)'s verdict-eligibility gate
refuses a recording whose caller channel is dominated by leaked agent audio
rather than scoring it.
