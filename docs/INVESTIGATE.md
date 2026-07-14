# `hotato investigate`: one recording in, ranked candidate moments out

Point it at one recording -- a local dual-channel WAV, or a live pull by
call id -- and it authenticates the source, runs the input-health gate,
scans for candidate turn-taking moments, and prints the exact command to
turn each one into a signed, CI-ready contract.

```bash
hotato investigate label <candidate_ref> --expect yield|hold
```

The one decision that stays with you: which candidate is a bug, and whether
the agent should have *yielded* (stopped) or *held* (kept talking through a
backchannel, noise, or ack). `"mhm"` and `"stop"` can sound identical by
energy, so the label is yours.

## Scan one recording

```bash
hotato investigate call.wav                        # a local WAV
hotato investigate --stack vapi --call-id abc123    # or pull live
```

Give it a local `SOURCE` path, or `--stack`/`--call-id`, never both. Stacks:
`vapi`, `twilio`, `retell`, `bland`, `elevenlabs`, `synthflow`, `millis`,
`cartesia`. LiveKit and Pipecat capture in your own infra: run `hotato setup
--stack <name>` and pass the resulting WAV as `SOURCE`. `--allow-mono`
accepts a mono/mixed recording, but a summed channel can't score separated
candidates.

Flags: `--min-gap` (min. gap in seconds to surface, default `2.0`), `--top`
(candidates shown, default `10`, `0` = all), `--caller-channel` /
`--agent-channel`, `--state PATH` (default `.hotato/investigate-state.json`).

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

## Capture origin: tracked every run

Every run tags how the audio arrived: `frozen_regression` (a pinned hotato
fixture, replayed exactly), `provider_pulled` (fetched live from the
stack's own recording API -- for the stronger, signed claim see
[RECAPTURE.md](RECAPTURE.md)), or `operator_asserted_local` (a local WAV,
taken at face value).

## The verdict gate

Trust runs in contract mode, the same crosstalk/leakage bar `hotato contract
create` checks. Two outcomes:

- **NOT SCORABLE** (mono, identical channels, a silent required channel): no
  candidates scanned, the reason named, exit `2`. Fix the input and re-run.
- **Verdict path REFUSED** (a suspected channel swap or crosstalk/leakage):
  candidates still surface; a labeled event carries a verdict once you fix
  the crosstalk or confirm the mapping with `--confirm-channels`. See
  [TRUST.md](TRUST.md).

Scan runs whenever the input is scorable, so candidates appear even when the
verdict path is refused.

## State and candidate refs

State persists to `.hotato/investigate-state.json` (run-numbered, atomically
written) in the same shape `hotato analyze` / `hotato sweep` use, so it's a
valid `FILE#N` candidate ref for `hotato fixture promote` and `hotato
contract create --from-candidate`. `#1` is the top-ranked candidate.

## Label a candidate into a contract

Your `--expect` goes straight to `hotato contract create --from-candidate`,
which mints a signed label-record bound to the exact decoded audio when a
signing key is configured. Without one, `label_authority` floors at
`asserted`.

```bash
hotato investigate label .hotato/investigate-state.json#1 \
    --expect yield \
    --max-talk-over 0.6 --max-time-to-yield 1.0 \
    --reviewer "$USER" --out contracts
```

Writes `contracts/<id>.hotato/`. The id defaults to a slug from the source,
onset, and label; `--id` names it, `--force` overwrites. `--pre`/`--post`
(defaults `2.0`/`6.0`) set the clip window before/after the onset;
`--no-clip` keeps the full recording. If the verdict path was refused, add
`--confirm-channels` to carry it through `contract verify`. Source
basenames are redacted by default; `--include-identifiers` keeps them.

A contract, not a bare fixture, is the point: it carries the trust block,
the CI policy, and the exact `hotato contract verify` command -- the
handoff into [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md) and
[CONTRACTS.md](CONTRACTS.md).

Scanning, the gate, and labeling all run offline; audio stays on your
machine unless you pull it from your own stack
([THREAT-MODEL.md](THREAT-MODEL.md)).

## Exit codes

`hotato investigate`:

- **`0`** -- candidate-eligible: trust + scan ran, candidates (if any)
  persisted. A verdict may still be refused; see `verdict_status`.
- **`2`** -- usage error (no `SOURCE`/`--stack`+`--call-id`, both given, a
  bad flag, a missing credential, an unreadable state file), or NOT
  SCORABLE.

`hotato investigate label`:

- **`0`** -- a signed, CI-ready contract was written.
- **`2`** -- usage error (a bad `--expect`, a bad candidate ref, an
  unresolved source, an existing contract without `--force`), or not
  scorable.

## See also

- [BAD-CALL-TO-CI.md](BAD-CALL-TO-CI.md) -- the bad-call to CI-gate flow this feeds.
- [CONTRACTS.md](CONTRACTS.md) -- the failure-contract bundle.
- [TRUST.md](TRUST.md) -- the input-health scorability gate.
- [RECAPTURE.md](RECAPTURE.md) -- the stronger, signed fresh-recapture claim.
- [CONVERSATION-TEST.md](CONVERSATION-TEST.md), [SUITE-RUN.md](SUITE-RUN.md) -- the conversation-QA path.
