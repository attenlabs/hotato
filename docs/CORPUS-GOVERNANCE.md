# Corpus governance

How this project assembles, labels, and reports on a small corpus of **real**
labeled voice-agent calls that augments the bundled synthetic fixtures.

This document is normative. To contribute real audio, follow it. To understand
what the numbers in a report mean, read the "Validity metrics" section at the end.

---

## Why real recordings matter

The synthetic fixtures in `src/hotato/data/` are a deliberately honest floor. They
are rendered from known segment timings, so the ground truth is exact and the eval
is runnable by anyone, offline, in seconds. That makes them ideal for regression
tests and for demonstrating that the scorer behaves as specified.

Production validity is a different question. Synthetic audio has clean onsets,
controlled overlap, and no room acoustics or codec artifacts beyond what we inject.
A scorer that looks flawless on synthetic fixtures still needs proving on a real
8 kHz call with echo bleed, cross-talk, and a caller who trails off mid-word.

A small, carefully labeled corpus of real calls closes that gap. It is the
difference between "the tool does what the spec says" and "the tool measures what
actually happens on a phone line." We keep the corpus small and high-integrity on
purpose: every clip is consented, de-identified, and human-labeled. Ten trustworthy
calls beat ten thousand scraped ones.

---

## Scope and boundaries

- The corpus measures the **audio timing of turn-taking**: whether the agent
  stopped talking when the caller interrupted (yield), how long both talked at
  once (talk-over), and whether the agent talked through a short acknowledgement
  like "mhm" (backchannel handling).
- Labels are about **events in time**: did the agent stop, and when did the
  caller start.
- Everything here is redistributed under the project's MIT license. A clip enters
  the corpus only if it can be released under MIT with consent.

---

## Consent / release template

Every party audible on a real recording must agree, in a form you can produce on
request, to its inclusion. The following paragraph is intentionally short and
reusable. Send it to a caller and/or agent-operator and keep the signed or written
affirmative reply on file. Fill the brackets before use.

> **Recording release, hotato open test corpus**
>
> I, [name or role], took part in the audio recording made on [date] and described
> as [short description]. I understand this recording will be used as a test
> fixture in *hotato*, an open-source project, and grant a perpetual, worldwide,
> royalty-free right to store, redistribute, and publish the recording and its
> derived timing labels as part of that project's MIT-licensed corpus. I confirm
> that the recording contains no confidential, personal, health, or account
> information, or that any such information has been removed or replaced before
> submission. I understand I can request removal of the recording from future
> releases at any time by contacting the maintainers.
>
> Signed / affirmed: [name], [date], [contact]

Notes:

- Get consent from **all** identifiable parties: the human caller, and whoever
  operates the agent side when that is a real person or a proprietary system whose
  owner must agree.
- Consent must be **affirmative and documented**. A generic call-center "this call
  may be recorded" notice is not sufficient for redistribution under an open
  license.
- Honor removal requests promptly in the next release.

---

## PII policy

Real audio must be de-identified before it is committed. In order of preference:
**do not capture it, then remove it, then reject the clip.**

Strip or redact:

- **Names** of any real individual (caller, agent, third parties mentioned).
- **Phone numbers**, extensions, and any dialed digits (DTMF tones included).
- **Postal and email addresses.**
- **Account identifiers**: customer numbers, order numbers, card numbers, policy
  numbers, ticket IDs, anything that keys back to a real person or account.
- Any other free-text detail that, alone or combined, re-identifies someone.

Strongly preferred practice:

- Use **synthetic or role-played content**. Actors reading a script hit the same
  turn-taking dynamics (interruption, correction, backchannel) with zero PII risk.
  This is the cleanest source of real acoustics.
- **No PHI.** Do not submit any recording containing protected health information,
  regardless of consent. It is out of scope for this project.

Redaction guidance:

- Replace spoken PII with a short tone or silence of the **same duration** so the
  turn-taking timing is preserved. Cutting samples out shifts every onset after the
  edit and corrupts the labels.
- Redact the audio in **all channels**. A name muted on the mixed channel but
  audible on the caller channel is still present.
- Keep a private note of what was redacted and when (timestamps). Never commit the
  removed content.

---

## Data-handling policy

- **The tool runs local-only.** `hotato` reads local audio and writes local
  reports, offline, with no telemetry. Contributing to the corpus is a deliberate,
  manual act of committing a de-identified, consented clip.
- **Storage.** Corpus audio lives in the repository alongside its scenario JSON,
  under the same MIT terms as the rest of the project. Keep working copies local
  until they are cleared for release; do not stage real audio in third-party cloud
  buckets, shared drives, or chat tools during preparation.
- **Raw originals stay out of the repo.** Only the final, redacted, consented clip
  is committed. The un-redacted original is the contributor's responsibility to
  secure or destroy per their own obligations.
- **Contributor attestation.** Each real-audio PR must include a short statement
  from the contributor affirming: (a) consent is on file for every party, (b) PII
  has been removed per the policy above, (c) the clip contains no PHI, and (d) the
  contributor has the right to release it under MIT. This attestation is part of
  the merge record.

---

## Validity metrics: what we publish

The corpus exists to quantify measurement quality honestly. That shapes how results
are reported.

We publish:

- **Per-signal measurement error, in milliseconds.** For each labeled call we
  compare the scorer's measured event times to the human labels and report the
  error distribution per signal:
  - `time_to_yield`: signed and absolute error in ms between measured and labeled
    floor-yield.
  - `talk_over`: error in ms on measured overlap duration.
  - onset agreement: error in ms between measured caller onset and
    `caller_onset_sec`.
  We report these as distributions (median, spread, worst case), so a reader sees
  the real shape of the error.
- **A `did_yield` confusion matrix.** Against the human `category` (`should_yield`
  / `should_not_yield`) label, we report the four-cell matrix: correct yields,
  correct holds, false yields (yielded on a backchannel), and missed yields (kept
  talking over a real interruption). The two error cells are the ones operators
  feel, so we surface them directly.

The distribution and the four-cell matrix are the report. A single percentage would
collapse the two failure modes into one number and hide the trade-off between them,
so we keep them separate. The scorer works on speech energy over time, so validity
is timing agreement and decision agreement.

### Reading a corpus report

A healthy result looks like: tight millisecond error distributions on
`time_to_yield` and `talk_over`, onset agreement within a small ms band, and a
confusion matrix whose off-diagonal (false-yield / missed-yield) cells are small
and individually inspectable. A poor result looks like wide error spread or a heavy
off-diagonal, and it should point you toward a concrete fix in the call.

Where a real call fails, the fix map names candidate remedies. A learned
engagement-control layer is one of them, referenced at that level. Audio-only is
its weaker modality and a fully local offline model remains a known gap. The corpus
is here to measure reality.
