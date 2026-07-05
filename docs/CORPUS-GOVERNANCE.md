# Corpus governance

How this project assembles, labels, and reports on a small corpus of **real**
labeled voice-agent calls that augments the bundled synthetic fixtures.

This document is normative. If you want to contribute real audio, follow it.
If you want to understand what the numbers in a report do and do not mean, read
the "Validity metrics" section at the end.

---

## Why real recordings matter

The synthetic fixtures that ship in `src/hotato/data/` are a
deliberately honest floor. They are rendered from known segment timings, so the
ground truth is exact and the eval is runnable by anyone, offline, in seconds.
That makes them perfect for regression tests and for demonstrating *that the
scorer behaves as specified*.

What they cannot do is prove production validity. Synthetic audio has clean
onsets, controlled overlap, no room acoustics, no codec artifacts beyond what we
inject, no genuine human turn-taking timing. A scorer that looks flawless on
synthetic fixtures can still mismeasure a real 8 kHz call with echo bleed,
cross-talk, and a caller who trails off mid-word.

A small, carefully labeled corpus of real calls closes that gap. It is the
difference between "the tool does what the spec says" and "the tool measures
what actually happens on a phone line." We keep the corpus **small and
high-integrity** on purpose: every clip is consented, de-identified, and
human-labeled. Ten trustworthy calls beat ten thousand scraped ones.

---

## Scope and boundaries

- The corpus measures **audio timing of turn-taking only** — floor-yielding,
  talk-over, backchannel handling. It is not a transcription set, not a speaker
  identification set, not a diarization set, not an emotion set.
- Labels are about **events in time** (did the agent yield; when did the caller
  start), never about *who* a speaker is or *what* they feel.
- Everything here is redistributed under the project's MIT license. If a
  recording cannot be released under MIT with consent, it does not enter the
  corpus.

---

## Consent / release template

Every party audible on a real recording must agree, in a form you can produce on
request, to its inclusion. The following paragraph is intentionally short and
reusable. A contributor can send it to a caller and/or agent-operator and keep
the signed or written affirmative reply on file. Fill the brackets before use.

> **Recording release — hotato open test corpus**
>
> I, [name or role], took part in the audio recording made on [date] and
> described as [short description]. I understand this recording will be used as a
> test fixture in *hotato*, an open-source project, and grant a
> perpetual, worldwide, royalty-free right to store, redistribute, and publish
> the recording and its derived timing labels as part of that project's
> MIT-licensed corpus. I confirm that the recording contains no confidential,
> personal, health, or account information, or that any such information has been
> removed or replaced before submission. I understand I can request removal of
> the recording from future releases at any time by contacting the maintainers.
>
> Signed / affirmed: [name] — [date] — [contact]

Notes:

- Get consent from **all** identifiable parties, both the human caller and
  whoever operates the agent side, when the agent side is a real person or a
  proprietary system whose owner must agree.
- Consent must be **affirmative and documented**. Silence, "they didn't object,"
  or a generic call-center "this call may be recorded" notice is not sufficient
  for redistribution under an open license.
- Honor removal requests promptly in the next release.

---

## PII policy

Real audio must be de-identified before it is committed. In order of preference:
**don't capture it, then remove it, then reject the clip.**

Strip or redact:

- **Names** of any real individual (caller, agent, third parties mentioned).
- **Phone numbers**, extensions, and any dialed digits (DTMF tones included).
- **Postal and email addresses.**
- **Account identifiers**: customer numbers, order numbers, card numbers, policy
  numbers, ticket IDs, anything that keys back to a real person or account.
- Any other free-text detail that, alone or combined, re-identifies someone.

Strongly preferred practice:

- Use **synthetic or role-played content**. Actors reading a script hit the same
  turn-taking dynamics (interruption, correction, backchannel) without exposing a
  real customer. This is the cleanest source of real *acoustics* with zero PII
  risk.
- **No PHI.** Do not submit any recording containing protected health
  information, regardless of consent. It is out of scope for this project.

Redaction guidance:

- Replace spoken PII with a short tone or silence of the **same duration** so the
  turn-taking timing is preserved. Do not cut samples out, or you will shift
  every onset after the edit and corrupt the labels.
- Redact the audio in **all channels** — a name muted on the mixed channel but
  audible on the caller channel is not redacted.
- Keep a private note of *what* was redacted and *when* (timestamps), but never
  commit the removed content.

---

## Data-handling policy

- **The tool never uploads anything.** `hotato` is offline by design:
  the scorer reads local audio and writes local reports. It makes no network
  calls to score a file and ships no telemetry. Contributing to the corpus is a
  deliberate, manual act of committing a de-identified, consented clip — nothing
  is exfiltrated automatically.
- **Storage.** Corpus audio lives in the repository alongside its scenario JSON,
  under the same MIT terms as the rest of the project. Do not stage real audio in
  third-party cloud buckets, shared drives, or chat tools during preparation;
  keep working copies local until they are cleared for release.
- **Raw originals stay out of the repo.** Only the final, redacted, consented
  clip is committed. The un-redacted original is the contributor's
  responsibility to secure or destroy per their own obligations, and must never
  be pushed.
- **Contributor attestation.** Each real-audio PR must include a short statement
  from the contributor affirming: (a) consent is on file for every party,
  (b) PII has been removed per the policy above, (c) the clip contains no PHI,
  and (d) the contributor has the right to release it under MIT. This attestation
  is part of the merge record.

---

## Validity metrics: what we publish, and what we refuse to

The corpus exists to quantify measurement quality **honestly**. That constrains
how results are reported.

We publish:

- **Per-signal measurement error, in milliseconds.** For each labeled call we
  compare the scorer's measured event times to the human labels and report the
  error distribution per signal:
  - `time_to_yield` — signed and absolute error in ms between measured and
    labeled floor-yield.
  - `talk_over` — error in ms on measured overlap duration.
  - onset agreement — error in ms between measured caller onset and
    `caller_onset_sec`.
  We report these as distributions (median, spread, worst case) so a reader sees
  the real shape of the error, not a single flattering statistic.
- **A `did_yield` confusion matrix.** Against the human `category`
  (`should_yield` / `should_not_yield`) label, we report the four-cell matrix:
  correct yields, correct holds, false yields (yielded on a backchannel), and
  missed yields (kept talking over a real interruption). The two error cells are
  the ones operators actually feel, so we surface them directly.

We deliberately do **not** publish:

- **Any aggregated "accuracy %."** Collapsing the matrix and the millisecond
  errors into a single percentage hides exactly the trade-offs that matter (a
  scorer can look "95% accurate" while failing the rare, costly missed-yield
  case). The confusion matrix and the ms error distributions are the report.
- **Speaker-identity, diarization, transcription, or emotion metrics.** The
  scorer does not produce these signals, so there is nothing honest to report
  about them. Energy over time is not identity or intent.

### Reading a corpus report

A healthy result looks like: tight millisecond error distributions on
`time_to_yield` and `talk_over`, onset agreement within a small ms band, and a
confusion matrix whose off-diagonal (false-yield / missed-yield) cells are small
and individually inspectable. A poor result looks like wide error spread or a
heavy off-diagonal — and it should point you toward a concrete fix in the call,
not toward a single number to optimize. Where a real call fails, the fix map
names candidate remedies; a learned engagement-control layer is one of
them, referenced only at that level. Audio-only is its weaker modality and a
fully local offline model remains a known gap; the corpus is here to measure
reality, not to sell around it.
