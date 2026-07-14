# explain: root-cause-by-layer, composed from what already exists

`hotato explain` reads a finished result and turns it into a root-cause
attribution: which layer likely failed, whether a fix is safe to try, the
opposite-risk tradeoff, and a plain next command. It is compositional: no new
scoring engine. It reframes `hotato diagnose`'s per-event findings and the
same policy gate `hotato plan` enforces (a mapped knob, one bounded step, a
passing opposite-risk fixture in the battery, config-only-safe), plus, for a
contract bundle, the bundle's own trust report and policy bounds and an
attached voice trace when present.

When the evidence cannot support picking ONE root cause, explain states the
reason plainly.

```
hotato run --suite barge-in --format json > result.json
hotato explain result.json
```

## Three input shapes, auto-detected

- **A run envelope** -- example: `hotato explain result.json`. What happens:
  full diagnose + policy-gate attribution, per failing event.
- **A sweep/analyze candidate ref** -- example:
  `hotato explain hotato-sweep.json#1`. What happens: ALWAYS refused -- a
  candidate carries no human label.
- **A contract bundle directory** -- example:
  `hotato explain contracts/refund-cutoff-001.hotato`. What happens:
  attributed from the contract's own measurement + policy bounds, or
  refused when disambiguation needs a signal the bundle does not carry.

## The attribution shape

Layer-general by design, so a future layer (asr, tool, policy, latency,
handoff, ...) can be added without a version bump. Only `turn_taking` is
populated in this build:

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

* `safe_to_patch` -- the failure maps to one setting AND the battery already
  contains a passing opposite-risk fixture on that axis (the same coverage
  `hotato plan` requires before it proposes a change).
* `insufficient_evidence` -- the failure maps to one setting but the
  opposite-risk fixture is missing, so a change could not be verified; or a
  contract bundle's own policy-bound comparison found a violation with no
  companion moment in the bundle to verify a change against.
* `needs_human` -- an audio-path problem (echo bleed).
* `do_not_patch` -- the threshold funnel: the battery missed a real
  interruption AND false-stopped on a backchannel in the SAME battery. No
  single sensitivity threshold can fix both; the fix class is
  engagement-control. This produces one COMPOSITE
  battery-level attribution (`event_id: null`, `type: threshold_funnel`) in
  addition to the two per-event attributions it explains.

## Refusals: a precise account of the gap

A refusal states exactly which gap in the evidence blocks attributing ONE root
cause:

* **a not-scorable event** -- an input problem.
* **an ambiguous slow yield** (`unknown_root_cause`, no passing opposite-risk
  fixture) -- TTS buffering, transport latency, and VAD smoothing are
  indistinguishable from one recording.
* **an echo-tagged false stop** -- the agent most likely heard its own TTS
  bleed, an audio-path problem.
* **a sweep/analyze candidate ref** -- ALWAYS refused. A candidate carries no
  human label (`yield` vs `hold`); explain prints the exact promote command
  for BOTH labels and lets a human choose.
* **a contract's false stop with no disambiguating `candidate_kind`** -- a
  `contract.json` does not carry the raw echo/ambient signal a full run
  envelope's `diagnose` has, so a false stop on `hold` could be a
  backchannel-discrimination miss, ambient non-speech noise, or echo bleed.
  Explain names the ambiguity and points back at
  `hotato run --dump-frames` / `hotato diagnose` on the original envelope for
  a definitive read.

```
{"event_id":         the refused event, or null,
 "reason":           why the evidence cannot support one attribution,
 "evidence_for":     what IS known,
 "unknowns":         the specific gap,
 "safe_next_action": one concrete next command}
```

## Contract bundles: attributed from the bundle's OWN fields, bounded

`contract.json` does not carry the scorer's raw `reasons` text a run
envelope's event does, so a contract-bundle attribution is deliberately
narrower than a full `diagnose`:

* a missed interruption (`expect: yield`, `did_yield: false`) is unambiguous
  and always attributed;
* a "yielded but still failed its policy" contract is attributed by comparing
  the MEASURED `seconds_to_yield` / `talk_over_sec` against the contract's OWN
  `policy.pass_conditions` bounds -- a bound comparison, never a guess;
* a false stop on `hold` is attributed as echo ONLY when the contract's
  `source.candidate_kind` says so (it was created `--from-candidate` an
  `echo_correlated_activity` moment); otherwise it is refused (see above);
* a single bundle carries coverage for one moment only, so a contract-bundle
  attribution caps at `insufficient_evidence` until a companion contract or
  fixture on the other axis exists.

When a voice trace is attached (`hotato trace attach`), its findings
(`traces/voice_trace.jsonl`) are folded into `evidence_for`; when it is not
attached, explain adds an explicit unknown saying so -- TTS-cancellation lag,
transport latency, and VAD smoothing stay indistinguishable from timing alone
without one. See [`TRACE.md`](TRACE.md).

## Output

* **text** (default): a plain summary, per attribution and per refusal.
* **`--format json`**: the full machine shape, schema
  `hotato.explain.v1` (`src/hotato/schema/explain.v1.json`).
* **`--html PATH`**: a self-contained report, reusing the same house style
  (`hotato.report`'s CSS and escaping) the other HTML reports use.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | explained: nothing attributable (no failing or ambiguous events) |
| 1 | explained: at least one attribution or refusal was produced |
| 2 | usage error or unusable input (a bad candidate ref, a file that is not a hotato result, or an unreadable contract bundle) |

## What explain measures

Every attribution here is evidence-based -- a measurement scoped to root
cause, with its `evidence_against` and `unknowns` stated on every record.
`explain` is read-only, exactly like `diagnose` and `plan`: applying a change
stays a human decision in your own stack; see [`FIX-PLANS.md`](FIX-PLANS.md)
and [`FIX-LOOP.md`](FIX-LOOP.md) for the guarded ladder from a plan to a
proven fix.
