---
name: Bug report
about: A number looks wrong, a run crashed, or the tool did something it shouldn't
title: "[bug] "
labels: bug
assignees: ''
---

<!--
Hotato measures one narrow thing: the audio timing of turn-taking. The more of
the recording and the exact numbers you give us, the faster this gets fixed.
Reproducibility is the whole point of the project, so help us reproduce it.
-->

## What happened

<!-- One or two sentences. What did the tool do that it shouldn't have? -->

## What you expected

<!-- What should it have done instead? -->

## Steps to reproduce

```bash
# the exact command(s) you ran
uvx hotato run ...
```

- Input: <!-- bundled suite? your own recording? synthetic or real? mono or two-channel? sample rate? -->
- If it's a specific number that looks wrong, paste the relevant slice of the
  JSON envelope (`--format json`), and ideally the frame-level evidence:
  ```bash
  hotato run --stereo your.wav --onset <sec> --dump-frames frames.json --format json
  ```
  Every reported number is re-derivable from that dump; it usually tells us
  exactly where the disagreement is.

## Environment

- Hotato version / install: <!-- `uvx hotato@latest`, a git SHA, a local checkout... -->
- Python version:
- OS:

## Anything else

<!--
Please do NOT attach real customer audio to a public issue. If a real recording
is needed to reproduce, say so and we'll sort out a private channel. Synthetic or
role-played clips that reproduce the bug are ideal.

Reminder on scope: Hotato reports timing measurements and a yield/hold matrix. It
does not do speaker ID, diarization, transcription, or emotion, and it has no
accuracy percentage. "The percentage is wrong" is not a bug we can have, because
there is no percentage.
-->
