# Fix plans: the guarded ladder

Hotato measures turn-taking; this ladder turns a failing measurement into a
reviewable, bounded fix proposal. Levels 0-2 are read-only. Level 3
(apply / verify) has shipped as a guarded, clone-only staged step.

## The ladder

- **Level 0** -- `hotato diagnose result.json`. What it does: per-failure
  diagnosis + a battery decision, with the advisory and the tradeoff
  stated. Writes to your stack: never.
- **Level 1** -- `hotato inspect --stack ...`. What it does: reads the
  CURRENT turn-taking config (GET or static parse) and normalizes it.
  Writes to your stack: never.
- **Level 2** -- `hotato plan result.json [target]`. What it does:
  combines diagnosis + inspected config into a fix-plan JSON
  (`hotato.fixplan.v1`). Writes to your stack: never.
- **Level 3** -- `hotato apply` / `hotato fix trial` / `hotato verify`.
  What it does: applies a plan to a CLONE and re-scores the battery on it
  under a pinned manifest. Writes to your stack: cloned assistant / branch
  only, never production.

Level 3 kept the guard it was designed with, PR-first and clone-first:
`hotato apply` applies a plan to a CLONED assistant
(or a branch config), `hotato fix trial` re-scores the battery on that clone
under a pinned manifest, and `hotato verify` gates the before/after -- it
graduates to production only behind an explicit human approval with a recorded
rollback value. Every plan still pins
`"approval": {"default": "manual", "production_apply": false}`, so every plan
built here requires a human's manual go-ahead before it reaches production. See
[APPLY.md](APPLY.md), [FIX-TRIAL.md](FIX-TRIAL.md), and
[FIX-LOOP.md](FIX-LOOP.md).

## Level 0: diagnose

```
hotato run --suite barge-in --format json > result.json
hotato diagnose result.json
```

One diagnosis per failing event:

```
{"finding":          missed_real_interruption | false_stop_on_backchannel |
                     slow_yield | excess_talk_over | endpointing_miss |
                     not_scorable | threshold_funnel,
 "evidence":         the measured fields for that event,
 "likely_layer":     interruption_detection | endpointing | unknown_root_cause,
 "config_only_safe": bool,
 "notes":            plain language}
```

plus one battery-level decision. The text mode is the Level 0 advisory, for
example: "Missed real interruption. Likely config layer. Try lowering the
stop-speaking word threshold one step. Tradeoff: may increase false stops on
short acknowledgements." The tradeoff is always stated.

Exit codes: 0 no failing events, 1 failing events diagnosed, 2 unusable input.

## Level 1: inspect

```
hotato inspect --stack vapi --assistant-id <id>      # + VAPI_API_KEY
hotato inspect --stack retell --agent-id <id>        # + RETELL_API_KEY
hotato inspect --stack livekit --config agent.py     # static parse
hotato inspect --stack pipecat --config bot.py       # static parse
```

Output: one normalized model (`interrupt_min_words`,
`interrupt_voice_seconds`, `resume_backoff_seconds`,
`endpointing_wait_seconds`, `backchannel_aware`) plus the raw fields and
provenance (what was fetched, when, and which docs the field names were
verified against). Absent or unreadable options are null with a note.
Suspicious values (an unusually high word threshold, a long endpointing wait)
are surfaced as observations, for you to judge.

Read-only by construction: Vapi and Retell are one GET each; LiveKit and
Pipecat files are parsed with `ast`, statically, as text. Missing credentials
exit 2 cleanly.

## Level 2: plan

```
hotato plan result.json --stack vapi --assistant-id <id> --out hotato-fixplan.json
hotato plan result.json                              # stack from the envelope, else generic
```

(`--run result.json` is the equivalent flag form.) The input must be a run
envelope; frame dumps, benchmark results, and compare results are rejected
with exit 2.

The plan is `hotato.fixplan.v1` (shipped schema:
`src/hotato/schema/fixplan.v1.json`). It carries the finding, a hypothesis,
zero or more changes, the verification gate, and the approval block. Every
plan also carries `kind: "fix-plan"`, the measured `evidence` behind it, the
stated `risks`, `next_commands` (apply the step manually, verify with
`hotato compare`, re-run the battery), not-scorable events as `input_issues`
(input problems, kept separate from fixes), and a `platform_mutation` block
whose `performed` is always false: hotato plan is read-only.

Twilio rule: Twilio carries the audio; the upstream voice-agent stack decides
when the agent yields. So `--stack twilio` (or a Twilio-stack envelope) gets a
checklist instead of agent-config advice: confirm the dual-channel
caller/agent assignment, then re-plan against that upstream stack.

### Policy rules (verbatim, as implemented and tested)

A change is proposed ONLY when ALL of these hold:

  (a) the failure class maps cleanly to ONE setting;
  (b) the proposed value is ONE bounded step in an unambiguous direction
      within documented bounds, anchored to the inspected current value as
      `from`; when inspection was not run or not possible the plan carries
      direction and bounds only, with `current_unknown: true`;
  (c) the run's battery contains at least one passing OPPOSITE-RISK fixture,
      else the plan DOWNGRADES to insufficient_coverage ("add an
      opposite-risk fixture before tuning") and names the exact fixture
      family to add;
  (d) the diagnosis marked the failure config_only_safe.

And the standing refusals:

* threshold_funnel: when the battery contains BOTH a missed real interruption
  AND a false stop on a backchannel, the plan is a refusal:
  `"decision": "do_not_tune_single_threshold"` with a vendor-neutral
  engagement-control pointer (no product names, no digits in the pointer
  text). No single threshold satisfies both axes; raising it for one worsens
  the other -- the threshold treadmill teams describe from the inside: tuned
  endlessly, with no perfect setting, because both failures share one knob.
* slow_yield without a clear layer: TTS buffering, transport latency, and VAD
  smoothing are indistinguishable from one recording, so the plan proposes a
  diagnostic checklist (instrumentation steps) first. A slow yield becomes
  config-only-safe only when the battery contains a passing opposite-risk
  backchannel fixture that makes a one-step change verifiable.
* not_scorable events are input problems, tracked separately from agent
  failures and kept out of the plan.

Every plan, of every decision kind, carries the same verification gate:

```
"required_verification": ["real_interruption_fixture_must_pass",
                          "backchannel_fixture_must_not_regress",
                          "slow_yield_p95_must_not_worsen"]
```

### Worked example: a safe one-step plan

Battery: the agent missed a real interruption; the backchannel fixture
passes (the opposite-risk coverage the policy requires). Inspection read
`stopSpeakingPlan.numWords = 2` from the live assistant.

```json
{
  "schema": "hotato.fixplan.v1",
  "target": {"stack": "vapi", "inspected": true,
             "assistant_id": "asst_123", "current_unknown": false},
  "finding": "missed_real_interruption",
  "hypothesis": "The agent missed a real interruption: the caller took the floor and the agent kept talking. Inspected stopSpeakingPlan.numWords is 2; one bounded step decrease is the smallest verifiable change on this axis.",
  "config_only_safe": true,
  "decision": "propose_one_step",
  "changes": [
    {"field": "stopSpeakingPlan.numWords",
     "from": 2, "to": 1, "direction": "decrease", "bounds": [0, 10],
     "reason": "The caller took the floor and the agent never stopped within the search window. A one-step sensitivity increase is a config-layer candidate; the tradeoff is more false stops on short acknowledgements. One step decrease from the inspected value. Bounds basis: documented range 0-10 (docs.vapi.ai, 2026-07-06).",
     "risk": "more false stops on short acknowledgements; the backchannel fixture must not regress"}
  ],
  "required_verification": ["real_interruption_fixture_must_pass",
                            "backchannel_fixture_must_not_regress",
                            "slow_yield_p95_must_not_worsen"],
  "approval": {"default": "manual", "production_apply": false}
}
```

One field, one step, both endpoints of the move visible, the risk named, the
verification gate attached: every value in the plan traces back to the
inspected config or a documented bound.

### Worked example: the refusal

Battery: the packaged demo (`hotato demo --format json`), which misses a real
interruption AND yields to a bare backchannel.

```json
{
  "schema": "hotato.fixplan.v1",
  "target": {"stack": "generic", "inspected": false},
  "finding": "threshold_funnel",
  "hypothesis": "The battery missed a genuine interruption and also stopped for a backchannel. One sensitivity threshold cannot satisfy both: raising it to hold through backchannels drops real interruptions, lowering it to catch interruptions yields to backchannels. The failure class is discrimination, not calibration.",
  "config_only_safe": false,
  "decision": "do_not_tune_single_threshold",
  "changes": [],
  "recommended_fix": {
    "class": "engagement-control",
    "examples": [
      "enable adaptive interruption handling where available",
      "use a backchannel-aware interruption classifier",
      "add addressee/turn-intent discrimination before stopping TTS"
    ]
  },
  "required_verification": ["real_interruption_fixture_must_pass",
                            "backchannel_fixture_must_not_regress",
                            "slow_yield_p95_must_not_worsen"],
  "approval": {"default": "manual", "production_apply": false}
}
```

The refusal is the product working as designed: the strongest thing a tuning
tool can say about a funnel is that tuning will not fix it.

## Provenance

Vendor field names and documented ranges used by inspect and plan were
verified against docs.vapi.ai (assistant startSpeakingPlan/stopSpeakingPlan),
docs.retellai.com (get-agent), docs.livekit.io (turn handling options), and
docs.pipecat.ai (user turn strategies) on 2026-07-06, and each result records
its own `field_basis`. Where a platform documents no hard range, the plan says
so and uses a conservative working range with a note to verify against the
installed version.
