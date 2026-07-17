# Bounded generative caller

`hotato.caller` runs caller-side conversation programs against a media or
signaling session supplied by an adapter. It supports scripts, state-reactive
graphs, allow-listed model proposals, and model-free frozen replay. The output
is a content-addressed evidence package.

The caller has participant authority only. It cannot determine whether the
agent completed a task, mutate backend state, write tool results, or produce a
test verdict. Feed the captured call, tool trace, and state evidence into the
separate Hotato exercise/evaluation path after the caller finishes.

## Execution modes

| Mode | Model use | Intended use |
|---|---:|---|
| `scripted` | forbidden | fixed regression inputs |
| `hybrid` | only at `generate` nodes | deterministic flow with bounded caller variation |
| `generative` | required | state-reactive caller programs with validated proposals |
| `frozen_replay` | forbidden | replay the text/PCM and signaling actions bound by a prior package |

Frozen replay repeats caller outputs. It does not claim to reproduce the
agent, carrier, network, or timing scheduler. When a source action contains
PCM, replay sends those PCM16LE bytes and refuses a transport without
`send_audio`. When the source action contains text only, the text is replayed;
any downstream speech synthesis remains outside the evidence boundary.

## Plan

Plans use `hotato.caller-plan.v1`. Every loop and resource has a hard bound.
This example listens for a refund request, observes a successful tool event,
then allows a model to propose only caller speech.

```json
{
  "schema": "hotato.caller-plan.v1",
  "id": "refund-follow-up",
  "mode": "hybrid",
  "start": "greeting",
  "initial_state": {"journey": "refund"},
  "limits": {
    "max_steps": 40,
    "max_turns": 8,
    "max_duration_ms": 120000,
    "max_model_calls": 2,
    "max_tokens": 2000,
    "max_cost_microusd": 0
  },
  "nodes": [
    {
      "id": "greeting",
      "type": "say",
      "text": "I need help with a refund.",
      "next": "agent-reply"
    },
    {
      "id": "agent-reply",
      "type": "listen",
      "timeout_ms": 10000,
      "max_events": 8,
      "until": {"event": "tool_result", "tool": "refund", "status": "success"},
      "next": "follow-up",
      "on_timeout": "leave"
    },
    {
      "id": "follow-up",
      "type": "generate",
      "prompt": "Ask for the refund confirmation identifier in one sentence.",
      "allowed_actions": ["say"],
      "next": "leave"
    },
    {"id": "leave", "type": "hangup", "reason": "scenario_complete"}
  ]
}
```

The code API is intentionally adapter-injected:

```python
from hotato.caller import load_plan, run_caller

result = run_caller(
    load_plan("refund.caller.json"),
    session=my_session,
    output_dir="artifacts/refund-caller",
    model=my_caller_model,
    tts=my_tts,
)
assert result.verification["ok"]
```

## Graph nodes

| Node | Effect |
|---|---|
| `say` | sends fixed participant text, or bound PCM when a TTS adapter is present |
| `generate` | asks a model for one allow-listed action and validates its exact shape |
| `listen` | consumes bounded session events until a trigger matches or times out |
| `wait` | advances the adapter's caller schedule |
| `dtmf` | sends declared DTMF digits |
| `silence` | emits a declared silence interval through the adapter |
| `impairment` | asks the adapter to apply a named/configured media profile |
| `expect` | branches on already observed events or caller working state |
| `set_state` | updates caller working state; outcome/verdict roots are forbidden |
| `branch` | takes the first matching trigger, with an optional default |
| `repeat_bounded` | visits a target at most `max_iterations` times |
| `transfer_expect` | requires observable evidence of a completed transfer |
| `hangup` | ends the caller leg with a recorded reason |

Every run also enforces `max_steps`, `max_visits_per_node`, `max_events`,
`max_event_chars`,
`max_wait_ms`, model request/response character limits, text and PCM limits,
token limits, and a micro-USD spend ceiling. The default spend ceiling is zero.
A hosted model adapter must report integer usage and an operator must raise the
ceiling explicitly. Missing usage is a blocked run.

## Triggers

Triggers are deterministic predicates over normalized session events. They can
be combined with `all`, `any`, and `not`.

```json
{
  "any": [
    {"event": "transcript", "text_regex": "refund"},
    {"event": "transcript", "text_regex": "chargeback"}
  ]
}
```

```json
{"event": "tool_result", "tool": "refund", "status": "success"}
```

```json
{"event": "state_snapshot", "path": "subscription.status", "equals": "cancelled"}
```

```json
{"event": "timing", "metric": "agent_latency_ms", "gte": 800}
```

The normalized event kinds are `transcript`, `tool_result`, `state_snapshot`,
`dtmf`, `lifecycle`, `transfer`, `hold`, `timing`, `timeout`, and `custom`.
An adapter may put fields at the top level or under `data`. Each accepted event
receives a canonical SHA-256 identity in the result.

`text_regex` uses a 512-character safe subset: literals, escaped characters,
`.`, character classes, boundary `^`/`$`, and bounded flat repeats. Groups,
alternation, lookaround, backreferences, nested repetition, more than one
unbounded repeat, unanchored unbounded repeats, and repeat sets exceeding the
fixed path budget are refused when the plan is loaded. Regex searches inspect
at most 1,024 characters; a
larger candidate text ends the caller run with the stable
`REGEX_SEARCH_TEXT_LIMIT` `ERROR`. Use `any` for alternatives, as above.

## Session contract

The engine does not implement SIP, RTP, WebRTC, telephony, STT, or TTS. A
`CallerSession` owns those operations:

```python
class CallerSession:
    def capabilities(self): ...
    def send_text(self, text, metadata): ...
    def send_audio(self, pcm_s16le, sample_rate_hz, metadata): ...
    def receive(self, timeout_ms): ...
    def send_dtmf(self, digits): ...
    def wait(self, duration_ms): ...
    def silence(self, duration_ms): ...
    def set_impairment(self, profile): ...
    def hangup(self, reason): ...
```

Capabilities use exactly three states:

- `SUPPORTED`: the requested operation can be executed and observed at this
  boundary.
- `UNSUPPORTED`: the adapter declares that it cannot perform the operation.
- `UNOBSERVABLE`: the operation may occur, but the adapter cannot supply the
  evidence required by the node.

Missing capabilities are treated as `UNSUPPORTED`. A transfer expectation with
`UNOBSERVABLE` evidence produces `CAPABILITY_UNOBSERVABLE`; it is never recoded
as a failed transfer.

Concrete adapters should run on top of maintained media substrates. Examples
include a Pipecat transport for streaming caller audio, a LiveKit room/SIP
sidecar for WebRTC or SIP legs, and a SIPp process for fixed high-rate SIP
fixtures. The caller engine controls the program and evidence contract while
those projects own their protocols.

Two optional concrete adapters cover common local paths without changing that
separation: `LiveKitCallerSession` joins a LiveKit room through the Python RTC
SDK, and `PiperCallerTTS` invokes a local Piper model with bounded,
digest-bound provenance. See [Direct LiveKit caller session](LIVEKIT-CALLER-SESSION.md)
and [Local Piper caller speech](PIPER-CALLER-TTS.md). Neither adapter claims
PSTN delivery, carrier behavior, or speech quality from successful local
execution.

## Model contract and authority boundary

A `CallerModel.propose(request)` response has six required fields:

```json
{
  "proposal": {"action": "say", "text": "What is the confirmation number?"},
  "raw": "provider response or parsed response text",
  "provider": "local-provider-name",
  "model": "model-identifier",
  "parameters": {"temperature": 0, "seed": 7},
  "usage": {"input_tokens": 120, "output_tokens": 12, "cost_microusd": 0}
}
```

The only model-proposable actions are `say`, `dtmf`, `silence`, and `hangup`.
Each `generate` node narrows that set with `allowed_actions`. The proposal must
contain the exact fields for its action. Extra fields are refused. `set_state`,
tool results, backend state, assertions, outcomes, and verdicts cannot be model
proposals.

The model request and response are saved separately and bound by SHA-256. The
run records provider, model identifier, parameters, usage, and proposal. This
is provenance, not a claim that hosted inference is repeatable.

### Local Ollama adapter

`OllamaCallerModel` is the built-in zero-dependency model adapter. It accepts
loopback HTTP endpoints only, refuses redirects, requests one JSON object, and
requires Ollama's input/output token counts.

```python
from hotato.caller import OllamaCallerModel

model = OllamaCallerModel(
    "your-local-caller-model",
    endpoint="http://127.0.0.1:11434",
    temperature=0,
    seed=7,
)
```

The adapter records a zero API charge because the daemon is local. Hardware,
energy, and operator costs are not represented by `cost_microusd`. A hosted
model remains an injected `CallerModel` implementation and must report its
metered charge.

## TTS contract

`CallerTTS.synthesize(text)` returns:

```python
{
    "pcm_s16le": pcm_bytes,
    "sample_rate_hz": 16000,
    "provider": "local-tts",
    "model": "model-id",
    "voice": "voice-id",
    "settings": {"speed": 1.0},
}
```

All fields are required. The run writes the exact PCM16LE bytes and the UTF-8
source text as separate artifacts and records both hashes. No text-only
fallback occurs when a TTS adapter is configured and `send_audio` is absent.

## Evidence package

Each output directory contains:

```text
caller-plan.json
caller-result.json
package-manifest.json
artifacts/
  text/             # exact UTF-8 participant utterances
  audio/            # exact PCM16LE submitted at the caller session boundary
  model-request/    # canonical request per model call
  model/            # canonical response/provenance per model call
```

`package-manifest.json` binds the exact file set, byte sizes, and SHA-256
digests. `verify_package()` rejects missing, changed, symlinked, path-escaping,
and unlisted files. Frozen replay verifies the complete source package before
performing any session action.

`caller-result.json` keeps these lanes separate:

- `actions`: participant outputs and signaling requests;
- `events`: normalized observations supplied by the session adapter;
- `model_calls`: model request/response provenance and usage;
- `actor_state`: scenario-owned working memory;
- `authority`: fixed declarations that outcome is unevaluated and no verdict
  was produced.

`COMPLETED` and `HUNG_UP` exit zero. Capability gaps, invalid model/TTS output,
and resource limits return a verifiable `BLOCKED` package. Adapter exceptions
return a verifiable `ERROR` package. Neither state is converted into an agent
quality verdict.

## Frozen replay

```json
{
  "schema": "hotato.caller-plan.v1",
  "id": "refund-follow-up-replay",
  "mode": "frozen_replay",
  "frozen_package": "artifacts/refund-caller"
}
```

No model or TTS adapter is called. The replay result names the verified source
package ID. Tampering with source text, PCM, model material, plan, or result
blocks replay before a session operation occurs.

## Remaining acceptance work for a concrete transport

This engine does not establish that a particular adapter delivers configured
audio or signaling to a target. Before publishing transport claims, execute a
common fixture through the adapter and retain target-boundary captures for PCM,
DTMF, hold, transfer, disconnect, and impairment behavior. Report unsupported
and unobservable operations as such. Do not infer carrier behavior from a
successful local method call.
