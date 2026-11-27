# Custom metrics: your own checks, reported like the built-in ones

A custom metric is a plain Python function that reads a call and returns
pass, fail or unknown with evidence. It runs next to hotato's built-in
assertion kinds and shows up in the same places: `hotato assert run`, both
lanes of a conversation test, the HTML and Markdown reports, and CI exit codes.

hotato's rules still apply to a custom metric. hotato checks them itself
instead of trusting the plugin:

- A pass has to cite evidence, and every citation is looked up in the call.
- A metric that needs data you did not supply is not run. Its result is
  INCONCLUSIVE.
- A deterministic metric is run twice. If the two answers differ, the run stops.
- A metric that calls a model is a judged metric. It runs only in the
  model-judged lane and is never counted with the deterministic checks.

The worked example below lives in
[`examples/custom-metrics/`](../examples/custom-metrics/) and runs as is.

## 1. Write a metric

Put a `.py` file in a `hotato_metrics/` folder next to where you run hotato.
Every module-level function whose name starts with `metric_` is a metric. Its
name is the function name without the prefix.

```python
# hotato_metrics/voice_checks.py
import re
from hotato.metrics import metric, passed, failed, unknown

def _quote_around(text, phrase):
    """The phrase with up to two words either side, as it appears in text."""
    m = re.search(r"(?:\S+\s+){0,2}" + re.escape(phrase) + r"(?:\s+\S+){0,2}",
                  text, re.IGNORECASE)
    return m.group(0) if m else None

@metric(needs=["transcript"], dimension="conversation")
def metric_greets_with_name(call):
    """The agent's first turn names the business."""
    name = call.params.get("name")
    if not name:
        return unknown("params.name is not set")
    agent_turns = call.turns(role="agent")
    if not agent_turns:
        return failed("the agent never spoke")
    index, turn = agent_turns[0]
    if name.lower() in turn["text"].lower():
        return passed([call.cite_turn(index, _quote_around(turn["text"], name))],
                      reason=f"first agent turn names {name}")
    return failed(f"first agent turn does not name {name}",
                  evidence=[call.cite_turn(index)])
```

The decorator is optional. Without it, a metric is deterministic and needs no
particular input. With it you can set:

| Argument | Meaning |
|---|---|
| `name` | the id assertions use (default: the function name without `metric_`) |
| `needs` | inputs that must be present: `transcript`, `spans`, `timing` |
| `dimension` | report dimension: `outcome`, `policy`, `conversation`, `speech` or `reliability` |
| `description` | one line for listings (default: the first docstring line) |
| `version` | bump it when the logic changes; it is reported with every result |
| `judged`, `model`, `temperature` | for metrics that call a model (section 5) |

The function is returned unchanged, so you can unit-test it directly.

### What the metric receives

`call` is a fresh copy of the call's data for each run, so a metric cannot
change what other checks read.

| Attribute or helper | What it gives you |
|---|---|
| `call.transcript` | the transcript turns (`[{"role", "text", ...}]`), or `None` |
| `call.spans` | the `hotato.voice_trace.v1` spans from `--trace`, or `None` |
| `call.timing` | the scored timing events, or `None` |
| `call.params` | the `params` mapping from the assertion |
| `call.turns(role=None)` | `(index, turn)` pairs, optionally for one role |
| `call.spans_of(type, name=None)` | `(index, span)` pairs of one span type |
| `call.cite_turn(i, quote=None)` | evidence: turn `i`, optionally a quote from it |
| `call.cite_span(i)` | evidence: span `i` of the trace |
| `call.cite_timing(event, field, value)` | evidence: a field of a timing event and its value |

A metric written as `def metric_x(call, params)` also receives the params as
its second argument.

### What the metric returns

- `passed(evidence, reason=None, measured=None)`: at least one citation is
  required. A quote that backs a pass must be at least three words, the same
  minimum `hotato test run` applies to a judge's quote, because one or two
  words ("yes", "booked") appear in almost any call.
- `failed(reason, evidence=(), measured=None)`
- `unknown(reason, measured=None)`: the call does not answer the question.
  It is reported as INCONCLUSIVE.

Each evidence item cites one thing: build it with `cite_turn`, `cite_span` or
`cite_timing`, and use one item per citation.

`measured` is an optional JSON object of numbers you want in the report, such
as `{"longest_agent_turn_words": 17}`.

## 2. Use it in an assertions file

A deterministic metric is an `assert.v1` assertion with `kind: custom`. It
sits beside built-in kinds and works with `when:` and `formula`.

```yaml
# assertions.yaml
version: 1
inconclusive_policy: refuse
assertions:
  - id: greets-with-business-name
    kind: custom
    metric: greets_with_name
    params: {name: "Harbor Dental"}
  - id: booking-claim-backed-by-tool
    kind: custom
    metric: claim_backed_by_tool
    params: {claim: "booked", tool: "book_appointment"}
  - id: no-card-numbers
    kind: pii
    detectors: [card_luhn]
    mode: must_not_leak
```

## 3. Run it

```bash
cd examples/custom-metrics
hotato assert metrics                       # list what hotato found
hotato assert run --assertions assertions.yaml \
  --transcript call.transcript.json --trace voice_trace.jsonl
```

```text
[custom] 3 pass, 0 fail, 0 inconclusive
  PASS         greets-with-business-name (custom metric greets_with_name, hotato_metrics/voice_checks.py) -- first agent turn names Harbor Dental
  PASS         short-agent-turns (custom metric max_agent_turn_words, hotato_metrics/voice_checks.py) -- longest agent turn is 17 words
  PASS         booking-claim-backed-by-tool (custom metric claim_backed_by_tool, hotato_metrics/voice_checks.py) -- the 'booked' claim is backed by a book_appointment call
deterministic: 4 pass, 0 fail, 0 inconclusive
```

Leave out `--trace` and the tool check cannot run. It needs spans, so it reports
INCONCLUSIVE, and because the file sets `inconclusive_policy: refuse` the run
exits 2:

```text
  INCONCLUSIVE booking-claim-backed-by-tool (...) -- custom metric claim_backed_by_tool needs trace, which was not supplied; the metric was not run
```

In JSON (`--format json`) a custom result is an ordinary `assert.v1` result
with a `custom` block that says where it came from:

```json
{
  "id": "booking-claim-backed-by-tool",
  "kind": "custom",
  "deterministic": true,
  "dimension": "outcome",
  "status": "PASS",
  "reason": "the 'booked' claim is backed by a book_appointment call",
  "custom": {
    "metric": "claim_backed_by_tool",
    "lane": "deterministic",
    "version": 1,
    "source": "hotato_metrics/voice_checks.py",
    "source_sha256": "…",
    "evidence": [{"turn": 3, "quote": "You're booked for Tuesday"}, {"span_id": "s_1"}]
  }
}
```

The same assertions work inside a conversation test
(`assertions.deterministic`). `hotato test run`, `hotato assert run` and
`hotato rubric run` all accept `--metrics PATH` for plugins kept somewhere
other than `./hotato_metrics/`.

## 4. How hotato checks a metric

| What happens | Result |
|---|---|
| A `needs` input was not supplied | INCONCLUSIVE; the metric is not called |
| A pass with no evidence | INCONCLUSIVE, with `claimed_status: PASS` |
| A citation that does not match the call (missing turn, a quote not in that turn, a wrong timing value) | INCONCLUSIVE, with the first mismatch as the reason |
| An evidence item that cites more than one thing, or carries keys its citation does not use | INCONCLUSIVE |
| A pass whose quote is shorter than three words | INCONCLUSIVE |
| Two different answers over the same input | the run stops, exit 2 |
| The metric raises, or returns something other than `passed` / `failed` / `unknown` | the run stops, exit 2 |
| The plugin file fails to import, or two metrics share a name | the run stops, exit 2 |
| An assertion names a metric that does not exist | exit 2 before anything runs |

When hotato turns a metric's PASS or FAIL into INCONCLUSIVE, it also prints
one warning line to stderr naming the metric and the status it returned, for
example `hotato: warning: custom metric claim_backed_by_tool returned FAIL,
reported INCONCLUSIVE: cites transcript turn 9, which does not exist`. Under
the default policy an INCONCLUSIVE does not fail the run, so without the
warning a metric with a broken citation would stop failing quietly.

`inconclusive_policy` (or `--inconclusive-policy`) decides what an
INCONCLUSIVE does to the exit code, the same as for built-in kinds: `report`
(the default) exits 0, `fail` exits 1, and `refuse` exits 2.

## 5. Metrics that call a model

A metric whose answer can change between runs, for example one that asks a
model, is declared judged. It must name the pinned model it calls and use
temperature 0:

```python
@metric(judged=True, model="qwen2.5:7b-instruct", temperature=0,
        needs=["transcript"])
def metric_polite(call):
    ...
```

A judged metric runs in the model-judged lane (`rubric.v1`, see
[RUBRIC.md](RUBRIC.md)), as a rubric with `kind: custom`:

```yaml
version: 1
rubrics:
  - id: stays-polite
    kind: custom
    metric: polite
```

The result is `deterministic: false` and advisory unless the run gates
(`hotato rubric run --gate`, or `hotato test run --gate-judge`). The plugin
is shown as the judge (`provider: custom`). The model and temperature are
listed under `declared_by_plugin`, because hotato did not watch the call to
the model. A judged metric that raises is an ERROR, the same as a model judge
that could not run.

Name a judged metric from an assertions file and hotato reports it
INCONCLUSIVE with a pointer to the rubric lane. It is never counted with the
deterministic checks.

## 6. Where hotato looks

1. `./hotato_metrics/*.py` in the directory you run from (files starting
   with `_` are skipped).
2. Each `--metrics PATH`, either a file or a folder.
3. Installed packages that declare an entry point in the `hotato.metrics`
   group. The entry point names a module (scanned the same way) or one
   decorated function:

   ```toml
   [project.entry-points."hotato.metrics"]
   acme = "acme_voice_checks.metrics"
   ```

hotato loads plugin code only when a document references `kind: custom`, or
when you run `hotato assert metrics`. Loading runs the file, the same way
pytest imports a test module, so point hotato only at code you trust. hotato's
core still depends only on the standard library; a plugin can import anything.
