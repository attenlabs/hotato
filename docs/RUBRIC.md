# Rubric evaluation: the model-judged lane (`rubric.v1`)

`hotato rubric` scores a call against a **user-authored rubric** with a pinned
**local** model, in a lane kept structurally separate from the deterministic
`assert.v1` wall. Every rubric result carries `deterministic: false` with full
provenance and lives on its own report shelf; it is never an `assert.v1` result,
never merged into a deterministic count, and never part of an overall number.

- `assert.v1`'s kinds stay `deterministic: {const: true}` forever. A model
  verdict physically cannot be an `assert.v1` result -- it lives in `rubric.v1`.
- The default judge is a **local Ollama** model (`http://localhost:11434`) reached
  with only the stdlib. Zero egress. A hosted judge is opt-in (see below).
- **Advisory by default**: a rubric FAIL is reported but never gates CI. `--gate`
  (or `--gate-judge` on `test run`) opts a team into failing on a rubric FAIL.
- Missing/insufficient evidence -> `INCONCLUSIVE`, never a fabricated verdict. A
  `human_rubric` is never model-scored -- it stays `INCONCLUSIVE`, human-required.

## The rubric object

```yaml
version: 1
rubrics:
  - id: acknowledged-frustration
    kind: judge_rubric            # or human_rubric (never model-scored)
    dimension: conversation       # optional report-dimension tag
    criterion: "Did the agent acknowledge the caller's frustration before proposing a fix?"
    evidence: [transcript]        # transcript and/or tool_trace; absent -> INCONCLUSIVE
    examples: {pass: "...", fail: "..."}      # optional
    evaluation:
      model: qwen2.5vl:3b         # optional; else --judge-model / the default
      repetitions: 1              # N model calls, aggregated
      aggregation: unanimous_or_inconclusive
      confidence_required: 0.85
    review:
      human_required_on: [fail, disagreement, confidence_below_threshold]
```

## Run it

```bash
# advisory (exit 0 regardless of verdicts)
hotato rubric run --rubrics rubrics.yaml --transcript call.json --trace trace.jsonl

# opt into CI gating on a rubric FAIL
hotato rubric run --rubrics rubrics.yaml --transcript call.json --gate

# re-query the model and DIFF against the cached verdict (surface drift)
hotato rubric run --rubrics rubrics.yaml --transcript call.json --no-cache
```

Inside a conversation-test, author the rubric in the `assertions.rubric` lane;
`hotato test run` scores it inline (advisory; `--gate-judge` to gate) and the
unified report shows a populated **Model-assisted (advisory)** shelf beside the
deterministic one -- two counts side by side, never merged.

## Result provenance (`rubric.v1`)

Every result records: the pinned `model` + its content `model_digest`, `provider`,
`prompt_id` / `prompt_version` / `prompt_sha256`, `temperature: 0`, the
`input_sha256`, the `cache_key`, `cached`, the raw `votes`, `disagreement`,
`confidence`, and `citations` to the exact transcript turns / trace events. No
`overall_score`, ever.

## Reproducibility (stated precisely, not oversold)

The model call is **not** claimed deterministic. What **is** deterministic is
**replay**: every verdict is content-addressed by
`sha256(provider:model + prompt_sha256 + input_sha256)` and cached (reusing the
content-addressed artifact store). A cache hit is byte-identical forever (same
`verdict_sha256`). `--no-cache` re-queries and **diffs** against the cached
verdict, surfacing drift instead of hiding it. `--sign` optionally signs a
cached verdict as a "judge-record" (Ed25519 `human` tier via the `[sign]` extra,
else HMAC `human-shared`), so a stored verdict is provably unmutated.

## Egress

The default local judge never leaves the box. A **hosted** judge
(`--judge-provider hosted --judge-endpoint URL`) or a **non-local**
`--judge-endpoint` sends the transcript off-box and is refused (exit 2) unless
`--judge-egress-opt-in` is passed -- the same posture as `--diarizer pyannoteai
--egress-opt-in`. See [`docs/EGRESS.md`](EGRESS.md) and
[`docs/THREAT-MODEL.md`](THREAT-MODEL.md).

## Calibration (credibility, never a marketing number)

```bash
hotato rubric calibrate --labeled ./labeled --out agreement.json
```

Each `*.json` item is `{rubric, transcript, trace?, label: pass|fail|inconclusive,
split?: train|held_out}`. Human labels are **mandatory** here -- the model is
scored *against* them, never used to create them. The command computes
**agreement** (held-out items where the model verdict equals the human label) and
**selective accuracy** (agreement restricted to items where the model did not
abstain) and writes a reproducible artifact of raw counts + method + provenance.
Re-running on the same corpus with the same model reproduces the same split and
verdicts -- it is an artifact, not a headline percentage.
