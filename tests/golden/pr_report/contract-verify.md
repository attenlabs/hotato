<!-- hotato-pr-report -->
## hotato: FAIL

Contract verification of `contracts`. 3 contracts: 1 pass, 1 fail, 1 not scorable. Exit code 1.

### Not passing (2)

| Status | Case | What failed | Evidence | Files |
|---|---|---|---|---|
| **FAIL** | `greeting-hold` | turn-taking regression \(expect hold\) | agent yielded after 0.36 s; talk-over 0.36 s | [record](https://github.com/octo/voice/actions/runs/123) · [card](https://github.com/octo/voice/actions/runs/123) |
| NOT_SCORABLE | `callback-yield` | recording not scorable | the agent channel is silent for the whole window | - |

### Caught moment

`greeting-hold` (expect hold): **agent yielded after 0.36 s; talk-over 0.36 s**

```text
caller  ............XXXXXX============
agent   ============XXXXXX............
= speaking   X both talking (talk-over)   . silent
```

This result re-measures stored evidence. It does not test the current agent.

### Compared with the base branch (`main`)

Fixed: **1** · Newly failing: **1** · Still failing: **0** · Now inconclusive: **1**

| Change | Case | Base | This PR |
|---|---|---|---|
| newly failing | `greeting-hold` | PASS | **FAIL** |
| fixed | `refund-yield` | FAIL | PASS |
| now inconclusive | `callback-yield` | not on base | NOT_SCORABLE |

1 case(s) on the base are not in this run.

### Evidence

In the results artifact of [this workflow run](https://github.com/octo/voice/actions/runs/123):

- `greeting-hold`: Failure Record `records/sha256-5e05f9f1d23e26b84f3c82bce9fdd78789dad072c1f59ef11dbee3aecf1dfda1/failure-record.md`, evidence card `cards/greeting-hold.svg`

Reproduce locally:

```bash
hotato contract verify contracts --format json
```

<sub>Deterministic checks decide this verdict; it re-renders byte-for-byte from the same result files. This comment is updated in place on every run.</sub>
