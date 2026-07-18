# Full-duplex turn-taking: scoring the moment both sides speak at once

In a full-duplex conversation the agent hears while it speaks, so overlap is
a normal part of the audio: a caller barges in mid-sentence and both voices
are live at the same time. The question a transcript cannot answer is what
happened next, in seconds: did the agent drop its turn when the caller took
the floor, how fast, and how long did both keep talking at once. hotato
measures exactly that from the two channels of the recording -- `did_yield`,
`seconds_to_yield`, `talk_over_sec` -- the same timing physics whether the
agent runs half- or full-duplex.

`examples/full-duplex/` is the runnable pair for that claim: two scripted
two-channel scenarios, byte-identical on every render, that differ in one
variable only -- what the agent's channel does after the barge-in.

| id | behaviour | outcome |
|---|---|---|
| `fdx-01-barge-in-clean-yield` | caller takes the floor at 2.00 s; the agent stops inside the bounds | overlap, then a clean yield: PASS |
| `fdx-02-barge-in-talk-over` | same barge-in; the agent keeps transmitting through 2.75 s of simultaneous speech | sustained talk-over: FAIL |

Both scenarios declare the same expectation (`yield: true`,
`max_time_to_yield_sec: 1.00`, `max_talk_over_sec: 1.00`) and the same
caller onset. The PASS case still contains measured simultaneous speech:
overlap itself is not the failure, holding the floor through it is.

## Render the pair (deterministic)

```bash
python examples/render_examples.py
```

The per-channel seed is `sha256(scenario_id)`, so two renders are
byte-identical on any machine; `tests/test_examples_full_duplex.py` renders
twice and diffs to pin it. The audio is synthetic, energy-shaped noise --
a scripted fixture, never a recorded person.

## Run both: one PASS verdict, one FAIL verdict

```bash
hotato run --scenarios examples/full-duplex/scenarios --audio examples/full-duplex/audio
```

```
hotato [suite] stack=generic offline=True
  1/2 events pass  (failed=1)
  [PASS] fdx-01-barge-in-clean-yield: did_yield=True seconds_to_yield=0.75s talk_over=0.75s
  [FAIL] fdx-02-barge-in-talk-over: did_yield=True seconds_to_yield=2.75s talk_over=2.75s
         fix[config]: Slow yield: the agent stopped, but too late
            knob: endpointing / VAD min-silence-duration
            move: lower the min-silence and hangover so the agent goes quiet sooner after the caller takes the floor
  exit_code=1
```

The battery exits 1 because it carries one caught regression. In the JSON
envelope (`--format json`) the FAIL verdict states its reasons exactly:

```
yielded in 2.75s, slower than the 1.00s bound
talked over the caller for 2.75s, more than the 1.00s bound
```

Note what the FAIL is not: in `fdx-02` the agent DID yield eventually
(`did_yield=True`), unlike `funnel-demo`'s `fd-01`, where the agent never
stops inside the search window. The regression here is the measured seconds
of talk-over on the way to the floor transfer -- the full-duplex failure
mode, invisible to a text-level eval.

## Score either recording on its own

The same verdicts, one file at a time, with the bounds on the command line:

```bash
hotato run --stereo examples/full-duplex/audio/fdx-01-barge-in-clean-yield.example.wav \
    --onset 2.0 --expect yield --max-time-to-yield 1.0 --max-talk-over 1.0   # exit 0
hotato run --stereo examples/full-duplex/audio/fdx-02-barge-in-talk-over.example.wav \
    --onset 2.0 --expect yield --max-time-to-yield 1.0 --max-talk-over 1.0   # exit 1
```

## Investigate the failure

`hotato investigate` ranks the caught moment without any label given up
front:

```bash
hotato investigate examples/full-duplex/audio/fdx-02-barge-in-talk-over.example.wav
```

```
hotato investigate [run 1]: fdx-02-barge-in-talk-over.example.wav
  capture origin: frozen regression clip (examples/full-duplex/scenarios/fdx-02-barge-in-talk-over.json)
    this recording is a previously-created hotato fixture clip (fdx-02-barge-in-talk-over.json), not a live call: a pinned regression, not fresh evidence
  input health: eligible for scan
  verdict path: eligible (a labeled event here can carry a real yield/hold verdict)
  most likely failure (top-ranked candidate):
    [1] t=1.99s overlap_while_agent_talking  overlap_sec=2.76
  next: label it (use --expect hold instead if the agent was right to keep talking):
    hotato investigate label '.hotato/investigate-state.json#1' --expect yield
  state remembered at: .hotato/investigate-state.json
```

The origin line says out loud that this is a fixture clip, not a live call.
On your own recording the same command prints the same ranked moments and
the one `investigate label` next step that pins the catch as a CI contract
-- the core loop in [AGENTS.md](../AGENTS.md) from step 2 on.

## Exit codes

| Exit | Meaning |
|---|---|
| `0` | every scorable event passed its declared bounds |
| `1` | a scorable event regressed (here: the talk-over bound) |
| `2` | usage error or unusable input (e.g. a mono export: NOT SCORABLE) |

## Related

- [`examples/README.md`](../examples/README.md) -- every additive fixture
  set, including this pair, and the deterministic renderer.
- [INVESTIGATE.md](INVESTIGATE.md) -- one recording to ranked candidate
  moments and a signed contract.
- [CONTRACTS.md](CONTRACTS.md) -- the CI gate that re-runs the stored
  evidence deterministically.
- [SIMULATE.md](SIMULATE.md) -- the scripted-caller renderer for
  transcript/trace fixtures (`origin=simulated`), the no-audio side of the
  same regression foundation.
