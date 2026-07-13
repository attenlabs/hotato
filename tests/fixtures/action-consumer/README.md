# Action consumer conformance fixture

A minimal stand-in for a consumer repository that contains no hotato source:
committed suites, conversation tests, evidence files, a workflow that invokes
the root Action through the `uses: ./` Action-path mechanism, and recorded
machine results the local harness (`tests/test_action_consumer.py`) renders
summaries from. Nothing here imports repository-only helper scripts.

## Layout and expected outcomes

| Path | Case | Expected |
|---|---|---|
| `suite/pass.suite.json` | pass | exit 0, status `pass` |
| `suite/consumer.suite.json` | mixed fail (escalation handoff check fails) | exit 1, status `fail` |
| `with spaces/spaced.suite.json` | suite path containing a space | exit 0, status `pass` |
| `test/refund.conversation-test.yaml` + transcript + trace | pass; rubric judge unreachable (advisory-unavailable) | exit 0, status `pass` |
| `test/refund.conversation-test.yaml` + transcript only | required evidence missing | exit 1, status `inconclusive`, lanes INCONCLUSIVE never PASS |
| `test/two-lane.conversation-test.yaml` + transcript | only policy and conversation lanes evaluated | exit 0, outcome and speech render NOT_RUN |
| `recorded/contract-verify.pass.json` | contract verification | exit 0, embedded-assertion lanes NOT_RUN |
| `recorded/malformed.json` | unusable machine result | status `error`, never a PASS |
| `workflows/consumer.yml` | the consumer workflow shape (`uses: ./`) | run by the `action-smoke` job in `.github/workflows/tests.yml` |

## Regenerating the recorded machine results

Each recorded file is the byte output of one hotato command, run from the
repository root. The commands print JSON to stdout; save each to its file
under `recorded/`.

```bash
hotato suite run tests/fixtures/action-consumer/suite/pass.suite.json --agent consumer-agent --release fixture-release --no-registry --format json
hotato suite run tests/fixtures/action-consumer/suite/consumer.suite.json --agent consumer-agent --release fixture-release --no-registry --format json
hotato test run tests/fixtures/action-consumer/test/refund.conversation-test.yaml --agent consumer-agent --transcript tests/fixtures/action-consumer/test/refund.transcript.json --trace tests/fixtures/action-consumer/test/refund.voice_trace.jsonl --judge-endpoint http://127.0.0.1:9 --no-cache --no-store --format json
hotato test run tests/fixtures/action-consumer/test/refund.conversation-test.yaml --agent consumer-agent --transcript tests/fixtures/action-consumer/test/refund.transcript.json --judge-endpoint http://127.0.0.1:9 --no-cache --no-store --format json
hotato test run tests/fixtures/action-consumer/test/two-lane.conversation-test.yaml --agent consumer-agent --transcript tests/fixtures/action-consumer/test/refund.transcript.json --no-store --format json
```

`recorded/contract-verify.pass.json` was recorded by creating one contract
bundle from the packaged example recording and verifying it from the bundle's
parent directory, so the recorded `dir` field stays the relative `contracts`:

```bash
hotato contract create --stereo src/hotato/data/audio/01-hard-interruption.example.wav --onset 2.39 --expect yield --id fixture-yield-001 --out contracts --format json
hotato contract verify contracts --format json --junit contracts-junit.xml
```

`recorded/malformed.json` is intentionally not JSON.
