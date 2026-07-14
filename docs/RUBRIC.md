# Rubric evaluation: the model-judged lane (`rubric.v1`)

`hotato rubric` scores a call against a **user-authored rubric** with a
pinned **local** model, structurally separate from the deterministic
`assert.v1` wall. Every rubric result carries `deterministic: false` with
full provenance and lives on its own report shelf, counted apart from any
overall number.

- `assert.v1`'s kinds stay `deterministic: {const: true}` forever. A model
  verdict lives in its own `rubric.v1` lane, structurally apart from
  `assert.v1`.
- The default judge is a **local Ollama** model (`http://localhost:11434`),
  reached with only the stdlib. Zero egress; a hosted judge is opt-in (see
  below).
- **Advisory by default**: the deterministic checks set the gate, and a
  rubric FAIL is reported while the model's read stays advisory. `--gate`
  (or `--gate-judge` on `test run`) opts a team into gating on a rubric FAIL
  too.
- Missing or insufficient evidence resolves to `INCONCLUSIVE`, always
  labeled plainly. A `human_rubric` stays `INCONCLUSIVE` and human-required,
  by design.

## The rubric object

```yaml
version: 1
rubrics:
  - id: acknowledged-frustration
    kind: judge_rubric            # or human_rubric (human-scored only)
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

Inside a conversation-test, author the rubric in the `assertions.rubric`
lane; `hotato test run` scores it inline (advisory; `--gate-judge` to gate)
and the unified report shows a populated **Model-assisted (advisory)** shelf
beside the deterministic one -- two counts side by side, each on its own
lane.

## Result provenance (`rubric.v1`)

Every result records: the pinned `model` + its content `model_digest`,
`provider`, `prompt_id` / `prompt_version` / `prompt_sha256`,
`temperature: 0`, the `input_sha256`, the `cache_key`, `cached`, the raw
`votes`, `disagreement`, `confidence`, and `citations` to the exact
transcript turns / trace events -- its own number, scored on its own lane;
the `rubric.v1` schema rejects an `overall_score` key.

## Reproducibility (stated precisely)

**What's guaranteed is replay.** Every verdict is content-addressed by
`sha256(provider:model + prompt_sha256 + input_sha256)` and cached (reusing
the content-addressed artifact store), so a cache hit reproduces the same
`verdict_sha256` every time. A fresh model call is a separate question,
checked explicitly with `--no-cache`, which re-queries the model and
**diffs** the result against the cached verdict, surfacing any drift
directly. `--sign` optionally signs a cached verdict as a "judge-record"
(Ed25519 `human` tier via the `[sign]` extra, else HMAC `human-shared`), so
a stored verdict stays provably unmutated.

## Egress

The default local judge stays on the box. A **hosted** judge
(`--judge-provider hosted --judge-endpoint URL`) or a **non-local**
`--judge-endpoint` sends the transcript off-box, and requires
`--judge-egress-opt-in` to proceed (exit 2 without it) -- the same posture
as `--diarizer pyannoteai --egress-opt-in`. See
[`docs/EGRESS.md`](EGRESS.md) and [`docs/THREAT-MODEL.md`](THREAT-MODEL.md).

## Calibration: measured credibility

```bash
hotato rubric calibrate --labeled ./labeled --out agreement.json
```

Each `*.json` item is `{rubric, transcript, trace?, label: pass|fail|inconclusive,
split?: train|held_out}`. Human labels are **mandatory** here: humans author
the labels, and the model is scored *against* them. The command computes
**agreement** (held-out items where the model verdict equals the human
label) and **selective accuracy** (agreement restricted to items where the
model committed to a verdict), and writes a reproducible artifact of raw
counts, method, and provenance. Re-running on the same corpus with the same
model reproduces the same split and verdicts -- the numbers travel with
their method and provenance built in.
