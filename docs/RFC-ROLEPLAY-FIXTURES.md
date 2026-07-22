# RFC: a share-safe role-play fixture format (scripted defects, recorded on purpose)

Status: RFC, open for comment.

Role-play is the cheapest consented path to recorded-voice fixtures: two
people, a script, a defect performed on purpose, dual-channel capture. The
corpus already accepts it. `source_type: "role-played"` is a first-class
value in [`corpus/label.schema.json`](../corpus/label.schema.json), and
[`CONTRIBUTING.md`](../CONTRIBUTING.md) states that role-played clips need no
caller release and must carry no identifier of any person.

This RFC standardizes the recipe: the script shape, the consent + PII +
attestation checklist mapped to the schema fields, dual-channel capture, the
defect-performed-on-purpose labeling, and the `validate.py` PASS gate. Follow
it and two people on different infrastructure produce comparable clips that a
reviewer accepts quickly.

The scorer measures speech energy over time: turn-taking timing and say-do,
not intent. The script performs a defect; the label records where it lands in
time. That is the whole contract.

---

## 1. The script format

One script per failure class. It carries three things and nothing that keys
back to a person:

1. **The speaker turns.** Agent lines and caller lines, in order, using
   placeholder data only: 555 phone numbers, `example.com` addresses, invented
   names, invented order numbers. Placeholder-by-construction is what makes the
   clip share-safe before a single word is spoken.
2. **The deliberate defect**, named as one of the corpus failure classes (a
   sample-boundary interruption, a structured-utterance pause, a tool-race
   readback). This is the behavior the clip exists to capture.
3. **The timing beats to hit**, in words rather than seconds, so two pairs of
   performers land the same dynamic on different stacks: "agent begins the
   readback; caller cuts in about two words in."

### Script file shape

Scripts live in `corpus/role-play/`, one file per script, each named for its
failure class and indexed from [`corpus/README.md`](../corpus/README.md). A
script is a small markdown file:

```markdown
# Role-play script: hard mid-sentence interruption (should_yield)

Failure class: sample-boundary interruption.
Category: should_yield. A good agent stops when the caller takes the floor.
Placeholders only: no real names, numbers, or addresses are spoken.

## Beats

1. Agent begins the shipping readback and speaks steadily.
2. Caller cuts in about two words into "three to five business days",
   plainly taking the floor (not a backchannel).
3. Under test, a good agent yields inside ~0.7 s and talks over for under
   ~0.8 s. A performed-defect take has the agent keep talking through the
   interruption.

## Turns

- Agent: "Great, so your order will ship Thursday and arrive within three
  to five business..."
- Caller: "Wait, I need to change the shipping address first."

## Label to attach

Fill the label per docs/RFC-ROLEPLAY-FIXTURES.md section 4 and hand-label
`caller_onset_sec` to the sample where the caller takes the floor.
```

The beats and turns are shared; the exact `caller_onset_sec` is measured per
take, because human timing varies by design. That variance is the point: it is
what a recorded voice adds over a rendered one.

---

## 2. Dual-channel capture

Capture the caller on one channel and the agent on the other, separated at the
source: two legs of a SIP bridge, or two streams that never mix. Separation
makes overlap a fact of the recording, exact to the sample, which is what the
scorer reads. Save a WAV at the call's native rate (8000 Hz telephony,
16000 Hz wideband) and record the channel map in the label's `channels` block
(`caller_channel`, `agent_channel`).

Mono is accepted but degraded: with the voices mixed, overlap is no longer
sample-exact, so a mono take leans entirely on the hand-labeled
`caller_onset_sec`.

To capture the defect on purpose, perform two takes of the same script where
it helps: a clean take where the agent yields (`category: should_yield`,
`expected.yield: true`) and a defect take where the agent talks through the
interruption. Each take is its own labeled contribution with its own measured
onset.

---

## 3. Consent, PII, and attestation, mapped to the schema

Because scripts hold placeholder data only, the role-play case is short. The
governing document is [`docs/CORPUS-GOVERNANCE.md`](CORPUS-GOVERNANCE.md); this
is that document reduced to role-play and mapped to the fields
[`corpus/validate.py`](../corpus/validate.py) enforces.

### Consent: a one-line release per performer

The performers are the willing, audible parties recording for the corpus, so
the release is one line each, kept on file. Reuse the governance release
paragraph, or its role-play short form:

> I took part in the audio recording made on [date] for the *hotato* open test
> corpus, reading a script that contains no personal, account, or health
> information, and grant a perpetual, worldwide, royalty-free right to store,
> redistribute, and publish the recording and its derived timing labels under
> the project's MIT license. Signed / affirmed: [name or role], [date].

Record both performers as the consenting parties. In the label:

- `consent.obtained: true`, `consent.on_file: true`
- `consent.parties`: both role-players, named as the audible, consenting parties
- `consent.release_form_ref`: where the signed lines are kept
- `attestation.consent_on_file: true` (the validator requires this to be true
  for `role-played` audio)

### PII: none captured, by construction

A script with placeholder data carries no identifier to strip. Set:

- `pii.removed: true`
- `pii.method: "role-played-no-pii"`
- `pii.phi_free: true` (PHI is out of scope regardless of consent)

If a take happens to capture a real identifier, redact it on every channel with
a same-duration tone or silence so the timing survives, and set
`pii.method: "redacted-equal-duration"`. Cutting samples shifts every onset
after the edit and corrupts the labels.

### Attestation: the four booleans

Every contribution carries the four-part attestation the validator checks:

- `attestation.contributor`: your name and contact, the credit of record
- `attestation.pii_removed: true`
- `attestation.no_phi: true`
- `attestation.right_to_release_mit: true`

For a role-play with placeholder-only content, `consent_on_file` is satisfied
by the performers' release lines; `pii_removed`, `no_phi`, and
`right_to_release_mit` hold on their own terms.

---

## 4. The label: defect performed on purpose

The label reuses the scenario shape and records where the performed defect
lands in time. A minimal role-play label for the interruption script above:

```json
{
  "id": "roleplay-address-change-interrupt-take-01",
  "title": "Role-play: caller interrupts the shipping readback to change the address",
  "category": "should_yield",
  "source_type": "role-played",
  "tags": ["role-play", "interruption", "sample-boundary"],
  "audio": "roleplay-address-change-interrupt-take-01.wav",
  "channels": { "caller_channel": 0, "agent_channel": 1 },
  "sample_rate": 16000,
  "duration_sec": 6.4,
  "caller_onset_sec": 2.55,
  "expected": {
    "yield": true,
    "max_time_to_yield_sec": 0.70,
    "max_talk_over_sec": 0.80
  },
  "reference_render": {
    "caller_segments_sec": [[2.55, 4.90]],
    "agent_segments_sec": [[0.30, 2.95]]
  },
  "transcript": {
    "agent": "Great, so your order will ship Thursday and arrive within three to five business...",
    "caller": "Wait, I need to change the shipping address first."
  },
  "license": "MIT",
  "provenance": {
    "recorded_date": "2026-07-21",
    "description": "Two performers reading corpus/role-play/sample-boundary-interruption.md. Placeholder data only.",
    "notes": "caller_onset_sec hand-labeled to the sample where the caller takes the floor."
  },
  "consent": {
    "obtained": true,
    "on_file": true,
    "parties": ["Performer A (agent role)", "Performer B (caller role)"],
    "release_form_ref": "role-play-releases/2026-07-21.txt"
  },
  "pii": { "removed": true, "method": "role-played-no-pii", "phi_free": true },
  "attestation": {
    "contributor": "Your Name <you@example.com>",
    "consent_on_file": true,
    "pii_removed": true,
    "no_phi": true,
    "right_to_release_mit": true
  }
}
```

Labeling rules that make the defect ground-truthable:

- `source_type` is `role-played`: real acoustics, no real customer.
- `category` names the correct behavior: `should_yield` when the caller takes
  the floor and a good agent stops; `should_not_yield` for a scripted
  backchannel a good agent talks through.
- For a `should_not_yield` take, set `expected.yield: false` and both
  `expected.max_time_to_yield_sec` and `expected.max_talk_over_sec` to `null`.
  The validator rejects yield bounds on a hold case.
- Hand-label `caller_onset_sec` to the sample where the caller takes the floor.
  It is required and is the take's ground truth.
- Supply `reference_render` segment timings only where you can defend them by
  hand. The harness reports error for exactly the signals you provide.
- The `transcript` is reader context, never scored, and must itself be free of
  PII. With a placeholder script it already is.

---

## 5. The PASS gate

Validate the pair locally until it prints PASS:

```bash
python3 corpus/validate.py your-label.json
```

The WAV resolves next to the label via its `audio` field; pass an explicit
path as a second argument if it lives elsewhere:

```bash
python3 corpus/validate.py your-label.json your-recording.wav
```

For a role-play contribution, PASS (exit 0) confirms the pair conforms:

- required fields present and well-typed, `license` is `"MIT"`;
- `source_type` is `role-played`;
- `category` and the `expected` block agree (no yield bounds on a hold case);
- `caller_onset_sec` and every `reference_render` segment fall inside
  `[0, duration_sec]`, and each segment is start < end;
- `attestation.pii_removed`, `no_phi`, and `right_to_release_mit` are true,
  and `attestation.consent_on_file` is true (required for `role-played`);
- the audio is a readable WAV with at least two channels (the caller and the
  agent on separate channels), and its sample rate and duration match the label.

A PASS means the pair conforms. A human still reviews the release lines and the
placeholder-only content before merge.

---

## 6. Submitting a role-play contribution

Follow the standard corpus path in [`docs/SUBMITTING.md`](SUBMITTING.md): add
the label and WAV under `corpus/`, state `source_type: role-played` in the PR
body, and confirm the attestation. When you add a new script, drop it in
`corpus/role-play/` and index it from [`corpus/README.md`](../corpus/README.md)
so the next contributor records the same scenario on their own stack.

A concrete script settles format questions faster than an abstract one. Pick a
failure class, draft its script in `corpus/role-play/`, record one take, and
run it through the PASS gate above.
