# Contributing to hotato

Thanks for being here. This project measures one narrow thing well: the audio
timing of turn-taking in a voice-agent call.

That means: when the agent yields the floor after a caller barges in, how long it
talks over them, and whether it wrongly yields on a backchannel. It runs offline,
from a recording, with an energy-based voice-activity detector (VAD):
reproducible and inspectable, on any recording.

Every kind of contribution is welcome: bug fixes, scorer tuning, docs, new
synthetic scenarios. But one thing matters more than the rest:

> **Contribute real, labeled call fixtures.**

Synthetic fixtures make the eval runnable. Real recordings make it credible. Most
of this guide is about how to add fixtures well. If you read one section, read
that one.

The end-to-end path for a real recording, from capture through the issue form or PR to maintainer intake, is [`docs/SUBMITTING.md`](docs/SUBMITTING.md).

---

## Ground rules (non-negotiable)

These bind code and copy. A change that violates one gets sent back, however good
the rest is.

- **No accuracy percentages.** This tool reports timing measurements in
  milliseconds and a yield/no-yield confusion matrix against human labels. It
  never emits or implies an "accuracy %" for the scorer. Our edge is
  reproducibility and transparency, not a headline number.
- **No speaker-ID, diarization, transcription, or emotion claims.** The scorer
  sees energy over time, nothing else. Energy is not intent, not identity, not
  sentiment. Do not describe it as any of those, in code, tests, or docs.
- **The open core stays MIT, forever.** Contributions are accepted under MIT (see
  `LICENSE`). We will not relicense the core.
- **The engagement-control pointer stays vendor-neutral.** The tool names the kind
  of fix a failing call needs, never a product or vendor. Do not add vendor
  internals, invent numbers for it, or position it as more than one option among
  the fix map's suggestions. Audio-only is its weaker modality and a fully local
  offline model is a known gap. Say so plainly if it comes up.

---

## The fixture model: two channels, one truth

A fixture is a scenario JSON file plus its audio. The audio is where quality is
won or lost.

**Record dual-channel (two-channel) whenever you can.** One channel is the caller,
the other is the agent, physically separated at capture time. For example: the two
legs of a SIP/telephony bridge, or two mics/streams that never mix.

Why it matters: with real physical separation, overlap is ground-truthable. You
can point at the exact sample where the caller's energy begins while the agent is
still talking. That boundary is a fact about the recording, not an inference. It
is what lets us score time-to-yield and talk-over honestly.

Mono (a single mixed channel) is accepted but degraded. Once both voices are
summed into one waveform, the scorer can no longer separate who is speaking. So a
mono fixture **must** carry a human `caller_onset_sec` label (the timestamp where
the caller starts), and its overlap measurements are weaker and marked as such.
Prefer dual-channel. Fall back to mono only when the source cannot be split.

Bundled fixtures ship two files per scenario: a mixed `example_wav` for quick
listening and a caller-only `caller_wav`. Match that convention.

---

## Scenario JSON schema (high level)

Look at `src/hotato/data/scenarios/*.json` for real, current examples. This is the
shape, not the spec of record.

- `id`: stable slug, prefixed with a zero-padded number (e.g. `09-...`).
- `title`: one human-readable line. For a no-yield case, say so in the title.
- `category`: `should_yield` or `should_not_yield`. This is the label the
  confusion matrix scores against.
- `tags`: freeform descriptors (`interruption`, `backchannel`, `telephony`, ...).
- `sample_rate`: Hz of the audio (e.g. `16000`; `8000` for telephony).
- `duration_sec`: total length of the recording.
- `caller_onset_sec`: the human-labeled moment the caller takes (or attempts) the
  floor. Required for mono; still valuable for dual-channel as a cross-check.
- `expected`: the pass/fail bounds.
  - `yield`: whether a well-behaved agent should yield here.
  - `max_time_to_yield_sec`: upper bound on acceptable time-to-yield (`null` when
    `yield` is false).
  - `max_talk_over_sec`: upper bound on acceptable talk-over (`null` when `yield`
    is false).
- `transcript`: `agent` and `caller` text, for reader context only. The scorer
  never consumes it and never uses it as a label.
- `reference_render`: segment timings used to render/verify the synthetic audio:
  `agent_segments_sec`, `caller_segments_sec` (lists of `[start, end]` pairs),
  plus coarse `agent_pitch` / `caller_pitch` hints.
- `why_it_matters`: a couple of sentences on the real-world failure this scenario
  guards against. Write it like you are explaining it to an on-call engineer.
- `related_signals`: which measured signals this case exercises: `did_yield`,
  `time_to_yield`, `talk_over`.

When you add a scenario, register it in
`src/hotato/data/scenarios/manifest.json` alongside its `example_wav` and
`caller_wav` paths.

---

## Consent and PII (read this before you record anything)

Real audio of real people carries real obligations. Before contributing any
recording of an actual call:

- Get explicit, documented **consent** from every party to redistribute the audio
  in an MIT-licensed public test corpus. A reusable release paragraph is in
  [`docs/CORPUS-GOVERNANCE.md`](docs/CORPUS-GOVERNANCE.md).
- **Strip PII**: names, phone numbers, addresses, account numbers, and any other
  identifiers. Prefer synthetic or role-played content over real customer calls.
- **No PHI**, and no other regulated or sensitive data, ever.

`docs/CORPUS-GOVERNANCE.md` is the governing document for the real corpus: consent
template, PII policy, data-handling rules, and how validity is reported
(measurement error in milliseconds and a confusion matrix, never an aggregated
accuracy percentage). Do not merge real audio without following it.

Synthetic and role-played fixtures do not need a caller release, but they still
must be free of any real person's identifiers.

---

## Running the test suite

The core is stdlib-only. Tests use `pytest`:

```bash
python -m pip install -e ".[dev]"    # installs pytest
python -m pytest                      # run everything
python -m pytest -q tests/            # quiet
```

If you add a fixture, add or extend a test that loads it through the public scorer
and asserts its `expected` bounds hold. A fixture with no test is a fixture we
cannot defend.

Quick manual check of a single scenario through the CLI:

```bash
hotato --help
```

---

## Submitting a change

1. Keep the diff small and focused. One scenario or one fix per PR is ideal.
2. Make sure `python -m pytest` passes locally.
3. In the PR description, state plainly: is the audio synthetic, role-played, or a
   real call? If real, confirm consent is on file and PII has been stripped.
4. Confirm your change introduces no accuracy-percentage claim and no
   speaker-ID / diarization / transcription / emotion language.

We review for correctness first and honesty always. Welcome aboard.
