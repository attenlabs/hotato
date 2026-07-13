---
name: Bug report
about: A number looks wrong, a run crashed, or hotato did something it shouldn't
title: "[bug] "
labels: bug
assignees: ''
---

<!--
Reproducibility is the point of the project, so the fastest fix comes from a
copy-paste repro. The exact command and the JSON envelope usually pin it down.
-->

## What happened

<!-- One or two sentences: what did hotato do that it shouldn't have? -->

## What you expected

<!-- What should it have done instead? -->

## Steps to reproduce

```bash
# the exact command(s) you ran
uvx hotato ...
```

- Input: <!-- bundled demo? your own recording or transcript? dual-channel or mono? sample rate? -->
- For a number that looks wrong, paste the relevant slice of the JSON envelope
  (`--format json`). For a timing result, the frame-level evidence pins it exactly:
  ```bash
  hotato run --stereo your.wav --onset <sec> --dump-frames frames.json --format json
  ```
  Every reported number is re-derivable from that dump, which usually shows where
  the disagreement is.

## Environment

- hotato version / install: <!-- `uvx hotato@latest`, a git SHA, a local checkout... -->
- Python version:
- OS:

## Anything else

<!--
Please do not attach recorded customer audio to a public issue. If a recording is
needed to reproduce, say so and we will sort out a private channel; a synthetic or
role-played clip that reproduces the bug is ideal.

Scope reminder: hotato reports timing measurements, per-dimension pass/fail counts,
and a yield/hold matrix. It has no accuracy percentage and no blended score, so
"the percentage is wrong" is not a bug it can have. It also makes no speaker-ID,
emotion, or intent claim.
-->
