# refund-issued failed: expected a refund.create tool call; none was found in the trace

`FAIL` failure record for `refund-claimed-not-issued` (origin `captured`).

| Dimension | Status | Observed |
|---|---|---|
| Outcome | FAIL | expected a refund.create tool call; none was found in the trace |
| Policy | PASS | The policy assertion passed against the supplied evidence. |
| Conversation | PASS | The latency assertion passed against the supplied evidence. |
| Speech | INCONCLUSIVE | no timing context was provided for a latency field |
| Reliability | NOT_RUN | No assertion ran in this dimension. |

**Deterministic gate:** `FAIL` (policy `all_deterministic_assertions_pass`)  
**Model advisory:** UNAVAILABLE (gate not enabled, reason backend-not-requested)

## Primary assertion

- Rule: `assert.tool_call`
- Status: `FAIL`
- Expected: The declared tool_call conditions hold against the supplied evidence.
- Observed: expected a refund.create tool call; none was found in the trace
- Evidence references: `assertion-refund-issued-evidence`

## Reliability

pass@1=0.400 pass@k=1.000 pass^k=0.000 (2 of 5 trials passed); 95% Wilson interval [0.117621, 0.769280]

## Reproduce

```bash
hotato record render source-result.json --out record
```

`sha256:4fba280a24712a25e863b1bd5f5857801aafd5e0b03bebfa40f1966c120d8c6e` · hotato 0.0.0-golden · privacy profile `share-safe-v1`
