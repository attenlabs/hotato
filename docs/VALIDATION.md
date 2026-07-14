# What Hotato validates

A turn handoff is not one number, so Hotato is validated on **three separate
jobs**, each measured on its own terms, each with an explicit reported output.
If you are judging whether to trust Hotato, judge these three jobs one at a time.

Everything below runs offline against recordings you control. Every threshold is
exposed and every frame is inspectable (`hotato run --dump-frames`).

---

## Job 1: timing reproducibility

**The question:** given the same recording and the same reference config, does
Hotato produce the same timing measurements every run? (Deterministic for a
fixed hotato version; byte-identical re-runs are verified in CI on Linux
x86_64, Python 3.10, 3.11, and 3.12 -- `.github/workflows/tests.yml`, job
`pytest`. The same double-run check now also runs in CI on macOS and Windows,
with the digest additionally compared ACROSS those OSes -- jobs `portability`
and `determinism` in the same file. Cross-OS agreement is measured and
reported there, not asserted -- it is not part of this claim.)

**What is reported.** Per scored event: `did_yield` (true/false),
`seconds_to_yield`, and `talk_over_sec`, plus the exact thresholds used
(`max_talk_over`, `max_time_to_yield`) and the frame grid behind them. No
learned weights, no sampling, no RNG: the energy VAD and the reference framing
are deterministic, so the numbers are byte-stable.

**How to check it.** Score the same file twice and diff the output.

```text
$ hotato run --stereo 01-hard-interruption.example.wav --expect yield
hotato [single] stack=generic offline=True
  1/1 events pass  (failed=0)
  [PASS] 01-hard-interruption.example.wav: did_yield=True seconds_to_yield=0.51s talk_over=0.51s
  exit_code=0

$ hotato run --stereo 01-hard-interruption.example.wav --expect yield
  [PASS] 01-hard-interruption.example.wav: did_yield=True seconds_to_yield=0.51s talk_over=0.51s
```

`seconds_to_yield=0.51s` and `talk_over=0.51s` are identical across runs. This
is the property a regression test needs: under a fixed hotato version and the
same pinned audio, channel map, onset, label, and scoring config, a changed
result means one of those pinned inputs changed, not that the scorer drifted.

**What this job establishes.** That the measurement is stable and re-derivable
by hand from [`METHODOLOGY.md`](../METHODOLOGY.md) -- not that 0.51s is the
"true" yield latency in some absolute sense, or that the reference thresholds
are right for your product.

---

## Job 2: candidate-discovery usefulness

**The question:** when Hotato scans a whole recording, does it surface the
moments a human reviewer would want to look at, ranked by salience?

**What is reported.** A ranked list of candidate turn-taking moments as **timing
facts only**: overlap onsets (caller became active while the agent was talking,
with the overlap length and whether the agent went silent), agent starts during
caller activity, and long response gaps. Each candidate is a timestamp and a
measurement -- the timing fact, reported without a verdict or an intent label.

```text
$ hotato scan --stereo 02-backchannel-mhm.example.wav --top 5
hotato scan: 02-backchannel-mhm.example.wav  (6.0s, 3 candidate moments)
Candidates are timing events. You decide the expected behavior; label with: hotato fixture create --onset <t> --expect yield|hold
  [ 1] t=2.09s  overlap_while_agent_talking  overlap=1.58s  agent did not go silent within 3.0s
  [ 2] t=3.19s  overlap_while_agent_talking  overlap=1.07s  agent did not go silent within 3.0s
  [ 3] t=4.29s  overlap_while_agent_talking  overlap=0.56s  agent did not go silent within 3.0s
```

The usefulness bar is **recall of human-notable moments at a workable candidate
count**, not precision against a ground-truth intent label (there is no such
label at scan time, by design). A candidate that turns out to be a harmless
backchannel is simply one you label `hold` and move on. The validation
artifact is the [trust gallery](TRUST-GALLERY.md), which
includes a deliberate false positive so you can see what an unhelpful candidate
looks like and why Hotato still surfaces it.

**What this job establishes.** Scan widens the net for you to make the call --
not that every candidate is a bug, or that a quiet region is guaranteed clean.

---

## Job 3: contract verification

**The question:** once you have labelled a moment's expected behavior
(`yield` = stop for the caller, `hold` = keep the floor through a backchannel),
does Hotato's PASS/FAIL verdict agree with that label on the audio, against an
explicit, portable, CI-enforced policy?

Today this job runs on a fixture (`hotato fixture create` / `hotato run`): a
labelled recording plus an explicit threshold policy, scored the same way on
every machine covered by CI (Linux, proven; macOS and Windows now run the same
scoring code path too, pending a first green determinism run -- see Job 1
above). The portable contract bundle (`hotato contract create` /
`hotato contract verify`, audio plus timing evidence plus trace evidence plus
label plus policy plus a CI command in one artifact) carries this exact job
forward into a single self-contained object once it ships; the verdict this
job validates does not change shape, only the artifact it travels in.

**What is reported.** Per fixture: the verdict (`PASS`/`FAIL`), the measured
signals behind it, and the named fix class when the failure maps cleanly to a
config family. Agreement is checked against **your** label, not against an
opaque key.

```text
$ hotato demo --no-open --format text
hotato demo: recorded calls a provider's default agent fails
hotato [suite] stack=generic offline=True
  0/2 events pass  (failed=2)
  [FAIL] fd-01-missed-interruption: did_yield=False seconds_to_yield=- talk_over=0.25s
         fix[config]: Missed interruption: the agent kept talking over the caller
  [FAIL] fd-02-backchannel-yielded: did_yield=True seconds_to_yield=0.34s talk_over=0.32s
         fix[engagement-control]: False barge-in: a backchannel was treated as a bid for the floor
  note: no single sensitivity threshold satisfies this battery
  exit_code=1
```

Both labelled failures are caught, and the battery-level note reports the
disagreement plainly when a missed interruption and a false stop fail in the
same run -- no single threshold satisfies both. Reporting that disagreement
instead of inventing a fix is part of the validated behavior.

**What this job establishes.** That the verdict follows the audio and the
label consistently -- not that the label was correct (you own the label), or
that a passing fixture means the agent is good in general.

---

## What Hotato measures, and where it stops

Read this as the scope of the claim, stated once, plainly.

- **Timing, not semantic intent.** Hotato measures timing; whether a caller
  sound meant "stop" or "mhm, go on" is a label you supply.
- **A likely layer, not certainty.** A slow yield can be TTS buffering,
  transport, or VAD. `diagnose` names a likely layer and stays
  `unknown_root_cause` when one recording cannot separate them. A voice trace
  (once the trace layer ships) narrows the candidates further, short of
  turning one into a proof.
- **Timing, not task success.** Whether the call booked the appointment,
  resolved the ticket, or satisfied the caller is a QA platform's job (see
  [COMPARE.md](COMPARE.md)).
- **A demonstration, not a vendor ranking.** Hotato scores calls, not
  platforms. A provider-default example demonstrates the threshold funnel on
  one assistant, one config, one date, one scripted caller.
- **Timing, not tone.** Sentiment, satisfaction, and CSAT sit outside what
  Hotato measures.
- **Reproducible timing measurements, with the method exposed.** The three
  jobs above are the whole claim -- no headline percentage, by design.

The validation plan for the launch battery (external testers, consented
fixtures, before/after) lives in
[docs/evidence/validation-plan.md](evidence/validation-plan.md).
