# Contributing to hotato

Thanks for being here. hotato finds what broke in your agent calls and pins
it so it never ships again: it simulates, evaluates, reviews, and tracks
calls across five dimensions (outcome, policy, conversation, speech,
reliability), with the evidence behind every result. Deterministic checks
stay separate from the model-judged rubric, and no output is ever a single
blended score.

Every kind of contribution is welcome. This guide gets your first one merged with
the least friction, then points you at the contribution that helps most.

## The 30-second contribution

The smallest useful change, start to finish:

- **A one-line doc fix.** A typo, a stale flag, a command that reads wrong.
  Edit the file on GitHub, open the PR. Done.
- **A labeled synthetic fixture.** One JSON scenario that stresses a timing
  edge the suites miss. Copy an existing label under
  [`corpus/suites/`](corpus/suites/), change the timings, run
  `python3 corpus/validate.py your_label.json` until it prints PASS.

Both are reviewed quickly and merge without a release. If you have more time,
read on.

## Ways to help

Pick whatever fits the time you have:

- **Good first issues.** Small, well-scoped, reviewed quickly:
  [open `good first issue` tickets](https://github.com/attenlabs/hotato/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
- **Fix a bug or sharpen the docs.** Open an issue with a copy-paste repro, or send
  a doc fix straight as a PR.
- **Add a synthetic scenario** that stresses a timing edge the suites miss. Start
  from [`corpus/README.md`](corpus/README.md).
- **Write a stack adapter or a fix-map entry.** Adapters live in
  [`adapters/`](adapters/README.md).
- **Contribute a labeled call recording.** The highest-value contribution, and the
  one this guide spends the most words on. Jump to
  [the corpus](#the-highest-value-contribution-a-labeled-call-recording).

## Your first contribution in five minutes

The core is standard-library Python, so setup is one install:

```bash
git clone https://github.com/attenlabs/hotato && cd hotato
python -m pip install -e ".[dev]"    # pytest + jsonschema
```

Supported Python versions: 3.9 to 3.13 (the tested matrix in CI). `pyproject.toml`
sets `requires-python = ">=3.9"` as a floor with no upper cap, so newer
interpreters install and are best-effort until they join the CI matrix.

Run the checks the way CI does, before you open a PR:

```bash
python -m pytest -q               # the full suite (bounded to tests/)
python scripts/copy_lint.py       # the copy gate: plain, declarative, no overclaims
```

Try the tool end to end with no account, keys, or network:

```bash
hotato start --demo               # sweep two bundled calls, write a dashboard, verify a contract
hotato --help                     # the public surface
hotato lab --help                 # the deep toolkit behind it
```

Then keep the diff small: one scenario, one fix, or one recording per PR.

## Surface stability and release cadence

The CLI has two surfaces, and they carry different stability promises:

- **Public** (everything `hotato --help` lists: `autopsy`, `scan`, `pin`,
  `prove`, `connect`, the `<stack> health` commands, `start`, `demo`,
  `doctor`, `console`, `production`, `serve`, `contract`, `describe`).
  These commands, their flags, and their exit codes are durable: a script
  or CI job written against them keeps working across releases.
- **Lab** (everything `hotato lab --help` lists). The lab surface evolves
  faster between releases. Every pre-1.17 top-level spelling keeps working
  unchanged as a compat alias, and `hotato describe` records both
  spellings for every command.

Releases follow a **monthly-stable cadence**: a versioned release ships
about once a month, carries the full CHANGELOG entry for everything that
landed, and is the unit the stability promise attaches to. Any change to a
public command's flags or exit codes is called out in the CHANGELOG at
that release.

## The highest-value contribution: a labeled call recording

hotato gets more credible with every consented, de-identified call clip in the
shared corpus. Synthetic scenarios prove the scorer matches its spec; recorded
calls prove it measures what happens on a live line. The full walkthrough (record,
label, validate, submit, intake) is [`docs/SUBMITTING.md`](docs/SUBMITTING.md). The
short version:

1. **Record dual-channel.** Caller on one channel, agent on the other, separated at
   capture: the two legs of a SIP bridge, or two streams that never mix. That
   separation makes overlap a fact of the recording, exact to the sample. Mono is
   accepted but degraded, and must carry a human `caller_onset_sec` label.
2. **Label it.** A small JSON file next to the WAV: the scenario shape plus
   provenance and attestation. Spec of record:
   [`corpus/label.schema.json`](corpus/label.schema.json); worked example:
   [`corpus/examples/sample-contribution.json`](corpus/examples/sample-contribution.json).
3. **Validate locally** until it prints PASS:
   ```bash
   python3 corpus/validate.py your_label.json
   ```
4. **Submit** through the
   [corpus issue form](https://github.com/attenlabs/hotato/issues/new?template=corpus_submission.yml)
   or a PR that adds the label and WAV under [`corpus/`](corpus/).

### Consent, PII, and PHI (read before you record anything)

Audio of people carries obligations. These are non-negotiable, and
[`docs/CORPUS-GOVERNANCE.md`](docs/CORPUS-GOVERNANCE.md) is the governing document
(consent template, PII policy, data handling, removal requests):

- **Documented consent** from every audible party to redistribute the audio in an
  MIT-licensed public test corpus. A reusable release paragraph is in the
  governance doc.
- **Strip PII**: names, phone numbers, addresses, account numbers, any identifier.
  Redact with same-duration tone or silence so the timing survives.
- **No PHI**, ever, regardless of consent.

Synthetic and role-played clips need no caller release, but must still carry no
identifier of any person. For the repeatable role-play recipe -- script shape,
the consent/PII/attestation checklist mapped to the schema, dual-channel
capture, and the defect-performed-on-purpose labeling -- follow
[`docs/RFC-ROLEPLAY-FIXTURES.md`](docs/RFC-ROLEPLAY-FIXTURES.md).

## Ground rules (these bind code and copy)

A change that violates one gets sent back, however good the rest is.

- **No accuracy percentage, no blended score.** hotato reports millisecond timing
  measurements, per-dimension pass/fail/inconclusive counts, and a yield/hold
  confusion matrix against human labels. No output carries an `overall_score`, and
  none implies an "accuracy %". The edge is reproducibility, not a headline number.
- **Two lanes stay separate.** Deterministic checks (phrase, PII, policy, tool-call,
  sequence, latency, outcome) stay behind a wall from the model-judged rubric lane.
  A rubric verdict is advisory and never merges into a deterministic count.
- **Energy is not intent.** The timing scorer sees energy over time. No output
  claims speaker identity, emotion, or intent. Optional transcription and
  diarization extras are context layers that never feed the reference score.
- **The open core stays MIT, forever.** Contributions are accepted under MIT (see
  [`LICENSE`](LICENSE)). The core is not relicensed.
- **The vendored `_engine` stays byte-identical to upstream.** New behavior goes in
  the hotato layer around it; `python sync_engine.py --check` stays green.
- **The engagement-control pointer stays vendor-neutral.** The fix map names the
  kind of fix a failing call needs, never a product or vendor, and invents no
  numbers for it. Audio-only is its weaker modality; keep the pointer to the kind
  of fix, not a capability claim.

## Opening a PR

1. Small and focused: one scenario, one fix, or one recording.
2. `python -m pytest -q` and `python scripts/copy_lint.py` both pass locally.
3. If you added a fixture, add or extend a test that loads it through the public
   scorer and asserts its `expected` bounds. A fixture with no test is one we
   cannot defend.
4. Fill in the checklist in the PR template. It confirms the ground rules above.

We review for correctness first, and hold the line on the ground rules always.
Welcome aboard.
