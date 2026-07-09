# Trust matrix: what Hotato does for each input condition

Before Hotato scores a recording, `hotato trust` inspects the audio and decides
whether a score would be meaningful. The point is that a bad export must never
turn into a confident-looking but hollow verdict. This page is the exact
contract: input condition on the left, Hotato's behavior on the right. Nothing
here is a turn-taking verdict; `trust` reports input health only.

Worked examples with real CLI output for every row are in
[TRUST-GALLERY.md](TRUST-GALLERY.md).

## The contract

| Input condition | `trust` behavior | Exit | Scoring downstream |
|---|---|---|---|
| **Clean dual-channel (stereo)** | `safe to scan` | 0 | **Full scoring.** `scan`, `run`, `compare`, `verify` all run normally. |
| **Silent caller channel** | `NOT SCORABLE: caller channel has no detected speech` | 2 | **Refused.** There is no caller to measure a yield against. |
| **Silent agent channel** | `NOT SCORABLE: agent channel has no detected speech` | 2 | **Refused.** There is no agent floor to interrupt. |
| **Channel swap risk** | `safe to scan` **plus** `possible channel swap` **warning** | 0 | Scores, but confirm the mapping first (`--caller-channel` / `--agent-channel`). A swap silently inverts every yield/hold. |
| **High crosstalk / echo bleed** | `safe to scan` **plus** high `crosstalk: coherence` **warning** | 0 | Scores at **lower confidence.** `scan` tags the moment `echo_correlated_activity`; a "yield" there may be the agent hearing itself. |
| **Mixed mono (single channel)** | `NOT SCORABLE: single channel, caller and agent cannot be told apart` | 2 | **Refused by default.** Export dual-channel, or take one of the two opt-in escapes (below). |
| **Mono, opt-in `--allow-mono`** | accepted in **degraded mode**, results **indicative only** | 0 | On `capture` / `pull` / `sweep` for a mono-only stack. Talk-over cannot be attributed, so no SLA gate fires. Never equivalent to dual-channel. |
| **Mono, opt-in `--diarize`** | separability **tier**: `high` / `low` / `refuse` | 0 (high/low), 2 (refuse) | The separation front-end. `hotato run --mono call.wav --diarize` scores it; at `low` the verdict is stamped `indicative_only`. Never equivalent to dual-channel. |
| **Short backchannel ("mhm") overlap** | (not a trust concern) `scan` lists it as a **candidate** | 0 | **Candidate only, human labels.** `trust`/`scan` never decide `yield` vs `hold`; you label the intent. |
| **Noisy / false-positive candidate** | `trust` warns (clipping, crosstalk, hot capture); `scan` still lists the candidate | 0 | Surfaced **with** the warning. A candidate you inspect and label `hold`, not a bug Hotato asserts. |

**Reading the two axes.** `trust` answers one question: is this audio good enough
to score? The exit code is the machine contract, `0` = safe to scan (possibly
with warnings), `2` = not scorable (with the reason and the next step). Warnings
(swap, crosstalk, clipping, leading silence) do **not** by themselves make a
recording unscorable; the three hard refusals do (mono, identical channels, a
silent required channel).

## Why refuse instead of guessing

Every refusal above is a place where a lesser tool would still print a number.
A mono file "scored" by guessing which speaker is which produces a verdict that
looks identical to a real one and is worthless. A swapped-channel file scored
without the warning inverts caller and agent, so every "the agent yielded"
becomes "the caller yielded" with no visible sign. Hotato treats these as input
defects, reports them, and stops, so a green or red build always means something.

## Two ways past a mono refusal

Mono is refused by default because one channel cannot separate the two parties.
There are two opt-in escapes, and both produce results marked indicative only,
never equal to dual-channel.

**1. `--allow-mono` (degraded mode).** On `capture`, `pull`, and `sweep`, the
`--allow-mono` flag (or `HOTATO_ALLOW_MONO=1`) accepts a mono-only recording from
a mono stack. Nothing is separated: talk-over cannot be attributed to caller or
agent, so the result is explicitly indicative and no SLA gate fires. Use this when
a stack only exports a mono mix and you want a rough signal anyway.

**2. `--diarize` (separation front-end).** The opt-in `[diarize]` extra
(`pip install 'hotato[diarize]'`) runs a local diarizer and reports a confidence
tier before you score:

- **high**: confidently separable. `hotato run --mono call.wav --diarize` gives a
  diarized-mono verdict. Exit 0.
- **low**: separable but only indicative (voices close, overlap elevated). The
  verdict is stamped `indicative_only`; no SLA gate fires. Exit 0.
- **refuse**: not confidently two clean parties. Not scorable, exit 2; record
  dual-channel.

The dual-channel path stays the gold reference. Neither a degraded-mono nor a
diarized-mono verdict is promoted to equivalence with it. Full front-end:
[DIARIZE.md](DIARIZE.md).

## Composing the gate

Because `trust` exits `2` on any unscorable input, it drops straight into a shell
gate ahead of a scan or a scheduled sweep:

```bash
hotato trust --stereo call.wav && hotato scan --stereo call.wav
```

For agents, `hotato trust --stereo call.wav --format json` emits one
machine-parseable report; branch on `scorable`, and on a defect read
`not_scorable_reason` and `next_step`. Details: [TRUST.md](TRUST.md).
