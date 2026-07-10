# Evidence, not adoption claims

Hotato is early. This directory is deliberately not a wall of logos or a star
count. Stars and installs measure attention, not whether the tool measures what
it says. Judge Hotato on **artifacts you can run and inspect**, and hold it to
that bar even when the adoption numbers are small. Zero stars is fine; a hollow
verdict is not.

## What counts as evidence here

Ranked by how hard it is to fake:

1. **The bundled demo.** `hotato demo` scores two recorded calls a
   provider-default agent fails, offline, in under a minute. You see the FAILs,
   the timelines, and the fix cards on your own machine. No account, no upload.
2. **The real battery.** `corpus/vapi-defaults/` is 12 scripted calls against a
   live voice agent on its provider's default settings, where a missed
   interruption and a false stop on a backchannel fail in the same run, so
   `diagnose` refuses to name one threshold. The refusal is the evidence.
3. **Deterministic outputs.** The same recording produces the same timing numbers
   on every run, on every OS CI verifies (Linux proven; macOS and Windows now
   checked in CI too, pending a first green run -- see
   [VALIDATION.md](../VALIDATION.md), Job 1). You can diff two runs and get
   nothing.
4. **Not-scorable examples.** The [trust gallery](../TRUST-GALLERY.md) shows
   Hotato refusing to score mono, silent-channel, and swapped recordings, and
   flagging echo-driven false positives. A tool that knows when to say "not
   scorable" is more trustworthy than one that always prints a number.
5. **A before/after.** A real failing moment, a named change, a re-recording that
   passes, plus an opposite-risk fixture that must still hold. This is the
   strongest artifact and the hardest to produce honestly, because it forbids
   inventing an "after" for a moment that already passed.
6. **A public PR.** A promoted fixture landing in a repository, in the open.

## Case studies

Each case study follows one template
([case-study-TEMPLATE.md](case-study-TEMPLATE.md)) and reports only what Hotato
measured on real audio, with a repro command and an explicit "what Hotato did
not prove" section. A study that has a real before but no honest after says so,
and stays a baseline rather than a fabricated win (see the sibling
`docs/case-studies/` for a worked example of exactly that restraint).

Launch target for this directory: **3 external or semi-external case studies,
5 consented fixtures, 1 before/after verify, 1 public PR.** The first case study
is The Table dogfood (before/after); external testers follow. The current gap
per study is tracked in [validation-plan.md](validation-plan.md).

## The standard

- Every number traces to a command's output. Nothing is reconstructed from memory.
- A recording that passes is not turned into a fix study.
- Consent and provenance are recorded for every real call. Audio that is not
  cleared for the public corpus stays private; the case study reports the
  measurements without shipping the audio.
- "What Hotato did not prove" is mandatory, not optional.
