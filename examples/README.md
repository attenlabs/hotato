# examples/ additive fixtures

These fixtures live outside the shipped `hotato` package. The bundled `barge-in`
battery inside the package stays at exactly 8 scenarios.

Everything here is additive: extra labelled reference scenarios you run with
`run_suite(scenarios_dir=..., audio_dir=...)` (or the CLI's `--scenarios` /
`--audio`) to exercise dimensions beyond the 8-scenario smoke test, and to see
what specific failures look like.

The audio here is synthetic: deterministic, band-limited, energy-shaped noise. It
is a runnable floor and a regression guard. Real validity comes from your own
labelled calls (see the repo `CONTRIBUTING.md`).

## Scope

- Reproducible timing against overridable thresholds. Every number below is a
  measurement with an exposed method.
- Scoring works on speech energy over time, deterministically.
- The latency signals (`response_gap_sec`, `premature_start_sec`) are pure timing
  on the same two VAD tracks. Every threshold that produces them
  (`turn_end_silence_sec`, `premature_tolerance_sec`, VAD `hangover_sec`, ...) is a
  documented, exposed `ScoreConfig` parameter.

## Layout

```
examples/
  render_examples.py            # deterministic renderer (stdlib + the vendored engine)
  scenarios/                    # good-reference labels (all PASS)
  audio/                        # rendered <id>.example.wav (stereo) + <id>.caller.wav (mono)
  funnel-demo/
    scenarios/                  # a deliberately-bad agent battery (all FAIL, on purpose)
    audio/
```

## Scenarios

### Latency (endpointing): `scenarios/lat-*`

A multi-turn prompt-response: the caller asks a complete question and stops, then
the agent answers. The scored dimension is the endpointing latency, carried in
`signals.latency`. `reference_render` gives the exact caller offset, so the
response-gap ground truth is known.

These are rendered continuous (gapless) so the VAD's active-track edges equal the
rendered segment edges to within one frame hop, which lets the tests check the
measured gap against the rendered gap exactly. The barge-in verdict passes for all
three (the agent yields cleanly); the latency bound
(`latency_bounds.max_response_gap_sec`, an exposed threshold in each JSON) is what
separates them.

| id | behaviour | latency outcome |
|---|---|---|
| `lat-01-prompt-response-prompt` | agent answers ~0.5 s after the caller stops | within the response-gap bound: PASS |
| `lat-02-prompt-response-sluggish` | agent stalls ~1.8 s of dead air | exceeds the bound: FAIL (latency) |
| `lat-03-prompt-response-overeager` | agent starts ~0.5 s before the caller finishes | `premature_start_sec` fires, `response_gap_sec` is null: FAIL (latency) |

### Backchannel discrimination: `scenarios/bc-*`

The caller only gives listener feedback; the correct agent holds the floor
(`did_yield` stays false, event passes).

| id | behaviour | outcome |
|---|---|---|
| `bc-01-repeated-backchannels` | four short "mhm / right / yeah / okay" across the turn | HOLD: PASS |
| `bc-02-midutterance-backchannel` | one "got it" at maximum overlap with live agent speech | HOLD: PASS |
| `bc-03-near-miss-floor-take` | a long "yeah no totally that makes sense" that briefly looks like the caller taking over | HOLD: PASS |

### funnel-demo/: a deliberately bad agent (labelled): `funnel-demo/scenarios/fd-*`

This battery demonstrates failure. The agent here both misses a real interruption
and stops for a bare backchannel, so `run_suite` over it fails on both axes and
`fixmap.systemic_pointer` returns a non-null pointer: no single sensitivity
threshold satisfies both cases at once.

| id | behaviour | outcome |
|---|---|---|
| `fd-01-missed-interruption` | agent talks straight over a 2.5 s interruption | should yield, did not: FAIL, `config` (raise sensitivity) |
| `fd-02-backchannel-yielded` | agent stops mid-sentence for a bare "mhm" (the bad twin of `bc-03`) | should hold, yielded: FAIL, `engagement-control` |

Because fixing one failure means raising the interruption threshold and fixing
the other means lowering it, the suite-level funnel pointer fires. Its message
contains no numbers by design.

The same fixtures also ship inside the package as `hotato demo`: `uvx hotato demo`
runs this battery and opens its visual report with zero checkout.

## Run them

```python
from hotato.core import run_suite

# the good references (all pass)
run_suite(suite="barge-in", scenarios_dir="examples/scenarios", audio_dir="examples/audio")

# the bad-agent battery (fails on both axes; env["funnel"] is non-null)
env = run_suite(suite="barge-in",
                scenarios_dir="examples/funnel-demo/scenarios",
                audio_dir="examples/funnel-demo/audio")
assert env["funnel"] is not None
```

or from the CLI:

```bash
hotato run --suite barge-in \
  --scenarios examples/funnel-demo/scenarios --audio examples/funnel-demo/audio --format json
```

## Regenerate (deterministic)

```bash
python examples/render_examples.py            # rewrite the committed WAVs in place
python examples/render_examples.py /tmp/out   # render elsewhere (used by the CI determinism diff)
```

`render_examples.py` is the project-local mirror of the canonical upstream
generator (`openrepo/scenarios/generate_fixtures.py`): the render algorithm is
identical and stdlib-only, the WAV bytes come from the vendored engine, and the
per-channel seed is `sha256(id)`, so two runs are byte-identical on any machine.
CI renders twice and diffs to prove it.

## Roadmap (v1.x)

These are intentional fast-follows:

- **Overlap / double-talk grading fixtures**: scoring the quality of sustained
  simultaneous speech beyond the single `talk_over_sec` number.
- **Resume / re-interruption fixtures**: grading whether the agent comes back
  cleanly after yielding, and handles a second interruption during its resume.
- **SNR / codec robustness sweeps**: the same scenarios under added noise,
  8 kHz / Opus / mu-law transcode, and level variation, to characterise the
  method's floor across channel conditions.
