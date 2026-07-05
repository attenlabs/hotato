# hotato

**The open turn-taking eval for voice agents.** *Does your agent drop the turn,
or hog it?* Point it at one of **your own call recordings** and it shows where your
agent talks over the caller — or misses a real interruption — then points each
failure at the config knob that addresses it. Free, MIT, offline, deterministic,
and built to be called by an AI agent mid-task.

**Wire it into CI and it catches the commit that makes your agent start talking
over people — before it ships.** It runs locally on any recording from any stack;
your call audio never leaves your box. No cloud, no account, no accuracy score to
argue with — just reproducible timing you can gate a PR on.

## Score your own call in under a minute

Bring a dual-channel recording (caller and agent on separate channels) from your
stack. Vapi and Twilio need only an API key — no SDK, no export step:

```bash
# Vapi (near-zero friction): an API key + a call id, nothing else
export VAPI_API_KEY=...
uvx hotato capture --stack vapi --call-id <call-id>

# Twilio dual-channel recording
export TWILIO_ACCOUNT_SID=AC...  TWILIO_AUTH_TOKEN=...
uvx hotato capture --stack twilio --recording-sid RE...

# LiveKit / Pipecat capture in your own pipeline — scaffold the recording config:
uvx hotato setup --stack livekit   # then: hotato capture --stack livekit --caller a.wav --agent b.wav
uvx hotato setup --stack pipecat   # then: hotato capture --stack pipecat --stereo captured.wav

# already have a two-channel WAV (caller on channel 0, agent on channel 1)?
uvx hotato run --stereo your_call.wav --expect yield
```

Want to see the capture → score loop run first? It works end-to-end **offline**,
with zero third-party deps and no network:

```bash
uvx hotato capture --stack vapi --demo
```

The scorer runs locally and never sends your audio anywhere. Every stack — plus
the honest Retell status (no self-serve stereo export yet) — is in
[`adapters/README.md`](adapters/README.md).

## The self-test (checks the tool, not your agent)

`hotato run --suite barge-in` runs a bundled **synthetic** battery. It is Hotato's
own regression self-test: it proves the tool is behaving, **not** that your agent
is good. No account, API key, or network:

```bash
uvx hotato run --suite barge-in
```

```text
$ uvx hotato run --suite barge-in
hotato [suite] stack=generic offline=True
  8/8 events pass  (failed=0)
  [PASS] 01-hard-interruption: did_yield=True seconds_to_yield=0.50s talk_over=0.50s
  [PASS] 02-backchannel-mhm:   did_yield=False seconds_to_yield=-   talk_over=1.57s
  ...
  exit_code=0
```

The bundled fixtures are synthetic — a runnable floor and a regression guard, not
production accents, codecs, or room acoustics. To measure **your** agent, capture a
real call (above). The core has **zero third-party dependencies**.

## Why "hotato"

Good turn-taking is a game of hot potato: take your turn, then pass it — fast and
clean. Don't fumble it (miss a real interruption) and don't clutch it (talk over
the caller). Voice agents get this wrong in both directions, all day. Hotato
catches exactly those two failures on your own call recordings and points each
one at a fix. It is a potato with opinions about whose turn it is.

## What it measures

It scores the audio-**timing** of turn-taking from a call recording, and returns
three objective signals per event:

- `did_yield` — did the agent stop talking for the caller?
- `seconds_to_yield` — how long that took (the latency of the yield).
- `talk_over_sec` — how many seconds it kept talking over the caller first.

From those it renders a `PASS`/`FAIL` verdict against the expected behaviour
(the agent should *yield* to a real interruption, and *hold* through a
backchannel like "mhm"). The bundled 8-scenario battery covers the vocabulary
engineers actually hit: hard interruption, backchannel, filler-start,
correction, 8 kHz telephony, double-talk, echo-bleed, and rapid turn-taking.

## What it does NOT do — and the honest ceiling

This is the part closed tools bury; we lead with it.

- **No accuracy percentage, anywhere.** These are reproducible timing
  measurements with an exposed method and every threshold a documented,
  overridable parameter — not a graded judgement of any detector's internal
  quality. Automated sub-second scoring on a single channel has a real ceiling.
- **Energy is not intent.** The scorer detects speech-level energy in time, not
  meaning. It does **no** speaker identification, **no** diarization, **no**
  transcription, and **no** emotion/intent detection.
- **Best input is a two-channel recording** (caller and agent on separate
  channels), where overlap is physically ground-truthable. Mono is accepted but
  degraded: separation is then only as good as the VAD, so mono requires an
  onset label and will not report separated overlap as if it were authoritative.
- **The bundled fixtures are synthetic** — a runnable floor and a regression
  guard, not production accents, codecs, or room acoustics. For real validity,
  bring 10–15 of your own labelled calls (see `CONTRIBUTING.md` and
  `docs/CORPUS-GOVERNANCE.md`).

Full method: `METHODOLOGY.md`.

## Optional neural cross-check (non-reference)

The scoring core is the **energy** VAD, and it stays the reference — every
published, golden, and bundled number comes from it, deterministically. For a
second opinion you can re-run the *same* turn-taking timing math over a learned
speech track:

```bash
pip install 'hotato[neural]'
hotato run --stereo your_call.wav --backend neural   # opt-in; energy is the default
```

It uses **Silero VAD (MIT)**, locally and offline, behind one shared contract (so
an open-weight turn-detector like Smart-Turn can slot in later). It **tightens
onset precision** on clean speech — but it does **not** recover intent: a cough
still reads as speech energy, and *any* single-channel VAD, energy or neural, can
mark it active. So this is a **flagged cross-check, not a new source of truth**,
and there is still **no accuracy number**. The bundled `--suite` self-test always
uses the energy reference; without the extra installed, `--backend neural` errors
cleanly rather than falling back to energy. Details: `METHODOLOGY.md`.

## The fix map

Every failing event carries exactly one honest fix:

- **`config`** — a concrete knob for your stack (`livekit` | `pipecat` | `vapi`
  | `generic`), the direction to move it, and the trade-off it makes. No upsell.
- **`engagement-control`** — when the failure is a *discrimination* problem (a
  genuine bid for the floor vs a backchannel or speech not addressed to the
  agent) that no single sensitivity dial can solve, it points — high-level, no
  numbers, and **vendor-neutral** — at the *kind* of fix the failure needs: a
  learned engagement-control / addressee-detection layer, not a config knob. The
  pointer names no vendor and nothing you can adopt, license, or buy; separating
  a real floor-bid from a backchannel is an open, hard research problem, and the
  tool says so plainly. It is pointed to only on that genuine both-axes case,
  never as an upsell.

## Use it from an agent (MCP)

```bash
uvx --from "hotato[mcp]" hotato-mcp
```

Exposes exactly one stdio tool, `voice_eval_run`, returning the identical JSON
envelope as the CLI, with the honest scope and limits stated inline in the tool
description.

## Use it in CI

`run` exits non-zero on any regression, so it drops straight into a PR gate:

```bash
uvx hotato run --suite barge-in --format json   # exit 1 on regression
```

## Exit codes

`0` all pass (or `--no-fail`) · `1` a regression (≥1 event failed) · `2`
usage/IO error.

## License

MIT (`LICENSE`). The open core stays open — it is never relicensed.
