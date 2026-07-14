# Corpus governance

The rulebook for contributing labeled voice-agent calls to hotato's corpus:
why they matter, consent, PII, and what gets published. See "Validity
metrics" at the end for what a report's numbers mean.

---

## Why real recordings matter

The synthetic fixtures in `src/hotato/data/` are a deliberately conservative
floor: rendered from known segment timings, so ground truth is exact and the
eval runs offline in seconds. They're the regression backbone -- proof the
scorer behaves as specified.

Production validity is a different question. Synthetic audio has clean
onsets, controlled overlap, and no room acoustics or codec artifacts beyond
what we inject. A scorer that looks flawless on synthetic fixtures still
needs proving on an 8 kHz call with echo bleed, cross-talk, and a caller
trailing off mid-word.

A small, carefully labeled corpus of real calls closes that gap: the
difference between "the tool does what the spec says" and "the tool measures
what happens on a phone line." The corpus stays small and high-integrity on
purpose -- every clip is consented, de-identified, and human-labeled. Ten
trustworthy calls beat ten thousand scraped ones.

---

## Scope and boundaries

- The corpus measures the **audio timing of turn-taking**: did the agent
  stop when the caller interrupted (yield), how long both talked at once
  (talk-over), and did the agent talk through a short "mhm" (backchannel
  handling).
- Labels are about **events in time**: did the agent stop, and when did the
  caller start.
- Everything here redistributes under the project's MIT license. A clip
  enters the corpus only if it can be released under MIT with consent.

---

## Consent / release template

Every party audible on a recording agrees to its inclusion, in a form you
can produce on request. Below: a short, reusable paragraph. Send it to the
caller and/or agent-operator, keep the signed or written reply on file, and
fill in the brackets.

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

- Get consent from **all** identifiable parties: the caller, and whoever
  runs the agent side, whether that's a person or a proprietary system
  whose owner must agree.
- Consent is **affirmative and documented**. A generic call-center "this
  call may be recorded" notice doesn't clear the bar for redistribution
  under an open license.
- Honor removal requests in the next release.

---

## PII policy

Audio is de-identified before it is committed, in order of preference:
**do not capture it, then remove it, then reject the clip.**

Strip or redact:

- **Names** of any individual (caller, agent, third parties mentioned).
- **Phone numbers**, extensions, and any dialed digits (DTMF tones included).
- **Postal and email addresses.**
- **Account identifiers**: customer numbers, order numbers, card numbers,
  policy numbers, ticket IDs, anything that keys back to a person or account.
- Any other free-text detail that, alone or combined, re-identifies someone.

Strongly preferred practice:

- Use **synthetic or role-played content**: actors reading a script hit the
  same turn-taking dynamics (interruption, correction, backchannel) with
  zero PII risk and a live call's acoustics.
- **PHI stays out of scope.** It disqualifies a clip regardless of consent.

Redaction guidance:

- Replace spoken PII with a tone or silence of the **same duration**, so the
  turn-taking timing survives -- cutting samples out shifts every onset
  after the edit and corrupts the labels.
- Redact **all channels**. A name muted on the mixed channel but audible on
  the caller channel is still present.
- Keep a private note of what was redacted and when (timestamps). The
  removed content itself is never committed.

---

## Data-handling policy

- **Self-hosted.** `hotato` reads and writes local files, offline;
  recordings stay on your machine. Contributing is a deliberate, manual
  act: committing a de-identified, consented clip.
- **Storage.** Corpus audio lives in the repository next to its scenario
  JSON, under the project's MIT terms. Keep working copies on your own
  machine until cleared for release -- never a third-party cloud bucket,
  shared drive, or chat tool.
- **Only the final clip ships.** The committed clip is redacted and
  consented; securing or destroying the un-redacted original is your own
  call.
- **Contributor attestation.** Each real-audio PR carries a short statement
  affirming: (a) consent is on file for every party, (b) PII is removed per
  the policy above, (c) no PHI, and (d) you have the right to release it
  under MIT -- part of the merge record.

---

## Validity metrics: what we publish

The corpus exists to quantify measurement quality, which shapes how results
are reported. We publish:

- **Per-signal measurement error, in milliseconds.** For each labeled call,
  we compare measured event times to human labels and report the error
  distribution per signal:
  - `time_to_yield`: signed and absolute error in ms between measured and
    labeled floor-yield.
  - `talk_over`: error in ms on measured overlap duration.
  - onset agreement: error in ms between measured caller onset and
    `caller_onset_sec`.

  We report these as distributions (median, spread, worst case), so a reader
  sees the shape of the error.
- **A `did_yield` confusion matrix.** Against the human `category`
  (`should_yield` / `should_not_yield`) label, we report four cells: correct
  yields, correct holds, false yields (yielded on a backchannel), and
  missed yields (talked over a real interruption). The two error cells are
  what operators feel, so we surface them directly.

The distribution and the four-cell matrix are the report: each failure mode
scored on its own, so the trade-off between them stays visible. The scorer
works on speech energy over time, so validity means timing agreement plus
decision agreement.

### Reading a corpus report

A healthy result: tight millisecond error distributions on `time_to_yield`
and `talk_over`, onset agreement within a small ms band, and a confusion
matrix whose off-diagonal (false-yield / missed-yield) cells are small and
individually inspectable. A poor result: wide error spread or a heavy
off-diagonal -- pointing you toward a concrete fix in the call.

Where a call fails, the fix map names candidate remedies, including a
learned engagement-control layer at that level. Audio alone is the weaker
modality for that fix class; the corpus measures how it holds up on the
room it was recorded in.
