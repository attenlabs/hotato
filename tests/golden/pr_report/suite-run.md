<!-- hotato-pr-report -->
## hotato: FAIL

Suite `action-consumer-suite`, agent `consumer-agent`. 2 tests: 1 pass, 1 fail. Exit code 1.

### Not passing (1)

| Status | Case | What failed | Evidence | Files |
|---|---|---|---|---|
| **FAIL** | `escalate-not-handed-off-test` | policy check failed | policy: handoff: no handoff to 'human_supervisor' span occurred in the trace; 0/3 runs passed | - |

### Compared with the base branch (`main`)

Fixed: **0** · Newly failing: **1** · Still failing: **0**

| Change | Case | Base | This PR |
|---|---|---|---|
| newly failing | `escalate-not-handed-off-test` | not on base | **FAIL** |

Results: [workflow run](https://github.com/octo/voice/actions/runs/123).

<sub>Deterministic checks decide this verdict; it re-renders byte-for-byte from the same result files. This comment is updated in place on every run.</sub>
