# Trust gallery: eight recordings, eight verdicts

Eight named input conditions, each with the `hotato` output it produces.
Every block below is verbatim CLI output from hotato 1.15.0, offline. The rows
correspond to the [trust matrix](TRUST-MATRIX.md).

The clean and echo cases use the bundled examples
`01-hard-interruption.example.wav` and `07-echo-bleed.example.wav` (shipped in
`src/hotato/data/audio/`); the backchannel case uses `02-backchannel-mhm.example.wav`.
The four defect cases (silent caller, silent agent, swap, mono) are
deterministic synthetic two-channel fixtures pinned in `tests/test_trust.py`,
each isolating one input defect, so every verdict reproduces byte-for-byte
from the shipped builders.

---

## 1. Safe stereo: the good case

Clean two-channel export, caller on ch0, agent on ch1. `trust` clears it.

```text
$ hotato trust --stereo 01-hard-interruption.example.wav
hotato trust: 01-hard-interruption.example.wav
  recording: 6.0s, 16000 Hz, 2 channels
  caller (ch0): 2.46s speech, first at 2.39s, peak -4.4 dBFS
  agent  (ch1): 2.71s speech, first at 0.19s, peak -4.4 dBFS
  leading silence: 0.19s
  crosstalk: coherence 0.306 (low) at 0.5s lag
  scorability: separated tracks yes, caller activity yes, agent activity yes
  => eligible for scan
```

**Verdict:** `eligible for scan`, exit 0. Score it -- the only condition
where a full turn-taking verdict is trustworthy without a caveat.

---

## 2. Silent caller: nothing to measure against

The caller channel carries no speech (a dead leg, a wrong export, a muted line).

```text
$ hotato trust --stereo silent-caller.wav
hotato trust: silent-caller.wav
  recording: 6.0s, 16000 Hz, 2 channels
  caller (ch0): 0.00s speech, -, peak -120.0 dBFS
  agent  (ch1): 5.76s speech, first at 0.19s, peak -9.1 dBFS
  leading silence: 0.19s
  crosstalk: coherence 0.0 (low) at 0.0s lag
  scorability: separated tracks yes, caller activity no, agent activity yes
  => NOT SCORABLE: caller channel has no detected speech
     next step: verify channel mapping or export dual-channel again
```

**Verdict:** `NOT SCORABLE`, exit 2. A yield is a response to a caller --
with no caller, there is nothing to be late to. Hotato names the gap instead
of printing a hollow pass.

---

## 3. Silent agent: no floor to interrupt

The agent channel is empty. There is no floor for the caller to barge into.

```text
$ hotato trust --stereo silent-agent.wav
hotato trust: silent-agent.wav
  recording: 6.0s, 16000 Hz, 2 channels
  caller (ch0): 2.16s speech, first at 0.99s, peak -9.1 dBFS
  agent  (ch1): 0.00s speech, -, peak -120.0 dBFS
  leading silence: 0.99s
  crosstalk: coherence 0.0 (low) at 0.0s lag
  possible channel swap: channel 0 (mapped as caller) holds the floor 2.16s vs 0.0s on channel 1 (mapped as agent); an agent usually holds the floor longer, so the caller/agent channels may be reversed
  scorability: separated tracks yes, caller activity yes, agent activity no
  => NOT SCORABLE: agent channel has no detected speech
     next step: verify channel mapping or export dual-channel again
```

**Verdict:** `NOT SCORABLE`, exit 2. Hotato also flags a possible channel
swap here: a caller-only recording looks like a mis-mapped agent. Two
independent checks point at the same fix -- re-check the export.

---

## 4. Swapped channels: candidate-eligible, verdict pending confirmation

Both channels carry speech, but the long floor-holder is on the channel
mapped as the caller. `trust` keeps the input candidate-eligible and holds
the verdict until the mapping is confirmed.

```text
$ hotato trust --stereo swapped-warn.wav
hotato trust: swapped-warn.wav
  recording: 6.0s, 16000 Hz, 2 channels
  caller (ch0): 5.76s speech, first at 0.19s, peak -9.1 dBFS
  agent  (ch1): 0.66s speech, first at 1.99s, peak -9.1 dBFS
  leading silence: 0.19s
  crosstalk: coherence 0.31 (low) at 0.5s lag
  possible channel swap: channel 0 (mapped as caller) holds the floor 5.76s vs 0.66s on channel 1 (mapped as agent); an agent usually holds the floor longer, so the caller/agent channels may be reversed
  scorability: separated tracks yes, caller activity yes, agent activity yes
  => scan with caution: channel 0 (mapped as caller) holds the floor 5.76s vs 0.66s on channel 1 (mapped as agent); an agent usually holds the floor longer, so the caller/agent channels may be reversed
  [!] not verdict-eligible (scan mode): channel mapping unconfirmed: suspected swap/crosstalk; confirm mapping or supply provider metadata
```

**Verdict:** `scan with caution`, exit 0: candidate-eligible, verdict
eligibility waiting on your confirmation. A swap would invert every yield
into a hold, so Hotato surfaces candidates for review and holds the
turn-taking verdict until the mapping is confirmed. Set
`--caller-channel` / `--agent-channel`, or supply provider metadata, to
restore verdict eligibility.

---

## 5. Crosstalk / echo bleed: candidate-eligible, verdict pending confirmation

The caller channel carries a delayed copy of the agent's own audio. Coherence
pegs at 1.0 and the measured leakage is -9.1 dB.

```text
$ hotato trust --stereo 07-echo-bleed.example.wav
hotato trust: 07-echo-bleed.example.wav
  recording: 6.0s, 16000 Hz, 2 channels
  caller (ch0): 5.69s speech, first at 0.31s, peak -13.5 dBFS
  agent  (ch1): 5.81s speech, first at 0.19s, peak -4.4 dBFS
  leading silence: 0.19s
  crosstalk: coherence 1.0 (HIGH) at 0.12s lag
  leakage: -9.1 dB (agent->caller, HIGH)
  scorability: separated tracks yes, caller activity yes, agent activity yes
  => scan with caution: cross-channel leakage: agent audio on the caller channel sits at -9.1 dB, a consistent delayed copy (lag 0.12s); leaked audio at this level can be counted as the other party's activity, so a downstream timing measurement may be wrong even though the tracks are separated
  [!] not verdict-eligible (scan mode): channel mapping unconfirmed: suspected swap/crosstalk; confirm mapping or supply provider metadata
```

**Verdict:** `scan with caution`, exit 0: candidate-eligible, verdict
eligibility waiting on confirmation. The "caller" activity may be leaked
TTS, so Hotato still surfaces candidates for review (example 8's scan names
the specific moment) and holds the turn-taking verdict until the mapping is
confirmed.

---

## 6. Mono: refused by default, tiered under `--diarize`

A single channel. The caller and the agent are mixed into one track.

```text
$ hotato trust --stereo mono-mixed.wav
hotato trust: mono-mixed.wav
  recording: 6.0s, 16000 Hz, 1 channel
  scorability: separated tracks no, caller activity no, agent activity no
  => NOT SCORABLE: the recording has a single channel, so the caller and the agent cannot be told apart
     next step: export a dual-channel recording with the caller on one channel and the agent on the other
```

**Verdict:** `NOT SCORABLE`, exit 2 -- the gold-standard refusal. Two
opt-in escapes exist, both indicative only: `--allow-mono` on `capture` /
`pull` / `sweep` accepts a mono-only stack in degraded mode (talk-over
unattributable, no SLA gate), and the `--diarize` separation front-end
reports a `high` / `low` / `refuse` tier, with `hotato run --mono call.wav
--diarize` stamping the verdict `indicative_only` at `low`. Neither stands
in for dual-channel. See [TRUST-MATRIX.md](TRUST-MATRIX.md) and
[DIARIZE.md](DIARIZE.md).

---

## 7. Backchannel candidate: surfaced, yours to label

A scan of a call where the caller says "mhm" over the agent. `scan` lists the
overlaps as candidates and hands the intent decision to you.

```text
$ hotato scan --stereo 02-backchannel-mhm.example.wav --top 5
hotato scan: 02-backchannel-mhm.example.wav  (6.0s, 3 candidate moments)
Candidates are timing events. You decide the expected behavior; label with: hotato fixture create --onset <t> --expect yield|hold
  [ 1] t=2.09s  overlap_while_agent_talking  overlap=1.58s  agent did not go silent within 3.0s
  [ 2] t=3.19s  overlap_while_agent_talking  overlap=1.07s  agent did not go silent within 3.0s
  [ 3] t=4.29s  overlap_while_agent_talking  overlap=0.56s  agent did not go silent within 3.0s
```

**Verdict:** three candidates, exit 0. Whether "agent did not go silent" is
correct (held the floor through a backchannel, good) or a bug (talked over a
caller trying to take the floor) is **your** call. `scan` reports the
timing; you label `hold` or `yield` -- the intent behind a caller sound is
always yours to decide.

---

## 8. Noisy false positive: a candidate that is really the agent

A scan of the echo-bleed call. One candidate is an overlap fact; the
second is Hotato flagging that the "caller" activity is leaked TTS.

```text
$ hotato scan --stereo 07-echo-bleed.example.wav --top 5
hotato scan: 07-echo-bleed.example.wav  (6.0s, 2 candidate moments)
Candidates are timing events. You decide the expected behavior; label with: hotato fixture create --onset <t> --expect yield|hold
  [ 1] t=0.31s  overlap_while_agent_talking  overlap=3.00s  agent did not go silent within 3.0s
  [ 2] t=0.31s  echo_correlated_activity     WARNING likely agent echo: coherence=1.00 at lag 0.12s  (caller channel looks like leaked TTS; a yield here may be the agent hearing itself)
```

**Verdict:** two candidates, exit 0. Candidate 1 looks like a bad
talk-over. Candidate 2 is Hotato telling you the overlap is probably the
agent hearing its own audio, not the caller interrupting. This is candidate
discovery's failure mode: the net is wide, and Hotato labels a
likely-spurious candidate instead of hiding it. You label it `hold` (or fix
the echo capture) and it never becomes a fixture.

---

## How to read the gallery

The eight cases fall into three buckets:

- **Score it** (1): clean stereo, verdict-eligible, full confidence.
- **Candidate-eligible, verdict pending confirmation** (4, 5): swap and
  crosstalk keep `scan` surfacing candidates (exit 0), with the turn-taking
  verdict waiting on your confirmation of the channel mapping. Example 8 is
  that same echo call under `scan`, naming the specific leaked moment.
- **Refuse** (2, 3, 6): silent caller, silent agent, and mono are not
  scorable; Hotato names the reason and the next step, and exits 2.

Case 7 is the everyday case: a candidate with no defect, waiting for your
label. The goal: any Hotato verdict you act on has already survived this
gauntlet.
