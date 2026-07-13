# The evidence pack

Hotato ships proof as reproducible commands, recorded audio, and deterministic,
byte-stable verdicts you can rerun on your own machine.

The standard every artifact in this pack is held to, and why it is ranked the
way it is, lives in [evidence/README.md](evidence/README.md). Read that first
if you are deciding how much to trust any single piece below.

## What's in the pack

| Artifact | What it shows | Where |
|---|---|---|
| Bundled demo | Two recorded calls a provider-default agent fails, scored offline in under a minute, one command | `hotato demo` · [START.md](START.md) |
| Recorded provider-default battery | 12 scripted calls against a live voice agent on its default settings; a missed interruption and a false stop fail in the same run | [`corpus/vapi-defaults/README.md`](../corpus/vapi-defaults/README.md) |
| Determinism check | Same recording, same numbers, every run, on every OS CI verifies (Linux proven; macOS/Windows now checked, pending a first green run) | [VALIDATION.md](VALIDATION.md) Job 1 |
| Not-scorable gallery | Eight input conditions and the exact verdict each produces, including three hard refusals | [GALLERY.md](GALLERY.md) · [TRUST-GALLERY.md](TRUST-GALLERY.md) |
| Trust contract | The input-condition table the gallery demonstrates | [TRUST-MATRIX.md](TRUST-MATRIX.md) |
| What is and is not validated | The three jobs Hotato is measured on, and the explicit does-not-claim list | [VALIDATION.md](VALIDATION.md) |
| Where Hotato fits next to a QA platform | Named-vendor routing guide | [COMPARE.md](COMPARE.md) |
| Case studies | Recorded-audio write-ups, each with a repro command and a mandatory "What Hotato did not prove" scope section | [`case-studies/`](case-studies/README.md) |
| Shareable cards | Self-contained SVGs of a candidate, a threshold-funnel finding, or a verify result | [CARDS.md](CARDS.md) · [`assets/cards/`](assets/cards/) |
| Launch-bar status | The checklist of what is done and what is still an open gap | [evidence/validation-plan.md](evidence/validation-plan.md) |

## How to check it yourself

Every artifact above is re-derivable from a command, not asserted from memory:

```bash
hotato demo --no-open --format text          # the two bundled failures
hotato run --stereo FILE.wav --expect yield  # diff two runs, get nothing
hotato trust --stereo FILE.wav               # the refusal contract, live
```

Every command above runs offline, on your machine -- reproduce it yourself
instead of taking the claim on faith.

## What counts as evidence here

Proof in this pack is reproducible: a command, a recording, a verdict you can
check yourself, ranked by the standard in
[evidence/README.md](evidence/README.md).
