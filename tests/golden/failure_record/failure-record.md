# Outcome failed: No tool call satisfied the declared call conditions.

`FAIL` failure record for `refund-claimed-not-issued` (origin `captured`).

| Dimension | Status | Observed |
|---|---|---|
| Outcome | FAIL | No tool call satisfied the declared call conditions. |
| Policy | PASS | The policy assertion passed against the supplied evidence. |
| Conversation | PASS | The latency assertion passed against the supplied evidence. |
| Speech | INCONCLUSIVE | Required input for the latency assertion was absent. |
| Reliability | NOT_RUN | No assertion ran in this dimension. |

**Deterministic gate:** `FAIL` (policy `all_deterministic_assertions_pass`)  
**Model advisory:** UNAVAILABLE (gate not enabled, reason backend-not-requested)

## Primary assertion

- Rule: `assert.tool_call`
- Status: `FAIL`
- Expected: The declared tool_call conditions hold against the supplied evidence.
- Observed: No tool call satisfied the declared call conditions.
- Evidence references: `assertion-refund-issued-evidence`

## Reliability

pass@1=0.400 pass@k=1.000 pass^k=0.000 (2 of 5 trials passed); 95% Wilson interval [0.117621, 0.769280]

## Reproduce

```bash
hotato record render source-result.json --out record
```

`sha256:a53c49db05029463aba6406a926d991c4566576661077d3e9e922dd5e2609b98` · hotato 0.0.0-golden · privacy profile `share-safe-v1`
