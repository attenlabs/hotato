# The evidence pack

Hotato's proof is reproducible: commands, recorded audio, and
deterministic, byte-stable verdicts you rerun on your own machine.

The standard every artifact here is held to, and why it's ranked the way
it is, lives in [evidence/README.md](evidence/README.md) -- read that
first when deciding how much to trust any single piece below.

## What's in the pack

- **Bundled demo** -- two bundled calls an agent fails (a synthesized
  talk-over and a recorded provider-default call), scored offline in under a
  minute, one command. `hotato demo` ·
  [START.md](START.md)
- **Recorded provider-default battery** -- 12 scripted calls against a
  live voice agent on default settings; a missed interruption and a false
  stop fail in the same run.
  [`corpus/vapi-defaults/README.md`](../corpus/vapi-defaults/README.md)
- **Determinism check** -- the same recording produces the same numbers
  every run. CI runs it on Linux, macOS, and Windows; Linux is green
  today. [VALIDATION.md](VALIDATION.md) Job 1
- **Not-scorable gallery** -- eight input conditions, and the exact
  verdict each produces, including three hard refusals.
  [GALLERY.md](GALLERY.md) · [TRUST-GALLERY.md](TRUST-GALLERY.md)
- **Trust contract** -- the input-condition table the gallery
  demonstrates. [TRUST-MATRIX.md](TRUST-MATRIX.md)
- **Validation scope** -- the three jobs Hotato is measured on, and where
  that measurement stops. [VALIDATION.md](VALIDATION.md)
- **Where Hotato fits next to a QA platform** -- a named-vendor routing
  guide. [COMPARE.md](COMPARE.md)
- **Case studies** -- recorded-audio write-ups, each with a repro command
  and a scope section on exactly what that run proved.
  [`case-studies/`](case-studies/README.md)
- **PR cards** -- self-contained SVGs of a candidate, a
  threshold-funnel finding, or a verify result. [CARDS.md](CARDS.md) ·
  [`assets/cards/`](assets/cards/)
- **Launch-bar status** -- the checklist scoring where each launch item
  stands. [evidence/validation-plan.md](evidence/validation-plan.md)

## Check it yourself

Every artifact above is re-derivable from a command:

```bash
hotato demo --no-open --format text          # the two bundled failures
hotato run --stereo FILE.wav --expect yield  # diff two runs, get nothing
hotato trust --stereo FILE.wav               # the refusal contract, live
```

Every command runs offline, on your machine -- reproduce it yourself
instead of taking the claim on faith.

## What counts as evidence here

A command, a recording, a verdict you check yourself -- ranked by the
standard in [evidence/README.md](evidence/README.md).
