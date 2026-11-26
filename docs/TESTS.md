# Simulated-conversation tests: `hotato test run`

A test case describes a caller (a persona), what that caller wants (a goal), and
what a good call looks like (success criteria). An LLM plays the caller against
your chat agent turn by turn; a judge LLM then grades each run. Each case runs
several times and the result reports a pass rate, so one lucky run cannot hide a
flaky agent.

Voice is not part of this command: it drives chat agents.

`hotato test run` also accepts a `hotato.conversation-test` file
(see `docs/CONVERSATION-TEST.md`); the two are told apart by the file itself. A
directory, or a file with `persona` / `success_criteria` and no `kind`, is a
simulated-conversation case.

## Write a case

`hotato test new refund-damaged` writes a starter. A case is YAML when PyYAML is
importable, otherwise JSON (YAML files need `pip install pyyaml` to be read;
`test new` writes JSON when PyYAML is absent).

```yaml
name: refund-damaged
persona: "A {{mood}} customer whose order arrived damaged. Short answers."
goal: "Get a replacement or refund arranged for order {{order_id}}."
success_criteria:
  - "The agent asks for the order number or confirms the order"
  - "The agent offers a concrete next step (replacement or refund)"
forbidden:
  - "promises a refund before checking the order"
expected_tool_calls:
  - lookup_order
  - {name: create_refund, arguments: {order_id: "A-1001"}}
max_turns: 12
repeat: 3
variables: {mood: frustrated, order_id: A-1001}
```

| key | meaning |
| --- | --- |
| `name` | label (defaults to the file name) |
| `persona`, `goal` | free text; `{{variable}}` placeholders are filled from `variables` |
| `success_criteria` | plain-language statements the judge grades |
| `forbidden` | behaviours that must not happen |
| `expected_tool_calls` | tool names, or `{name, arguments}` (arguments are a subset match) |
| `max_turns` | caller turns before the run is cut off (default 12) |
| `repeat` | runs per case (default 3; `--repeat N` overrides) |

Unknown keys are refused, so a typo never silently weakens a test.

## Run it

```
hotato test run cases/ --agent chat:http://127.0.0.1:8080/chat --repeat 5 --out ./out
hotato test run refund.yaml --agent vapi:<assistant-id> --egress-opt-in --judge-provider anthropic --judge-egress-opt-in
hotato test run refund.yaml --agent retell:<agent-id> --egress-opt-in --format json
```

`--agent` takes:

- `chat:URL` (or a bare `http(s)` URL): the chat HTTP contract of
  `hotato simulate --chat` (`POST {conversation_id, turn_index, text}` ->
  `{text}`). Tool calls are not part of that contract.
- `vapi:ASSISTANT_ID`: Vapi's chat API (`POST /chat`, later turns pass
  `previousChatId`). Key from `VAPI_API_KEY` or `hotato connect vapi`.
- `retell:AGENT_ID`: Retell's chat API (`create-chat`, `create-chat-completion`);
  the case `variables` are passed as dynamic variables and tool calls are
  reported. Key from `RETELL_API_KEY` or `hotato connect retell`.

### The LLM (caller and judge)

The same LLM plays the caller and judges. Default: local Ollama (no egress).

| `--judge-provider` | endpoint / key |
| --- | --- |
| `ollama` (default) | `http://localhost:11434` or `--judge-endpoint` |
| `openai` (alias `hosted`) | any OpenAI-compatible base URL via `--judge-endpoint` / `OPENAI_BASE_URL`; key `HOTATO_JUDGE_API_KEY` or `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `gemini` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

`--judge-model` (or `HOTATO_JUDGE_MODEL`) picks the model; `HOTATO_JUDGE_PROVIDER`
picks the provider. Egress is opt-in, as everywhere in hotato: a non-local LLM
needs `--judge-egress-opt-in`; a non-local chat URL, Vapi or Retell need
`--egress-opt-in`. Refusals happen before any request.

## Read the result

`--out DIR` writes `testrun.json` (schema `hotato.testrun.v1`) and
`report.html`. `--format json` prints the same JSON; the default prints a text
summary.

Per run, each criterion and forbidden item gets a verdict
`{criterion, verdict, confidence, evidence, explanation}`:

- `pass`, `fail`, or `unknown` (the transcript gives no evidence).
- Evidence is an exact quote from the transcript. A criterion `pass` without a
  quote that really occurs in the transcript is downgraded to `unknown`. A
  forbidden item that was not violated has nothing to quote, so its `pass` needs
  none.
- Expected tool calls are checked without the LLM. A target that does not report
  tool calls yields `unknown`, never `pass`.

A run passes when it has no error and every verdict is `pass` (`unknown` is not a
pass). Per case: `pass_rate` is passed runs over repeats and `pass_all` means
every repeat passed (the pass^k idea). `criteria[].pass_rate` is per criterion.
`summary.pass_rate` is the mean of case pass rates; `summary.pass_all` counts
cases that passed every repeat.

Exit codes: 0 every case passed every repeat (or met `--min-pass-rate`), 1 any
case failed, 2 bad input, a refused egress, or no usable judge.

## Turn a failed call into a case

```
hotato test from-call call.transcript.json --name refund-regression
hotato test from-call report.json --moment "The agent confirms the refund amount before ending"
hotato test from-call call.transcript.json --at 42.5
```

The persona and goal come from the caller's own lines. The failing moment becomes
a criterion: from `--moment`, from the findings in a report, or from the agent
reply at `--at` seconds. Review and tighten the written case before relying on it.
