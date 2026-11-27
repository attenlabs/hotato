<!-- hotato-pr-report -->
## hotato: INCONCLUSIVE

Conversation test `refund-flow`, agent `consumer-agent`. 4 checks: 1 pass, 3 inconclusive. Exit code 1.

### Not passing (3)

| Status | Case | What failed | Evidence | Files |
|---|---|---|---|---|
| INCONCLUSIVE | `refund-flow / refund-tool-called` | outcome check missing required evidence | no trace was provided in the context; tool_call assertions read voice_trace.v1 spans, never transcript text | - |
| INCONCLUSIVE | `refund-flow / lookup-then-refund` | conversation check missing required evidence | no trace was provided; sequence reads voice_trace.v1 spans | - |
| INCONCLUSIVE | `refund-flow / refund-latency` | speech check missing required evidence | no trace was provided; latency reads span latency_ms | - |

### Compared with the base branch

Fixed: **0** · Newly failing: **0** · Still failing: **0** · Now inconclusive: **3**

| Change | Case | Base | This PR |
|---|---|---|---|
| now inconclusive | `refund-flow / refund-tool-called` | PASS | INCONCLUSIVE |
| now inconclusive | `refund-flow / lookup-then-refund` | PASS | INCONCLUSIVE |
| now inconclusive | `refund-flow / refund-latency` | PASS | INCONCLUSIVE |

Model-judged rubric lane (advisory, does not change the verdict): 0 pass, 0 fail, 0 inconclusive, 1 error.

<sub>Deterministic checks decide this verdict; it re-renders byte-for-byte from the same result files. This comment is updated in place on every run.</sub>
