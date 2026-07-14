# explain: root-cause-by-layer, composed from what already exists

`hotato explain` reads a finished result and produces a root-cause
attribution: which layer likely failed, whether a fix is safe to try, the
opposite-risk tradeoff, and a plain next command. It's compositional, not
a new scoring engine: it reframes `hotato diagnose`'s per-event findings
and `hotato plan`'s policy gate (a mapped knob, one bounded step, a
passing opposite-risk fixture, config-only-safe), plus, for a contract
bundle, its own trust report, policy bounds, and an attached voice trace
when present.

When the evidence can't support picking ONE root cause, explain states
the reason plainly.

```bash
hotato run --suite barge-in --format json > result.json
hotato explain result.json
```

## Three input shapes, auto-detected

| Input | Example | Behavior |
| --- | --- | --- |
| Run envelope | `hotato explain result.json` | Full diagnose + policy-gate attribution, per failing event. |
| Sweep/analyze candidate ref | `hotato explain hotato-sweep.json#1` | ALWAYS refused: a candidate carries no human label. |
| Contract bundle directory | `hotato explain contracts/refund-cutoff-001.hotato` | Attributed from the contract's own measurement and policy bounds, or refused when it needs a signal the bundle lacks. |

## The attribution shape

Layer-general by design: `turn_taking` is the only populated layer today,
with room for the next (asr, tool, policy, latency, handoff, ...) without
a version bump.

```
{"event_id":         the failing event, or null for a battery-level finding,
 "failure_layer":    "turn_taking",
 "type":             missed_real_interruption | false_stop_on_backchannel |
                     false_stop_on_ambient_noise | slow_yield |
                     excess_talk_over | endpointing_miss | threshold_funnel,
 "turn_taking_layer": interruption_detection | endpointing,
 "confidence":       high | medium | low,
 "fixability":       safe_to_patch | needs_human | insufficient_evidence |
                     do_not_patch,
 "opposite_risk":    the tradeoff a fix on this axis trades against,
 "evidence_for":     measured fields and notes that support this attribution,
 "evidence_against":  the caveats against treating it as settled,
 "unknowns":         explicit gaps in the evidence (never silently assumed),
 "safe_next_action": one concrete next command, never an auto-apply}
```

`fixability` reuses the SAME gate `hotato plan` already enforces:

* `safe_to_patch` -- the failure maps to one setting AND the battery
  already has a passing opposite-risk fixture on that axis (the same
  coverage `hotato plan` requires before proposing a change).
* `insufficient_evidence` -- the failure maps to one setting but the
  opposite-risk fixture is missing, so a change couldn't be verified; or
  a contract bundle's policy-bound comparison found a violation with no
  companion moment to verify against.
* `needs_human` -- an audio-path problem (echo bleed).
* `do_not_patch` -- the threshold funnel: the battery missed an
  interruption it should have caught AND false-stopped on a backchannel.
  No single sensitivity threshold fixes both; the fix class is
  engagement-control. This adds one COMPOSITE battery-level attribution
  (`event_id: null`, `type: threshold_funnel`) alongside the two
  per-event attributions it explains.

## Refusals: a precise account of the gap

A refusal states exactly which gap in the evidence blocks attributing ONE
root cause:

* **a not-scorable event** -- an input problem.
* **an ambiguous slow yield** (`unknown_root_cause`, no passing
  opposite-risk fixture) -- TTS buffering, transport latency, and VAD
  smoothing are indistinguishable from one recording.
* **an echo-tagged false stop** -- the agent most likely heard its own
  TTS bleed, an audio-path problem.
* **a sweep/analyze candidate ref** -- ALWAYS refused (no human label);
  explain prints the promote command for BOTH labels and lets a human
  choose.
* **a contract's false stop with no disambiguating `candidate_kind`** --
  a `contract.json` carries a narrower signal set than a run envelope's
  `diagnose`, so a false stop on `hold` could be a
  backchannel-discrimination miss, ambient noise, or echo bleed. Explain
  names the ambiguity and points at `hotato run --dump-frames` / `hotato
  diagnose` on the original envelope for a definitive read.

```
{"event_id":         the refused event, or null,
 "reason":           why the evidence cannot support one attribution,
 "evidence_for":     what IS known,
 "unknowns":         the specific gap,
 "safe_next_action": one concrete next command}
```

## Contract bundles: attributed from the bundle's OWN fields, bounded

A `contract.json` carries a narrower field set than a run envelope's raw
`reasons` text, so a contract-bundle attribution is narrower than a full
`diagnose`:

* a missed interruption (`expect: yield`, `did_yield: false`) is
  unambiguous and always attributed;
* a "yielded but still failed its policy" contract is attributed by
  comparing the MEASURED `seconds_to_yield` / `talk_over_sec` against the
  contract's OWN `policy.pass_conditions` bounds -- a bound comparison,
  never a guess;
* a false stop on `hold` is attributed as echo ONLY when the contract's
  `source.candidate_kind` says so (created `--from-candidate` an
  `echo_correlated_activity` moment); otherwise it's refused (see above);
* a single bundle covers one moment only, so a contract-bundle
  attribution caps at `insufficient_evidence` until a companion contract
  or fixture on the other axis exists.

A voice trace, when attached (`hotato trace attach`), folds its findings
(`traces/voice_trace.jsonl`) into `evidence_for`; without one, explain
adds an explicit unknown -- TTS-cancellation lag, transport latency, and
VAD smoothing stay indistinguishable from timing alone. See
[`TRACE.md`](TRACE.md).

## Output

| Format | Gives you |
| --- | --- |
| text (default) | a plain summary, per attribution and per refusal |
| `--format json` | the full machine shape, schema `hotato.explain.v1` (`src/hotato/schema/explain.v1.json`) |
| `--html PATH` | a self-contained report, in the same house style (`hotato.report`'s CSS and escaping) the other HTML reports use |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | explained: nothing attributable (no failing or ambiguous events) |
| 1 | explained: at least one attribution or refusal was produced |
| 2 | usage error or unusable input (a bad candidate ref, a file that is not a hotato result, or an unreadable contract bundle) |

## What explain measures

Every attribution here is evidence-based: a measurement scoped to root
cause, with its `evidence_against` and `unknowns` stated on every record.
`explain` is read-only, like `diagnose` and `plan` -- applying a change
stays a human decision in your own stack. See
[`FIX-PLANS.md`](FIX-PLANS.md) and [`FIX-LOOP.md`](FIX-LOOP.md) for the
guarded ladder from a plan to a proven fix.
