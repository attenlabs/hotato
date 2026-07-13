---
name: Feature request
about: Propose a new scenario, signal, check, adapter, or capability
title: "[feature] "
labels: enhancement
assignees: ''
---

<!--
hotato stays deliberately small and measures what it can defend. The best
proposals make one dimension sharper. The quickest way to a "no, thank you" is a
request for an accuracy score, a leaderboard, or a single blended number.
-->

## The failure you want to catch

<!--
What voice-agent failure does this help someone find or fix? Write it like you're
describing it to an on-call engineer at 2am: the refund that never fired, the
disclosure that got skipped, the caller who got talked over.
-->

## What you're proposing

<!-- A new scenario? a measured signal? a deterministic check? a stack adapter? a fix-map entry? -->

## How you'd know it works

<!--
What is the ground truth, and how would we measure against it (milliseconds for a
timing signal, pass/fail against a labeled expectation for a check)? A proposal
that can't be checked against a rendered or hand-labeled truth probably doesn't
belong in the scorer.
-->

## Scope check (please confirm your idea fits)

- [ ] It needs no aggregated "accuracy %", leaderboard, or blended score.
- [ ] It keeps the two lanes separate: deterministic checks stay apart from the
      model-judged rubric.
- [ ] It makes no speaker-identity, emotion, or intent claim (energy is not
      intent); any transcription or diarization stays an opt-in context layer that
      never feeds the reference score.
- [ ] It keeps the core offline and dependency-light, or it's clearly optional.
- [ ] It keeps the vendored `_engine` untouched (scoring stays byte-identical to
      upstream; new behavior goes in the hotato layer around it).

## Anything else

<!-- Prior art, links, a sketch of the JSON or YAML, a sample recording (synthetic or role-played only in public). -->
