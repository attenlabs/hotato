# D2 gate report — interaction labels

## Deliverables
- src/hotato/schema/interaction-label.v1.json (kind hotato.interaction-label.v1)
- src/hotato/interaction_label.py (stdlib-only): build/validate/of/attach.
  build() takes ONLY explicit categorical values; no audio/pcm/timing/energy/
  transcript/verdict/model can reach it. Two supplied-data policies: non-speech
  forces addressed_to_agent=null + floor_intent=none; unknown authority degrades
  every judged field to unknown/null. of(carrier) reads an attached label or the
  all-unknown default (absent => unknown, backwards compatible). attach() is
  additive (one optional interaction_label key).
- tests/test_interaction_label.py (7): python-validator <-> JSON-Schema agree
  (jsonschema Draft2020-12 cross-check), both conditionals, bad-enum/extra/
  over-length refusals, absent-reads-unknown, additive round-trip, and the
  never-infer proof (build's signature admits no signal; the module imports no
  scorer/asr/judge to derive a label).

## Invariant honored
- Hotato NEVER infers addressee or turn intent. Every field is supplied by a
  human, trusted source, or fixture; existing unlabeled data reads as unknown.
- No speaker-ID / diarization claim (labels are supplied, not detected).
- Core stays zero runtime deps (validator is pure Python; jsonschema only in
  the test as a cross-check).

## For D3 (capability routing)
- D3 consumes speech_presence + addressed_to_agent + floor_intent + label_authority
  to decide utterance_addressee_gate eligibility: a paired battery needs a
  scorable missed interruption with addressed_to_agent=true AND a scorable false
  activation with addressed_to_agent=false, both from a named authority. All the
  ineligible classes (addressed backchannel, echo, non-speech, unknown addressee,
  one-sided, unscorable) are distinguishable from these fields + existing scorability.
