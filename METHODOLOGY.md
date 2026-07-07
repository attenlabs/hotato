# Methodology

How every number this tool reports is computed, why it is reproducible, and
where the method stops being trustworthy. There is no accuracy percentage in
this document and none is implied anywhere in the tool. What follows describes a
measurement, not a claim about any detector's internal quality.

Hotato scores the audio *timing* of turn-taking from a call recording: whether
the agent stopped talking once the caller started (a yield), how many seconds
that took, how many seconds it kept talking while the caller was talking
(talk-over), and, on the same two tracks, the endpointing timing: how the
agent's response sits around the moment the caller finished speaking. It
measures energy over time. It does not understand speech.

## Read this first: the honest ceiling

The limits come first because they bound everything below.

- **Energy is not intent.** The detector marks a frame "active" when its
 short-time energy crosses a threshold. A cough, a slammed door, or a burst of
 line noise reads as active exactly like a word does. Nothing here recovers
 *meaning*, it recovers *when there was speech-level energy*.
- **You supply the label.** Hotato does not infer intent. You label the
 expected behavior for the event: yield means the agent should stop for the
 caller. hold means the agent should keep speaking through a
 backchannel/noise/acknowledgement. Hotato then measures whether the timing
 matched that label. "mhm" and "stop" can carry identical speech energy; the
 verdict is only ever your label checked against measured timing.
- **Sub-second single-channel boundaries sit near the resolution limit.** At the
 default 10 ms hop a boundary is located to within a frame or two, but the
 hangover that keeps an utterance whole (default 150 ms) smears every trailing
 edge by up to that much, and on one mixed channel deciding *whose* energy
 crossed the threshold is not always possible.
- **Two channels is the required input.** Hotato's main scorer requires
 separated caller and agent tracks: either one two-channel WAV or two aligned
 mono WAVs. A single mixed mono call is not enough to attribute talk-over
 reliably: with both voices summed into one waveform, the scorer cannot
 attribute energy to a speaker. With physically separate channels, overlap is
 a fact of the recording: both tracks are active at once, by construction.
- **Out of scope, permanently:** no speaker identification, no diarization, no
 speech-to-text, no emotion or intent detection, and no claim about any
 vendor's internal accuracy. Word-level semantics are not measured; only the
 timing of the yield is.

These same limits are emitted inline in the `limits` block of every JSON result
(`core.py`) and in the MCP tool description, so a consuming agent sees them.

## The pipeline, step by step

Everything runs offline, standard-library only (NumPy only accelerates the
per-frame RMS when importable; results are identical without it). Scoring is
deterministic: the same audio and `ScoreConfig` give the same output.

### Default parameters

Every value below is read directly from `ScoreConfig` (`score.py`) and
`VADParams` (`vad.py`); the same defaults apply to both channels unless overridden.

Framing and VAD (`VADParams`, applied per channel):

| Parameter | Default | Meaning |
|--------------------|-----------|---------|
| `frame_ms` | 20.0 ms | analysis window length for one RMS frame |
| `hop_ms` | 10.0 ms | step between frames (the time resolution) |
| `noise_percentile` | 0.10 | quietest 10% of frames estimates the noise floor |
| `rel_db` | 15.0 dB | a frame is active this many dB above the floor |
| `abs_gate_db` | -60.0 dBFS| nothing below this absolute level is ever active |
| `dyn_margin_db` | 22.0 dB | on a rarely-silent channel, hold the threshold at least this far below the loudest frames |
| `hangover_sec` | 0.15 s | stay "active" this long after energy drops |

Signal windows (`ScoreConfig`):

| Parameter | Default | Meaning |
|---------------------------|---------|---------|
| `yield_hangover_sec` | 0.20 s | agent must stay quiet this long to count as yielded |
| `max_search_sec` | 3.0 s | how long after onset a yield is searched for |
| `caller_proximity_sec` | 0.5 s | a yield only counts if the caller held the floor within this window of it |
| `turn_end_silence_sec` | 0.20 s | caller must stay quiet this long for the turn to count as ended |
| `premature_tolerance_sec` | 0.05 s | agent may lead the caller's turn end by up to this before it counts as premature |
| `onset_min_run_sec` | 0.05 s | minimum sustained active run for the caller onset to count |
| `agent_onset_lookback_sec`| 0.10 s | window before onset checked for whether the agent was already talking |

Both onset-detection windows are exposed on `ScoreConfig` as well:
`onset_min_run_sec` (the `0.05 s` sustained active run a caller onset must
clear, applied by `first_active_sec`) and `agent_onset_lookback_sec` (the
`0.10 s` window before onset checked for "was the agent talking at onset?").
Neither affects the per-frame active/threshold decision the frame dump records.
Every threshold the scorer uses is a named, overridable parameter -- there are
no hidden constants in the scoring path.

### Step 1, per-frame RMS

Each channel is cut into 20 ms windows stepped by 10 ms. For each window the
linear root-mean-square amplitude is computed (`frame_rms`, `audio.py`). At
16 kHz this is a 320-sample window with a 160-sample hop, so `hop_sec` is
exactly `160 / 16000 = 0.01 s`; it is derived from the integer sample hop over
the sample rate, so it matches `hop_ms` exactly at common rates.

### Step 2, RMS to dBFS

Each linear RMS value is converted to decibels relative to full scale:
`dBFS = 20 * log10(rms)`, with a floor of `-120.0 dBFS` substituted before the
log to avoid `log(0)` (`to_dbfs`, `audio.py`). This is the per-frame `*_dbfs`
column in the frame dump.

### Step 3, per-channel energy VAD

For each channel independently (`energy_vad`, `vad.py`):

1. Sort the frame dBFS values and take the `noise_percentile` (10th percentile)
 as the **noise floor**.
2. The base **threshold** is `max(noise_floor + rel_db, abs_gate_db)`, i.e.
 15 dB above the floor, but never below the -60 dBFS absolute gate.
3. **Dynamic-margin guard:** if a channel is almost never silent (e.g. an agent
 talking the whole clip), the 10th-percentile floor lands *inside* speech and
 would push the threshold above the speech itself. So compute
 `cap = max(dBFS) - dyn_margin_db`; if `cap` is above the absolute gate, lower
 the threshold to `min(threshold, cap)`. This cannot rescue a genuinely silent
 channel, because the guard only fires when loud content exists above the gate.
4. A frame is **raw-active** when its dBFS ≥ the threshold.
5. **Hangover:** after any raw-active frame, keep the channel active for
 `round(hangover_sec / hop_sec)` more frames, 15 frames (150 ms) at the
 defaults, so brief inter-word gaps do not fragment one utterance.

The resulting threshold and noise floor are constant across frames for a given
channel and are recorded verbatim in the frame dump.

### Step 4, caller onset

If a caller onset label is supplied (scenario `caller_onset_sec`, or `--onset`),
it is used directly. Otherwise the onset is the start of the caller's first
sustained active run, the first run of at least 0.05 s of active frames
(`first_active_sec`). The label is preferred because on mixed or noisy audio the
first energy is not always the caller. `onset_idx` is `round(onset / hop_sec)`,
clamped into range. No label and no detectable caller speech means there is no
caller event to score: the recording is reported not scorable (`scorable: false`
with a plain reason), never scored against frame 0, and a single-recording run
exits 2.

### Step 5, the three barge-in signals

Computed in `score_channels` (`score.py`):

- **`agent_talking_at_onset`**, was any agent frame active in the 0.10 s up to
 and including onset? If not, on a should-yield expectation there is nothing to
 yield: the event is reported not scorable (`scorable: false`, with the reason
 in `not_scorable_reason`) rather than passed or failed, and a single-recording
 run exits 2. The input is what is wrong (onset time, channel mapping, or
 expectation), and it is reported as such.
- **`did_yield`**, scanning from onset up to `onset + max_search_sec` (300
 frames), the first frame where the agent goes quiet and *stays* quiet for
 `yield_hangover_sec` (20 frames) **and** the caller held the floor within
 `caller_proximity_sec` (±50 frames) of that quiet point. The proximity
 condition is what stops an agent that merely finishes its own sentence seconds
 after an isolated backchannel from being scored as a barge-in response.
- **`time_to_yield_sec`**, `(yield_frame - onset_frame) * hop_sec`, floored at
 0; `None` if the agent never yielded within the search window.
- **`talk_over_sec`**, count of frames from onset to the yield point (or to the
 end of the search window if it never yielded) where the caller and the agent
 were *both* active, times `hop_sec`.

### Step 6, the two latency (endpointing) signals

Pure timing on the same two VAD tracks, no second model (`score.py`):

- **`caller turn end`**, the first frame at/after onset where the caller goes
 quiet and stays quiet for `turn_end_silence_sec` (20 frames). `None` if the
 caller never activates after onset or is still talking when the clip ends.
- **`agent response onset`**, skip any agent speech already in progress at
 onset (the pre-caller turn or the yield tail), then the first frame the agent
 becomes active again. `None` if the agent never starts a fresh run.
- With both defined, `lead = turn_end_frame - response_onset_frame` (positive
 when the agent starts *before* the caller finishes). If `lead` exceeds
 `premature_tolerance_sec` (5 frames), `premature_start_sec = lead * hop_sec`
 and `response_gap_sec` is null. Otherwise `premature_start_sec = 0.0` and
 `response_gap_sec = max(0, response_onset - turn_end) * hop_sec`. Both are
 null when not derivable from the tracks, never fabricated. Reported values
 are rounded to the millisecond (3 decimal places of seconds).

### Step 7, two additive, opt-in cross-checks: echo and resume

Both live entirely in hotato's own layer (`echo.py`, `resume.py`), never in
the vendored `_engine`, and both add an optional `signals` block without
changing `did_yield`, `seconds_to_yield`, or `talk_over_sec` for any existing
recording.

- **`signals.echo`** (`echo.py`), computed on every scored event. Leaked TTS
 (an agent that hears its own audio back on the caller channel) is, by
 construction, a delayed, scaled copy of the agent's own envelope. The check
 is a deterministic cross-channel coherence: at each lag from 0 up to
 `DEFAULT_MAX_LAG_SEC` (0.5 s), take the cosine similarity between the caller
 frame-RMS envelope and the agent envelope shifted by that lag; `coherence`
 is the best (highest) cosine found, `lag_sec` is the lag it occurred at, and
 `echo_suspected` is `coherence >= DEFAULT_COHERENCE_THRESHOLD` (0.7) with at
 least `_MIN_OVERLAP_FRAMES` (8) of overlap. Independent speech (real
 turn-taking, backchannels) does not correlate with the agent's own envelope
 at any lag, so it scores low. `--echo-gate` (opt-in, `hotato run`) holds an
 echo-suspected yield out of the verdict (`scorable: false`) instead of
 counting it as a clean pass; without the flag the primary verdict is
 unchanged and `signals.echo` is still reported. `hotato diagnose` and the
 single-run text output print a WARNING for every echo-suspected yield.
- **`signals.resume`** (`resume.py`), present only on events where the agent
 yielded. From the agent's own VAD track, look for a fresh onset within
 `DEFAULT_RESUME_WINDOW_SEC` (4.0 s) after the yield: `resumed` is whether one
 is found, `resume_gap_sec` is the seconds from yield to that onset (`null`
 if it did not resume). `restart_suspected` flags whether the longest
 contiguous agent run at or after the resume onset reaches
 `DEFAULT_RESTART_MIN_SEC` (2.0 s), the timing fingerprint of re-answering a
 whole paragraph from the top rather than finishing the interrupted clause.
 Whether the resumed words literally repeat the earlier ones is a transcript
 question, explicitly out of scope: `restart_suspected` is a run-length
 heuristic on timing, not a text diff.
- **`agent_stop_no_caller`** (`scan.py`, not part of the scored envelope: a
 `hotato scan` candidate) surfaces the companion timing fact: the agent went
 from active to quiet with zero caller energy anywhere in
 `caller_proximity_sec` on either side, so nothing on the caller channel
 explains the drop. It is a candidate for self-truncation, endpointing
 mis-fire, or an upstream (LLM/TTS) cutoff, not a verdict; label it with
 `hotato fixture create --expect hold` (or `yield`, if that silence was in
 fact correct) to turn it into a scored fixture.

## Why it is reproducible

- **Deterministic** given `(audio, ScoreConfig)`. No randomness, no network, no
 hidden state. The bundled fixtures are themselves rendered deterministically
 from a seed derived from `sha256(scenario_id)` (`render_examples.py`), and CI
 re-renders and diffs them to prove byte-stability.
- **Every threshold that drives the active/threshold decision is a named,
 overridable parameter** on `ScoreConfig` / `VADParams`, and the resolved
 values are echoed back in the frame dump's `config` block.
- **Every frame and every derived index is inspectable.** `--dump-frames PATH`
 writes, per frame: `t_sec`, `caller_dbfs`, `agent_dbfs`, `caller_active`,
 `agent_active`, and the constant `caller_threshold_db`,
 `caller_noise_floor_db`, `agent_threshold_db`, `agent_noise_floor_db`
 (`frame_dump`, `score.py`). Nothing the scorer decides is off the record.

### Worked example: re-deriving `did_yield` and `talk_over` by hand

The excerpt below is illustrative of the dump shape; run `--dump-frames` on your
own file for real values. Assume the defaults (`hop_sec = 0.01`), a caller onset
label at `2.40 s` (`onset_frame = 240`), and these constant thresholds from the
dump header: caller threshold `-55.0 dBFS`, agent threshold `-52.0 dBFS`.

```
 frame t_sec caller_dbfs caller_active agent_dbfs agent_active
 240 2.40 -18.4 True -21.1 True
 288 2.88 -19.0 True -20.7 True
 289 2.89 -20.2 True -48.9 True (hangover tail)
 290 2.90 -21.5 True -71.3 False
 ... (agent dBFS stays below -52 through frame 310) ...
 310 3.10 -20.8 True -69.4 False
```

Re-derive `did_yield`: agent was active at onset (frame 240), so the question is
meaningful. Scanning from 240, the agent first drops below its threshold at
frame 290 and stays below it for at least 20 consecutive frames (through 310), 
that satisfies `yield_hangover_sec`. The caller is active within ±50 frames of
290, so the proximity condition holds. Therefore `did_yield = True`,
`yield_frame = 290`, and `time_to_yield_sec = (290 - 240) * 0.01 = 0.50 s`.

Re-derive `talk_over`: count frames from 240 up to the yield frame 290 where
`caller_active AND agent_active`. In this excerpt that is frames 240 through 289
inclusive, 50 frames, so `talk_over_sec = 50 * 0.01 = 0.50 s`. Every one of
those booleans is just `dbfs >= threshold` (plus hangover), which you can
recompute yourself from the two dBFS columns and the header thresholds.

## Aggregate statistics: one definition each

The `report`, `team`, and `export` surfaces summarize many events with mean,
median, p90, and p95. All four come from one stdlib implementation
(`src/hotato/_stats.py`, `dist_summary`) so every published aggregate is
re-derivable by hand:

- **mean** is the arithmetic mean (`statistics.fmean`).
- **median** is `statistics.median` (p50).
- **p90** and **p95** are linear interpolation between closest ranks (the
 definition NumPy calls "linear"): sort the values ascending, let
 `pos = q * (n - 1)` for `q` in `{0.90, 0.95}`, then
 `p_q = v[floor(pos)] + frac * (v[floor(pos) + 1] - v[floor(pos)])`.
- **Rates are fractions**, never a percentage: a pass rate of 8 of 8 reads
 `1.00`. That keeps every aggregate in the same unit system as the
 measurements and leaves no door open to an accuracy-percentage reading.
- **Empty input returns null.** No aggregate is ever fabricated from zero
 measurements, and fewer than two runs is stated plainly rather than drawn as
 a trend.

`team` and `export` pool `response_gap_sec` (dead air before the agent
speaks, defined above) across every scored event into the same `dist_summary`
shape already used for talk-over and time-to-yield. `--max-response-gap`
turns the pooled p95 into a latency SLA gate: the run exits 1 exactly when the
pooled p95 exceeds the bound, the same pass/fail contract as
`--max-talk-over` and `--max-time-to-yield`, just pooled instead of
per-event. A plain `hotato export` (no `--max-response-gap`) still writes a
byte-identical `envelope.json` to a run with none of this stage's work
applied; the p95 and gate live only in the printed summary and the returned
manifest.

## Optional neural cross-check (non-reference)

The energy VAD above is the **reference**: every published, golden, and bundled
number comes from it, deterministically. Hotato also ships an **optional,
opt-in, explicitly non-reference** neural VAD backend so you can re-run the *same*
turn-taking timing math over a learned speech track and compare, a direct answer
to "this is just energy VAD, rebuildable in a weekend."

- **How to use it.** Install the extra and pass the flag on your *own* recording:
 `pip install 'hotato[neural]'` then `hotato run --stereo call.wav --backend neural`.
 The default is `--backend energy`. The `--suite` self-test **always** scores
 with energy (it is the reference), so `--backend neural` is ignored there.
- **What it is.** Silero VAD (MIT), run locally/offline. The engine itself keeps
 zero third-party dependencies and never imports a model; the backend is injected
 behind one shared interface, so an open-weight turn-detector (e.g. LiveKit's
 Smart-Turn) can be plugged in the same way later. It returns the **identical**
 `VADResult` shape as the energy backend, `active`, `hop_sec`, `threshold_db`,
 `noise_floor_db`, aligned to the same hop grid. A neural model has no dB
 threshold, so `threshold_db` / `noise_floor_db` are **synthesized**: they are the
 energy-domain description of the *same* audio, reported for inspection only; the
 neural `active` decision is a learned speech probability, not a dB crossing.
- **What it changes, honestly.** It **tightens onset precision** on clean speech
 (a learned boundary can beat a fixed dB threshold on some audio). It does **not**
 close the energy-vs-intent gap: a cough, a laugh, a door slam, or crosstalk still
 carries speech-band energy, and *any* single-channel VAD, energy or neural, can
 mark it active. Whether a sound is a genuine bid for the turn is not decidable
 from one channel's activity alone. **No accuracy percentage is claimed for either
 backend.** Neural is a flagged cross-check, not a new source of truth.
- **No silent fallback.** If the `[neural]` extra is absent, `--backend neural`
 raises a clean, explicit error and exits non-zero, it never quietly scores with
 energy and presents it as the neural result.
- **Verification status: verified against the real model.** Executed in this
 repo with silero-vad 6.2.1 (ONNX weights bundled inside the package, inference
 through onnxruntime on CPU, fully offline; note silero-vad itself depends on
 torch, so the extra installs it). Properties that hold, measured over the
 bundled 8 and the 40-scenario gold suite:
 - **Contract holds end to end.** The real model returns the identical
   `VADResult` shape as energy, on the same hop grid, through the public scorer.
 - **Deterministic.** Two full sweeps over all 48 fixtures produced
   byte-identical JSON, for both backends; single recordings are likewise
   byte-identical run to run.
 - **Reference untouched.** With the extra installed, energy outputs are
   byte-identical to energy outputs without it, and the frozen bundled 8
   verdicts are unchanged.
 - **On the synthetic fixtures the two tracks diverge, and that divergence is a
   property of the fixtures.** The renders are shaped noise built for the
   energy reference; a speech-trained model assigns them near-zero speech
   probability (identical through the ONNX and torch paths), so at Silero's
   default threshold the neural track is empty there and the timing math
   reports no yield on any of the 48. This measures the fixtures, not any
   agent. Point the neural cross-check at real recordings.
 - **On a real-speech recording** (a dual-channel barge-in fixture assembled
   from a recorded human utterance) both backends returned the same yield
   verdict; the measured yield timing differed (energy 0.65 s including its
   0.15 s hangover, neural 0.18 s, because a speech model segments the word
   gaps that the energy hangover bridges). One recording, reported as a timing
   observation, not an accuracy claim.
 - **Sample rates.** Silero accepts 8000 Hz, 16000 Hz, and integer multiples of
   16000 Hz (silero-vad decimates those itself and returns timestamps in the
   original sample coordinates). Any other rate fails with an actionable
   resample message from the seam. The energy backend measures at any rate.
 The real-model tests run automatically when the `[neural]` extra is installed
 and skip cleanly when it is absent (`tests/test_backend.py`).

## How validity is measured, not claimed

The bundled fixtures are synthetic: band-limited, syllable-modulated noise
rendered from exact segment boundaries declared in each scenario's
`reference_render` block (`caller_segments_sec`, `agent_segments_sec`, and an
optional `continuous` flag that renders one unbroken run per segment so the
active track equals the rendered boundaries to within one hop). Because the true
boundaries are known exactly, the scorer's **own measurement error** is
computable, and that is what to report, rather than an accuracy score:

- For a timing signal (onset, yield time, response gap), report
 `|measured - true|` in **milliseconds**, per scenario. The true value comes
 straight from the segment boundaries: true onset is the caller's first segment
 start; true yield time is the agent's segment end minus onset; true gap is the
 agent's next segment start minus the caller's turn end. Expect a residual on
 the order of the hop plus the hangover smear on trailing edges, read it, do
 not average it away.
- For `did_yield`, report a **confusion matrix** against the scenario `category`
 labels (`should_yield` vs `should_not_yield`), four cells: correct yields,
 missed yields, correct holds, phantom yields. Keep the cells separate.

**Never aggregate these into a single accuracy percentage.** A missed hard
interruption and a phantom yield on a backchannel are different failures with
different fixes; one blended number hides exactly the distinction the tool
exists to surface.

State plainly what the synthetic battery is for: it proves the plumbing works
end-to-end and catches regressions (a threshold change that moves a number shows
up immediately against the frozen golden output). It is **not** production audio
, no real accents, codecs, packet loss, or room acoustics. For validity on your
system, bring **10-15 of your own labelled calls** (two-channel where you can
get it) and run them through the same `run` / `--dump-frames` path; see
`CONTRIBUTING.md` and `docs/CORPUS-GOVERNANCE.md`.

## How to verify a score on your own recordings

1. Run `--dump-frames out.json` on the recording. Read the per-channel
 `*_threshold_db` and `*_noise_floor_db` from the header, and confirm
 `threshold = noise_floor + 15 dB` (or the dynamic-margin cap, or the -60 dBFS
 gate) matches your levels.
2. Scan the `*_dbfs` and `*_active` columns around the reported onset, yield,
 and gap indices and confirm each `active` flag is just `dbfs >= threshold`
 carried forward by the 150 ms hangover. Re-derive the signal by hand as above.
3. If the VAD is mislabeling your audio, tune and watch the numbers move
 predictably: raise `rel_db` or `abs_gate_db` if line noise is marked active;
 lower `rel_db` if quiet speech is missed; raise `hangover_sec` if one
 utterance fragments into several; adjust `yield_hangover_sec` /
 `caller_proximity_sec` to match how long *your* callers pause. Re-dump and
 confirm the thresholds and active flags moved as expected.

If you disagree with a number, the frames are on the table and every threshold
is yours to change. That is the point: a measurement you can audit, not a
verdict you have to trust.
