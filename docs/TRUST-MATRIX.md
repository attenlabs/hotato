# Trust matrix: what Hotato does for each input condition

Before Hotato scores a recording, `hotato trust` checks the audio and
decides whether a score would mean anything -- catching a bad export before
it becomes a confident-looking but hollow verdict. This page is the exact
contract: input condition on the left, Hotato's behavior on the right. It
covers input health only; a turn-taking verdict is `scan`'s and `run`'s job.

Worked examples with CLI output for every row are in
[TRUST-GALLERY.md](TRUST-GALLERY.md). The four-tier evidence policy the
rows below implement is stated once in
[EVIDENCE-CONTRACT.md](EVIDENCE-CONTRACT.md).

## The contract

| Input condition | `trust` says | Exit | Downstream scoring |
|---|---|---|---|
| Clean dual-channel (stereo) | `eligible for scan` | 0 | **Full scoring.** `scan`, `run`, `compare`, `verify` all run normally. |
| Silent caller channel | `NOT SCORABLE: caller channel has no detected speech` | 2 | **Refused.** No caller to measure a yield against. |
| Silent agent channel | `NOT SCORABLE: agent channel has no detected speech` | 2 | **Refused.** No agent floor to interrupt. |
| Channel swap risk | `eligible for scan` **plus** `possible channel swap` warning | 0 | Scores -- but confirm the mapping first (`--caller-channel` / `--agent-channel`); a swap silently inverts every yield/hold. |
| High crosstalk / echo bleed | `eligible for scan` **plus** high `crosstalk: coherence` warning | 0 | Scores at **lower confidence.** `scan` tags the moment `echo_correlated_activity`; a "yield" there may be the agent hearing itself. |
| Mixed mono (single channel) | `NOT SCORABLE: single channel, caller and agent cannot be told apart` | 2 | **Refused by default.** Export dual-channel, or use one of the two opt-in escapes below. |
| Mono, opt-in `--allow-mono` | Accepted in **degraded mode**, results **indicative only** | 0 | Used by `capture` / `pull` / `sweep` for a mono-only stack. Talk-over can't be attributed, so no SLA gate fires; dual-channel stays the gold reference. |
| Mono, opt-in `--diarize` | Separability **tier**: `high` / `low` / `refuse` | 0 (high/low), 2 (refuse) | The separation front-end. `hotato run --mono call.wav --diarize` scores it; `low` stamps the verdict `indicative_only`. Dual-channel stays the gold reference. |
| Short backchannel ("mhm") overlap | Outside `trust`'s scope; `scan` lists it as a **candidate** | 0 | **Candidate only, human labels.** `trust`/`scan` report the timing; you always label `yield` vs `hold`. |
| Noisy / false-positive candidate | `trust` warns (clipping, crosstalk, hot capture); `scan` still lists the candidate | 0 | Surfaced **with** the warning, as a candidate for you to inspect and label `hold`. |

**Reading the two axes.** `trust` answers one question: is this audio good
enough to score? The exit code is the machine contract: `0` means eligible
for scan (maybe with warnings), `2` means not scorable, with the reason and
the next step. Warnings (swap, crosstalk, clipping, leading silence) inform
the read; only the three hard refusals -- mono, identical channels, a
silent required channel -- change scorability.

## Why refuse instead of guessing

A less careful tool would still print a number here. A mono file "scored"
by guessing which speaker is which produces a verdict indistinguishable
from a properly-scored one -- and it is worthless. A swapped-channel file
scored without the warning inverts caller and agent, so "the agent yielded"
becomes "the caller yielded" with no visible sign. Hotato treats these as
input defects, reports them, and stops: a green or red build always means
something.

## Two ways past a mono refusal

Mono is refused by default: one channel cannot separate the two parties.
Two opt-in escapes exist, both indicative only.

**1. `--allow-mono` (degraded mode).** On `capture`, `pull`, and `sweep`,
the `--allow-mono` flag (or `HOTATO_ALLOW_MONO=1`) accepts a mono-only
recording from a mono stack. Nothing is separated, so talk-over can't be
attributed to caller or agent: the result is explicitly indicative and no
SLA gate fires. Use it when a stack only exports a mono mix and a rough
signal still beats none.

**2. `--diarize` (separation front-end).** The opt-in `[diarize]` extra
(`pip install 'hotato[diarize]'`) runs a local diarizer and reports a
confidence tier before you score:

- **high**: confidently separable. `hotato run --mono call.wav --diarize`
  gives a diarized-mono verdict. Exit 0.
- **low**: separable but only indicative (voices close, overlap elevated).
  The verdict is stamped `indicative_only`; no SLA gate fires. Exit 0.
- **refuse**: not confidently two clean parties. Not scorable, exit 2;
  record dual-channel.

The dual-channel path stays the gold reference -- neither a degraded-mono
nor a diarized-mono verdict is promoted to equivalence with it. Full
front-end: [DIARIZE.md](DIARIZE.md).

## Composing the gate

Because `trust` exits `2` on any unscorable input, it drops straight into a
shell gate ahead of a scan or a scheduled sweep:

```bash
hotato trust --stereo call.wav && hotato scan --stereo call.wav
```

For agents, `hotato trust --stereo call.wav --format json` emits one
machine-parseable report. Branch on `scorable`; on a defect, read
`not_scorable_reason` and `next_step`. Details: [TRUST.md](TRUST.md).
