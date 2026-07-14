# Corpus governance

The rulebook for contributing real labeled voice-agent calls to hotato's
corpus: why they matter, how to get consent, how to strip PII, and what gets
published from a labeled call. Read the "Validity metrics" section at the end
to understand what the numbers in a report mean.

---

## Why real recordings matter

The synthetic fixtures in `src/hotato/data/` are a deliberately conservative
floor. They render from known segment timings, so the ground truth is exact
and the eval runs offline, in seconds, for anyone. That makes them the
regression backbone and the proof that the scorer behaves as specified.

Production validity is a different question. Synthetic audio has clean
onsets, controlled overlap, and no room acoustics or codec artifacts beyond
what we inject. A scorer that looks flawless on synthetic fixtures still
needs proving on an 8 kHz call with echo bleed, cross-talk, and a caller who
trails off mid-word.

A small, carefully labeled corpus of real calls closes that gap. It is the
difference between "the tool does what the spec says" and "the tool measures
what happens on a phone line." The corpus stays small and high-integrity on
purpose: every clip is consented, de-identified, and human-labeled. Ten
trustworthy calls beat ten thousand scraped ones.

---

## Scope and boundaries

- The corpus measures the **audio timing of turn-taking**: whether the agent
  stopped talking when the caller interrupted (yield), how long both talked
  at once (talk-over), and whether the agent talked through a short
  acknowledgement like "mhm" (backchannel handling).
- Labels are about **events in time**: did the agent stop, and when did the
  caller start.
- Everything here is redistributed under the project's MIT license. A clip
  enters the corpus only if it can be released under MIT with consent.

---

## Consent / release template

Every party audible on a recording agrees, in a form you can produce on
request, to its inclusion. The paragraph below is short and reusable. Send it
to a caller and/or agent-operator and keep the signed or written affirmative
reply on file. Fill the brackets before use.

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

- Get consent from **all** identifiable parties: the human caller, and
  whoever operates the agent side when that is a real person or a
  proprietary system whose owner must agree.
- Consent is **affirmative and documented**. A generic call-center "this
  call may be recorded" notice does not clear the bar for redistribution
  under an open license.
- Honor removal requests in the next release.

---

## PII policy

Audio is de-identified before it is committed. In order of preference:
**do not capture it, then remove it, then reject the clip.**

Strip or redact:

- **Names** of any individual (caller, agent, third parties mentioned).
- **Phone numbers**, extensions, and any dialed digits (DTMF tones included).
- **Postal and email addresses.**
- **Account identifiers**: customer numbers, order numbers, card numbers,
  policy numbers, ticket IDs, anything that keys back to a person or account.
- Any other free-text detail that, alone or combined, re-identifies someone.

Strongly preferred practice:

- Use **synthetic or role-played content**. Actors reading a script hit the
  same turn-taking dynamics (interruption, correction, backchannel) with
  zero PII risk, while keeping the acoustics of a live call.
- **PHI stays out of scope.** Protected health information disqualifies a
  clip regardless of consent.

Redaction guidance:

- Replace spoken PII with a tone or silence of the **same duration** so the
  turn-taking timing survives. Cutting samples out shifts every onset after
  the edit and corrupts the labels.
- Redact the audio in **all channels**. A name muted on the mixed channel
  but audible on the caller channel is still present.
- Keep a private note of what was redacted and when (timestamps). The
  removed content itself never gets committed.

---

## Data-handling policy

- **The tool is self-hosted.** `hotato` reads local audio and writes local
  reports, offline; recordings stay on your machine. Contributing to the
  corpus is a deliberate, manual act of committing a de-identified,
  consented clip.
- **Storage.** Corpus audio lives in the repository alongside its scenario
  JSON, under the same MIT terms as the rest of the project. Keep working
  copies local until they are cleared for release; prepare them on your own
  machine, not in third-party cloud buckets, shared drives, or chat tools.
- **Only the final clip ships.** The committed clip is redacted and
  consented; securing or destroying the un-redacted original is the
  contributor's own call.
- **Contributor attestation.** Each real-audio PR carries a short statement
  from the contributor affirming: (a) consent is on file for every party,
  (b) PII has been removed per the policy above, (c) the clip contains no
  PHI, and (d) the contributor has the right to release it under MIT. This
  attestation is part of the merge record.

---

## Validity metrics: what we publish

The corpus exists to quantify measurement quality. That shapes how results
are reported.

We publish:

- **Per-signal measurement error, in milliseconds.** For each labeled call
  we compare the scorer's measured event times to the human labels and
  report the error distribution per signal:
  - `time_to_yield`: signed and absolute error in ms between measured and
    labeled floor-yield.
  - `talk_over`: error in ms on measured overlap duration.
  - onset agreement: error in ms between measured caller onset and
    `caller_onset_sec`.

  We report these as distributions (median, spread, worst case), so a reader
  sees the real shape of the error.
- **A `did_yield` confusion matrix.** Against the human `category`
  (`should_yield` / `should_not_yield`) label, we report the four-cell
  matrix: correct yields, correct holds, false yields (yielded on a
  backchannel), and missed yields (kept talking over a real interruption).
  The two error cells are the ones operators feel, so we surface them
  directly.

The distribution and the four-cell matrix are the report — each failure
mode scored on its own, so the trade-off between them stays visible. The
scorer works on speech energy over time, so validity means timing agreement
and decision agreement.

### Reading a corpus report

A healthy result looks like: tight millisecond error distributions on
`time_to_yield` and `talk_over`, onset agreement within a small ms band, and
a confusion matrix whose off-diagonal (false-yield / missed-yield) cells are
small and individually inspectable. A poor result looks like wide error
spread or a heavy off-diagonal, and it points you toward a concrete fix in
the call.

Where a real call fails, the fix map names candidate remedies, including a
learned engagement-control layer referenced at that level. Audio alone is the
weaker modality for that fix class; the corpus measures exactly how it holds
up on the room it was recorded in.
