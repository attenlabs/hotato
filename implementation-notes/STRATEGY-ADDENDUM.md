# Strategy addendum: conformance-for-PRs layer (operator-shared GPT convo, 2026-07-13)

Reconciled against the verified delta pack. The delta stays the execution
authority; this addendum records what the newer conversation ADDS and how it
maps in, without widening the frozen scope.

## Adopted into current phases (refinements, not new scope)
- CATEGORY LINE (use in P10/discovery copy when behavior ships): "Open
  conversation conformance for voice-agent PRs." Activation line: "Your coding
  agent changed the voice agent. Hotato proves whether the call still works."
- D1 Action PR summary: adopt the five-lane PR summary format from the convo
  (Outcome/Policy/Conversation/Speech/Reliability lines + Reproduce command +
  acceptance-check IDs). Machine JSON stays the primary artifact.
- D1 Failure Record: include exact reproduction command + acceptance
  conditions + before/after relationship (already in the reference kit; keep).
- D3 naming: DELTA SCHEMA WINS. required_capability enum stays
  utterance_addressee_gate | turn_intent_discriminator | engagement_control
  (the convo's pre_stt_addressee_gate is the same concept, superseded name).
  fix_class: engagement-control; acceptance_tests refs as in schema.
- D5 Atlas: adopt the convo's page inventory as candidate slugs
  (refund-claimed-without-state-change, disclosure-skipped-after-interruption,
  addressed-interruption-missed, side-speech-triggered-agent), gated on
  cleared fixtures + labels per delta rules.

## Queued POST-D6 (needs launch-critical gates + operator go; do NOT start)
- Portable Agent Skill (.agents/skills/hotato-voice-regression/SKILL.md) +
  `npx skills add attenlabs/hotato` path.
- GitHub Agentic Workflow recipe (beyond the plain Action).
- Executable conformance suite packs + signed compatibility receipts
  (OpenTelemetry-pattern; external integrations live in their owners' repos).
- Upstream distribution PRs (LiveKit/Pipecat/Vapi/Retell starters) — external
  outreach, operator-gated per fleet law.
- Contribution model (bounded artifacts, pack CODEOWNERS, expiring status).

## Kill list (binding; matches delta freeze)
Marketplace, official adapters for every provider, hosted monitoring, generic
AI-eval content, Discord ops, gamified contributors, customer-audio corpus
default, central CI running arbitrary contributor code, generic leaderboard,
simulation feature race, MCP-as-headline.

## Validation gate (the metric that counts)
External repositories that emit a failing record and later rerun the same
acceptance ID successfully. Three-week test after launch-critical ships:
(1) schema+skill+action+tasks published, (2) one external coding agent
resolves a failing PR from the record alone, (3) one independent repo
emits/consumes the format, (4) one upstream starter lands a workflow.
If proofs 2-3 fail: stop expanding registry/governance.
