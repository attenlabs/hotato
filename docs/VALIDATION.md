# What Hotato validates

Hotato does not report a single accuracy percentage, and it never will. A turn
handoff is not one number. Instead, Hotato is validated on **three separate
jobs**, each measured on its own terms, each with an explicit reported output.
If you are judging whether to trust Hotato, judge these three jobs one at a time.

Everything below runs offline against recordings you control. Every threshold is
exposed and every frame is inspectable (`hotato run --dump-frames`).

---

## Job 1: timing reproducibility

**The question:** given the same recording and the same reference config, does
Hotato produce the same timing measurements every run, on any machine?

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
is the property a regression test needs: a red build means the audio changed,
not that the scorer drifted.

**What this job does NOT establish.** That 0.51s is the "true" yield latency in
some absolute sense, or that the reference thresholds are right for your product.
It establishes only that the measurement is stable and re-derivable by hand from
[`METHODOLOGY.md`](../METHODOLOGY.md).

---

## Job 2: candidate-discovery usefulness

**The question:** when Hotato scans a whole recording, does it surface the
moments a human reviewer would actually want to look at, ranked by salience?

**What is reported.** A ranked list of candidate turn-taking moments as **timing
facts only**: overlap onsets (caller became active while the agent was talking,
with the overlap length and whether the agent went silent), agent starts during
caller activity, and long response gaps. Each candidate is a timestamp and a
measurement. It is **not** a verdict, and it does not label intent.

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
backchannel is not a Hotato error: it is a candidate you label `hold` and move
on. The validation artifact is the [trust gallery](TRUST-GALLERY.md), which
includes a deliberate false positive so you can see what an unhelpful candidate
looks like and why Hotato still surfaces it.

**What this job does NOT establish.** That every candidate is a real bug, or that
a quiet region is guaranteed clean. Scan widens the net; you make the call.

---

## Job 3: contract verification

**The question:** once you have labelled a moment's expected behavior
(`yield` = stop for the caller, `hold` = keep the floor through a backchannel),
does Hotato's PASS/FAIL verdict agree with that label on the audio, against an
explicit, portable, CI-enforced policy?

Today this job runs on a fixture (`hotato fixture create` / `hotato run`): a
labelled recording plus an explicit threshold policy, scored the same way on
every machine. The portable contract bundle (`hotato contract create` /
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

Both labelled failures are caught, and the battery-level note refuses to name one
threshold when a missed interruption and a false stop fail in the same run. That
refusal is part of the validated behavior: Hotato reports the disagreement
instead of inventing a fix.

**What this job does NOT establish.** That the label was correct (you own the
label), or that a passing fixture means the agent is good in general. It
establishes that the verdict follows the audio and the label consistently.

---

## What we do not claim

Read this as a hard boundary, not a disclaimer.

- **No semantic intent.** Hotato measures timing. It does not know whether a
  caller sound meant "stop" or "mhm, go on." You supply that as a label.
- **No root-cause certainty.** A slow yield can be TTS buffering, transport, or
  VAD. `diagnose` names a likely layer and stays `unknown_root_cause` when one
  recording cannot separate them. A voice trace (once the trace layer ships)
  will narrow the candidates further; it will not convert a candidate into a
  proof.
- **No task success.** Whether the call booked the appointment, resolved the
  ticket, or satisfied the caller is out of scope. Use a QA platform for that
  (see [COMPARE.md](COMPARE.md)).
- **No vendor ranking.** Hotato never scores one platform against another. A
  provider-default example demonstrates the threshold funnel on one assistant,
  one config, one date, one scripted caller. It is not a benchmark of the vendor.
- **No human satisfaction or sentiment.** Hotato has no opinion on tone or CSAT.
- **No single accuracy score.** There is no headline percentage anywhere in
  Hotato, on purpose. The three jobs above are the whole claim.

The validation plan for the launch battery (external testers, consented
fixtures, before/after) lives in
[docs/evidence/validation-plan.md](evidence/validation-plan.md).
