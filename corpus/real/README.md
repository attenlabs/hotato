# corpus/real/ real conversational audio from the AMI Meeting Corpus

Every other fixture in this repository is deterministic shaped noise: audio
rendered for the energy reference, honest about being synthetic. This
directory is the first set of REAL recorded speech: real people, real
overlap, real barge-ins and backchannels, cut from a properly licensed
public corpus with the ground truth taken from human word-level
annotations.

## Provenance chain, in full

1. Source: the AMI Meeting Corpus (https://groups.inf.ed.ac.uk/ami/corpus/),
   released by its creators under CC BY 4.0. License verification with
   quotes and URLs: `LICENSES.md`. Every source file's URL and sha256 is
   pinned in `build_real.py` and recorded in `manifest.json`.
2. Audio: the corpus's individual headset microphones (IHM), one
   close-talking channel per participant. Two meetings are used: ES2002a
   (a scenario meeting) and EN2002b (a non-scenario staff meeting,
   different speakers).
3. Ground truth: the corpus's manual word alignments
   (`ami_public_manual_1.6.2.zip`, `words/*.words.xml`). Each clip's
   `caller_onset_sec`, its speech segments, and its category label are
   derived from these timings, not from any model.
4. Extraction: `build_real.py` cuts the SAME 9 second window from BOTH
   speakers' headset channels and writes one two-channel WAV per event:
   channel 0 = the party that starts talking (the "caller" role), channel
   1 = the party already holding the floor (the "agent" role). 16 kHz,
   16-bit PCM. Rebuilds are byte-identical; `--check` proves it.

Rebuild or verify everything (downloads about 200 MB of source audio into
`cache/` on first run, sha256-verified):

```bash
python3 corpus/real/build_real.py          # rebuild labels + clips + manifest
python3 corpus/real/build_real.py --check  # rebuild to a temp dir, byte-compare
```

## What the labels mean

- `category: should_yield` marks a genuine floor-take: the word alignments
  show one speaker starting a substantial utterance while the other is
  mid-utterance, and the floor holder actually stopping within a couple of
  seconds. The `expected` bounds are derived from what the human floor
  holder actually did, plus a stated 0.75 s margin.
- `category: should_not_yield` marks a backchannel: a short acknowledgement
  ("Mm-hmm", "Okay", "Oh right") dropped inside the floor holder's turn,
  with the floor holder keeping the turn per the transcript.
- `caller_onset_sec` is the annotated start time of the caller-side event,
  from the word alignments, mapped into clip time.
- Both parties are HUMAN. The labels describe what the human floor holder
  did; they are not judgments of any voice agent. These clips exercise the
  scorer on real speech. They do not benchmark a vendor and they carry no
  accuracy percentage.

Run them like any labelled battery:

```bash
PYTHONPATH=src python3 -m hotato.cli run --suite barge-in \
  --scenarios corpus/real/scenarios --audio corpus/real/audio
```

Expect a non-zero exit: several backchannel clips measure `did_yield=true`
(see the honest caveats below). The point is that every number is a real
timing measured on real speech; `manifest.json` records the expected
measurement for every clip and `tests/test_corpus_real.py` regression-tests
against it.

## Honest caveats, read before citing anything here

- Humans pause around backchannels. In 6 of the 7 kept backchannel clips
  the floor holder pauses for 0.25 s or more right around the
  acknowledgement; the energy VAD counts that as a yield, so the clip
  measures `did_yield=true` even though the transcript shows the turn was
  kept. That is real human behavior meeting an energy criterion, and each
  such clip says so in `provenance.notes`. It is a measurement, not an
  error, and also not a pass.
- Headset bleed is real. A loud speaker is audible on the quiet neighbor's
  close-talking mic. On one kept clip (`ami-es2002a-take-0677`) bleed makes
  onset AUTO-detection fire about 1.4 s early; the labeled onset from the
  word alignments is the usable one. Four extracted candidates were dropped
  in curation because bleed or quiet trailing speech made the measurement
  itself not meaningful; the dropped ids are listed in `build_real.py`.
- Meetings are not telephony. This is wideband headset audio from a meeting
  room, human-human, multi-party. It exercises the scorer on real speech
  and real overlap; it does not simulate a phone call or a human-agent
  exchange.
- The optional neural cross-check (`--backend neural`, Silero VAD) agrees
  with the energy reference on these real clips for the yield direction and
  lands within a few hundredths of a second on time-to-yield where both
  detect one. Two instructive differences, measured here: Silero bridges
  one 0.39 s human micro-pause that energy counts as a yield
  (`ami-en2002b-bc-0859`), and on the bleed clip it auto-detects the onset
  at 3.01 s (0.01 s from the annotation) where energy fires 1.4 s early on
  the bleed. On the synthetic suites the neural track is empty by design;
  on this real speech it is fully populated, which is the point of having
  real audio here.
- Word alignments are the ground truth here, and they have their own
  resolution (tens of milliseconds). Measured-vs-annotated onset deltas per
  clip are in `manifest.json` (`measured.onset_delta_sec`); on the 12 clean
  clips they are within 0.4 s, mostly within 0.03 s.

## Licensing of this directory

The clips and labels here are derived from CC BY 4.0 material and REMAIN
CC BY 4.0 (attribution in `LICENSES.md` and inside every label JSON). They
are deliberately NOT part of the MIT contribution corpus defined by
`corpus/label.schema.json`, and `corpus/validate.py` reflects that: on
these labels it reports exactly three policy differences (`source_type`
is `real` rather than a contribution enum value, `license` is CC-BY-4.0
rather than MIT, and `right_to_release_mit` is honestly false) and nothing
else. Structure, timings, expected-block consistency, and the two-channel
audio all conform, and `tests/test_corpus_real.py` pins that exact
difference set.

## Files

| file | what it is |
|---|---|
| `build_real.py` | The deterministic download + extract + label + score pipeline. |
| `scenarios/*.json` | One label per clip: onset, segments, transcript window, expected behavior, provenance, attribution. |
| `audio/*.example.wav` | Two-channel 16 kHz clips (caller ch0, agent ch1). Committed; about 7.5 MB total. |
| `manifest.json` | Sources with sha256, clip inventory, and the expected measurement for every clip. |
| `LICENSES.md` | License verification quotes, URLs, access date, attribution notice, and the sources that were rejected. |
| `sample-report.html` | One rendered report with the real audio embedded, as the shareable-artifact proof. |
| `cache/` | Gitignored source downloads. |
