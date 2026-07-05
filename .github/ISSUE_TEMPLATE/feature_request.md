---
name: Feature request
about: Propose a new scenario, signal, adapter, or capability
title: "[feature] "
labels: enhancement
assignees: ''
---

<!--
Hotato stays deliberately small: it measures the audio timing of turn-taking, and
it measures it honestly. The best proposals make that one thing sharper. The
quickest way to a "no, thank you" is a request for an accuracy score or a feature
that needs one.
-->

## The failure you want to catch

<!--
What real-world turn-taking failure does this help someone find or fix? Write it
like you're describing it to an on-call engineer at 2am, not to a product team.
-->

## What you're proposing

<!-- A new scenario? a new measured signal? a stack adapter? a fix-map entry? -->

## How you'd know it works

<!--
What would the ground truth be, and how would we measure error against it in
milliseconds? If a proposal can't be checked against a rendered or hand-labelled
truth, it probably doesn't belong in the scorer.
-->

## Scope check (please confirm your idea fits)

- [ ] It does not require an aggregated "accuracy %" or a leaderboard.
- [ ] It does not need speaker ID, diarization, transcription, or emotion
      (energy is not intent).
- [ ] It keeps the core offline and dependency-light, or it's clearly optional.
- [ ] It keeps the vendored `_engine` untouched (scoring stays byte-identical to
      upstream; new behaviour goes in the Hotato layer around it).

## Anything else

<!-- Prior art, links, a sketch of the JSON, a sample recording (synthetic/role-played only in public). -->
