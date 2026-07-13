# `hotato investigate`: one recording in, ranked candidate moments out

`hotato investigate` takes one recording -- a local dual-channel WAV, or a
live pull from a connected stack by call id -- and does four things, in order:
authenticates where the audio came from, runs the input-health / K6
verdict-eligibility gate, scans it for candidate turn-taking moments, and
prints the exact next command that turns each candidate into a signed,
CI-ready contract.

It is discovery and guidance, nothing more. `hotato investigate` never infers
intent and never mints a label or a verdict. The one decision that matters --
which candidate is a bug, and whether the agent should have *yielded* or
*held* -- stays with you, and you make it by running the command this tool
prints:

```bash
hotato investigate label <candidate_ref> --expect yield|hold
```

Every step reuses a shipped primitive: audio-in is the same per-stack fetch
`hotato pull` uses; the gate is `hotato trust` in contract mode; discovery is
`hotato scan`; the label step wraps `hotato contract create --from-candidate`.

## The label comes from you

Hotato does not infer intent. You label the expected behavior for the event:
`yield` means the agent should stop for the caller; `hold` means the agent
should keep speaking through a backchannel, noise, or acknowledgement. Hotato
then measures whether the timing matched that label. `"mhm"` and `"stop"` can
carry identical speech energy; no timing measurement tells them apart, so the
label is yours.

## Scan one recording

```bash
# a local dual-channel WAV you already have
hotato investigate call.wav

# or pull it live from a connected stack by call id
hotato investigate --stack vapi --call-id abc123
```

Give it either a local `SOURCE` path or `--stack STACK --call-id ID`, never
both. Connectable stacks: `vapi`, `twilio`, `retell`, `bland`, `elevenlabs`,
`synthflow`, `millis`, `cartesia`. LiveKit and Pipecat capture in your own
infrastructure -- run `hotato setup --stack <name>` and pass the resulting WAV
as `SOURCE`. `--allow-mono` permits a mono/mixed stack recording, but a summed
channel is degraded and is not candidate-eligible for separated scoring.

Useful flags: `--min-gap` (minimum response gap in seconds to surface, default
`2.0`), `--top` (how many top candidates to show and print label commands for,
default `10`; `0` shows all), `--caller-channel` / `--agent-channel`, and
`--state PATH` (default `.hotato/investigate-state.json`).

### What it reports

```
hotato investigate [run 1]: call.wav
  capture origin: operator-asserted local file (call.wav)
  input health: <trust recommendation>
  verdict path: eligible (a labeled event here can carry a yield/hold verdict)
  N candidate moment(s) (showing N):
    [1] t=42.18s <kind>  <durations>
        label: hotato investigate label .hotato/investigate-state.json#1 --expect yield|hold
  state remembered at: .hotato/investigate-state.json
```

The onset above is illustrative. Candidates are timing facts only, never a
verdict.

## Capture origin: authenticated, not asserted for you

Every run records where the audio came from, one of three kinds:

- **`frozen_regression`** -- a previously-created hotato fixture clip (a sibling
  scenario file names this exact audio): a pinned regression, not a live call.
- **`provider_pulled`** -- fetched just now from the stack's own recording API
  for a named call id. Stronger than an arbitrary file, but this is **not** a
  signed capture receipt -- never read it as machine-verified or
  runner-attested. For that stronger claim, see [RECAPTURE.md](RECAPTURE.md).
- **`operator_asserted_local`** -- you handed hotato a local WAV path; nothing
  here independently verifies it.

## The K6 verdict gate

Trust runs in contract mode -- the same stricter crosstalk/leakage bar
`hotato contract create` itself checks, since this command exists to produce a
contract. Two outcomes matter:

- **NOT SCORABLE** (mono, identical channels, a silent required channel): no
  candidates are scanned; the report names the reason, and the command exits
  `2`. Fix the input and re-run.
- **Verdict path REFUSED** (a suspected channel swap or crosstalk/leakage): the
  candidates below are still shown as timing facts, but no labeled event here
  can carry a yield/hold verdict until you confirm the mapping with
  `--confirm-channels` or fix the crosstalk. See [TRUST.md](TRUST.md).

Note the two gates are different widths: `scan` runs whenever the input is
scorable, so candidates appear even when the verdict path is refused.

## State and candidate refs

State persists to `.hotato/investigate-state.json` (run-numbered, atomically
written, with a history log). It is written in the same shape `hotato analyze`
/ `hotato sweep` produce, so it is itself a valid `FILE#N` candidate ref:
`hotato fixture promote` and `hotato contract create --from-candidate` read it
directly. `#1` is the top-ranked candidate, `#2` the next, and so on.

## Label a candidate into a contract

`hotato investigate label` is the label step. Your `--expect` goes straight to
`hotato contract create --from-candidate`, which mints a signed label-record
bound to the exact decoded audio when a signing key is configured. Without a
signing key it never crashes and never fabricates a human attestation: the
contract honestly floors its `label_authority` at `asserted`.

```bash
hotato investigate label .hotato/investigate-state.json#1 \
    --expect yield \
    --max-talk-over 0.6 --max-time-to-yield 1.0 \
    --reviewer "$USER" --out contracts
```

This writes `contracts/<id>.hotato/`. The id defaults to a slug derived from
the source, onset, and label; pass `--id` to name it, `--force` to overwrite.
Clipping keeps `--pre` seconds before the onset (default `2.0`) and `--post`
after (default `6.0`); `--no-clip` keeps the full recording. If the verdict
path was refused, add `--confirm-channels`, or `contract verify` refuses the
contract's verdict. Source basenames are redacted from the bundle unless you
pass `--include-identifiers`.

Building a contract (not a bare fixture) is deliberate: it carries the K6 trust
block, the CI policy, and the exact `hotato contract verify` command. That is
where investigate hands off to [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md) and
[CONTRACTS.md](CONTRACTS.md).

Scanning, the trust gate, and labeling run offline; audio stays on your machine
unless you explicitly pull it from your own stack ([THREAT-MODEL.md](THREAT-MODEL.md)).

## Exit codes

`hotato investigate`:

| code | meaning |
| --- | --- |
| `0` | the recording is candidate-eligible: trust + scan ran and candidates (if any) were persisted; a yield/hold VERDICT may still be refused (K6) -- see `verdict_status` |
| `2` | usage error (neither `SOURCE` nor `--stack/--call-id`, both given, a bad channel/`--min-gap` flag, a missing credential, or an unreadable state file), or the recording is NOT SCORABLE at all |

`hotato investigate label`:

| code | meaning |
| --- | --- |
| `0` | a signed, CI-ready contract was written from this candidate |
| `2` | usage error (a bad `--expect`, a bad candidate ref, an unresolved source recording, or an existing contract without `--force`), or the candidate turned out not scorable |

## See also

- [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md) -- the bad-call to CI-gate flow this feeds.
- [CONTRACTS.md](CONTRACTS.md) -- the failure-contract bundle.
- [TRUST.md](TRUST.md) -- the input-health / K6 scorability gate.
- [RECAPTURE.md](RECAPTURE.md) -- the stronger, signed fresh-recapture claim.
- [CONVERSATION-TEST.md](CONVERSATION-TEST.md), [SUITE-RUN.md](SUITE-RUN.md) -- the conversation-QA path.
