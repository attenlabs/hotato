# How do I verify DTMF reached the far end?

Verify it at the evidence layer: declare a deterministic `dtmf` assertion and
hotato checks the expected digits against the voice trace your pipeline
logged, stamps every DTMF send with the authority that observed it, and
returns INCONCLUSIVE instead of a guess when the evidence is missing. The
receipt lives wherever your far end logs what it decoded: put that log line
into the trace, and the assertion becomes a far-end check.

## The dtmf assertion, end to end

DTMF evidence enters hotato through two doors. The scripted live caller
([`hotato caller run`](../GENERATIVE-CALLER.md), driving a session over
`--target-ws` or a LiveKit room) has a `dtmf` action whose every send is
recorded as an evidence row naming the digits and the authority that observed
it; where the transport carries no delivery receipt, the row says so
(`target_delivery: UNOBSERVABLE`) instead of implying one. The second door is
the voice trace: any component that observes DTMF (your telephony webhook,
your IVR's decoder, your agent backend) logs a `dtmf` span into
`hotato.voice_trace.v1`, and the deterministic assertion lane gates on it.

The gate is a conversation-test with a `dtmf` assertion:

```yaml
# dtmf.test.yaml (trimmed)
assertions:
  deterministic:
    - id: dtmf-reached-billing-menu
      kind: dtmf
      digits: "42"
      dimension: outcome
success:
  required: [all_deterministic_assertions_pass]
```

and a trace whose `dtmf` span carries the digits the observer saw:

```json
{"_meta": true, "schema": "hotato.voice_trace.v1", "call_id": "dtmf-demo-call-1", "created_at": "2026-07-18T00:00:00Z"}
{"type": "dtmf", "digits": "42", "start_ms": 1000, "end_ms": 1200}
```

Run it:

```console
$ hotato test run dtmf.test.yaml --agent my-agent-v1 --trace voice_trace.jsonl --out ./conv-artifact
wrote conversation artifact to ./conv-artifact/
hotato test run: dtmf-demo (agent my-agent-v1) -- exit_code=0
inconclusive_policy: report
success: PASS  (required: all_deterministic_assertions_pass)
  [ok] all_deterministic_assertions_pass
```

The bound artifact records the assertion with the span it matched:

```json
{
  "deterministic": true,
  "dimension": "outcome",
  "id": "dtmf-reached-billing-menu",
  "kind": "dtmf",
  "span_ids": ["s_0"],
  "status": "PASS"
}
```

## Missing evidence refuses, never guesses

Run the same test with no trace and the assertion goes INCONCLUSIVE, the
required success condition fails, and the exit code is 1:

```console
$ hotato test run dtmf.test.yaml --agent my-agent-v1 --out ./conv-artifact
hotato test run: dtmf-demo (agent my-agent-v1) -- exit_code=1
success: FAIL  (required: all_deterministic_assertions_pass)
```

```json
{
  "id": "dtmf-reached-billing-menu",
  "kind": "dtmf",
  "status": "INCONCLUSIVE",
  "reason": "no trace was provided; dtmf reads voice_trace.v1 spans",
  "public_reason": "Required trace evidence was missing."
}
```

This is the honesty contract for signalling evidence: the check asserts
exactly what an observer logged, each evidence row names its authority, and a
claim with no evidence behind it is refused. A failing dtmf condition
packages into a share-safe [failure record](../EVIDENCE-PACK.md) as
trace-span evidence, so a reviewer re-derives the same verdict from the same
trace.

## Make it a far-end check

The strength of the verdict equals the strength of the observer. Digits
logged by the caller side verify what was sent; digits logged by the far
end's own decoder (an IVR event, a telephony provider webhook, your backend's
DTMF handler) verify what was received. Emit that far-end event as the `dtmf`
span in your trace ([`docs/TRACE.md`](../TRACE.md), [`docs/OTEL.md`](../OTEL.md))
and the same assertion above gates on far-end receipt in CI.
