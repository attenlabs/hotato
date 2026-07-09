# verify policy example

`hotato.verify.yaml` turns the measured before/after rollup from `hotato verify`
into a PASS/FAIL gate you can drop into CI.

```
hotato verify --before before.json --after after.json --policy hotato.verify.yaml
```

The policy has two parts, and **both** must hold for the check to pass:

- **`target.improve`** -- the success criteria (the failure the fix set out to
  move). A signed number is a required delta (`after - before`), so
  `talk_over_sec_p95: -0.5` means the pooled talk-over p95 must drop by at least
  0.5s. A keyword (`decrease`, `increase`, `no_worse`, `no_better`, `unchanged`)
  states direction only, so `failed_count: decrease` means fewer fixtures may
  fail than before.
- **`guardrails`** -- hard fail conditions. `max_new_false_yields` and
  `max_not_scorable` cap what a naive threshold bandaid would silently trade in;
  `require_hold_fixture` / `require_yield_fixture` refuse to certify a battery
  that does not even test the opposite axis.

This is the anti-bandaid gate: verify passes only when every guardrail holds AND
every target is met, so you cannot pass by improving talk-over on the yield side
while introducing false yields on the hold side. A patch that cuts talk-over by
making the agent yield to everything meets the talk-over target but trips
`max_new_false_yields`, and the whole check fails (exit 1).

Supported target metrics: `talk_over_sec_p95`, `seconds_to_yield_p95`,
`failed_count`, `false_yield_count`. Supported guardrails: `max_new_false_yields`,
`max_not_scorable`, `require_hold_fixture`, `require_yield_fixture`.

The policy is parsed with the standard library only (Hotato's core carries no
third-party runtime dependency); the supported YAML is exactly the small subset
this example uses. Verify reports **coincidence, never causation**.
