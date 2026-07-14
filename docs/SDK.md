# Python SDK (`hotato.sdk`)

`hotato.sdk` is a typed facade over the same functions the CLI runs. It imports the internal code paths directly, so there is no subprocess. Every result is a frozen dataclass whose fields are the keys of the JSON the CLI emits with `--format json`.

## Install

`pip install hotato` ships the SDK and its `py.typed` marker, so a type checker reads the package as typed. The scoring core is stdlib-only. Transcription uses the optional `hotato[transcribe]` extra.

## Run a suite

```python
from hotato.sdk import run_suite

result = run_suite()                  # the bundled labelled battery, zero files
print(result.passed, result.failed)   # process pass (exit 0) and failure count
for event in result.events:
    print(event.event_id, event.passed, event.seconds_to_yield, event.talk_over_sec)
```

Point `run_suite` at your own labelled set with `run_suite(scenarios="scenarios/", audio="audio/")`.

## Verify contracts

```python
from hotato.sdk import verify_contracts

result = verify_contracts("contracts/")
print(result.passed, result.summary["failed"])
for c in result.results:
    print(c.id, c.passed, c.authenticity)   # authenticity passes through verbatim
```

A regressed or tampered contract sets `passed` to `False` with `exit_code` 1. It returns as data; it does not raise. A missing or corrupt bundle raises `ValueError`.

## Compile a counterexample

```python
from hotato.sdk import compile_counterexample, verify_counterexample

compiled = compile_counterexample(
    "case.scenario.json", "case.test.json",
    target="pii-email", out="case.hotato-repro")
print(compiled.minimality, compiled.output)

verified = verify_counterexample("case.hotato-repro")
print(verified.status, verified.passed)
```

A deterministic refusal raises `CounterexampleRefusal`, which carries a stable `.code`.

## The JSON contract

The dataclass fields are the CLI's JSON keys. `SuiteResult`, `ContractVerifyResult`, `InvestigateResult`, `CounterexampleResult`, and `CounterexampleVerifyResult` mirror `hotato run`, `hotato contract verify`, `hotato investigate`, and `hotato counterexample`. A `SuiteResult.passed` is `exit_code == 0`, and `failed` is the count of failed scorable events. A NOT-SCORABLE or INCONCLUSIVE result keeps its own status; nothing is blended into a single number.

## Errors

Bad input raises `ValueError` and its subclasses. A missing optional extra raises `BackendUnavailable`. A counterexample refusal raises `CounterexampleRefusal`. All of these sit inside `hotato.errors.HANDLED`, the same set the CLI and MCP surfaces map to a structured error.

## Transcription

```python
from hotato.sdk import transcribe, build_transcript_cache

cache, _warning = build_transcript_cache()
transcript = transcribe("call.wav", cache=cache)
print(transcript.text)
```

A transcript is context beside the timing score. It leaves `did_yield`, `seconds_to_yield`, and `talk_over_sec` unchanged. For the cache-hit flag and the cache key, call `transcribe_cached` and read its `CachedTranscribeResult`.
