# Changelog

All notable changes to Hotato are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Every entry reports millisecond measurement error and a confusion matrix, by
design. See `docs/BENCHMARK.md`.

## [Unreleased]

### Added
- **`hotato run --mono call.wav --diarize`, the opt-in, quality-gated
  mono-scorability front-end**: a single-channel (mixed) recording -- until now
  the hard coverage wall, rejected as not scorable -- becomes scorable by running
  an off-the-shelf speaker diarizer over the mono to recover who was active when,
  reconstructing two caller/agent tracks, and feeding the EXISTING dual-channel
  scorer (zero engine edit). The two-channel path stays the gold reference; this
  widens coverage, honestly labeled. A pluggable **diarizer-backend seam**
  (mirroring the neural-VAD seam) ships three backends behind `--diarizer`:
  `pyannote` (local, offline, CPU-viable, default; `[diarize]`), `sortformer`
  (NVIDIA NeMo, local/GPU, best self-hostable on telephone;
  `[diarize-sortformer]`), and `pyannoteai` (HOSTED, best absolute, requires
  `--egress-opt-in`; `[diarize-hosted]`). A **real per-file confidence gate**
  (extending `trust.py`'s scorability model) reads six signals -- speaker count,
  per-speaker activity, mean segmentation posterior, embedding cluster-separation
  margin, overlap ratio, segment churn -- into a `separation_confidence` and a
  tier: `high` scores normally (labeled `diarized-mono`), `low` scores but stamps
  `indicative_only` and fires NO SLA gate, `refuse` is not scorable (exit 2).
  Caller/agent assignment reuses the floor-dominance heuristic to PROPOSE a
  mapping stated as an assumption, overridable with `--caller-speaker` /
  `--agent-speaker`; a balanced mapping downgrades to indicative. On this path
  `signals.echo` / crosstalk is definitionally N/A (two slices of one microphone)
  and the echo gate never fires. `hotato trust --stereo mono.wav --diarize`
  reports the separability tier (high/low/refuse) WITHOUT scoring. No silent
  fallback anywhere: a missing extra / token / model raises a clean
  `BackendUnavailable` (exit 2) and NEVER scores raw mono. Honesty invariants:
  the default path (no `--mono`/`--diarize`) stays byte-identical (a mono file is
  still rejected as today), and a diarized-mono verdict is never presented as
  equivalent to a true dual-channel measurement. New module `hotato.diarize`
  (`DiarizationResult`, the backend registry, `reconstruct_tracks`,
  `separation_confidence`, `assign_speakers`, `prepare_diarized_mono`, the
  pyannote/sortformer/pyannoteai adapters, and a hermetic stub backend); new
  `--mono`/`--diarize`/`--diarizer`/`--caller-speaker`/`--agent-speaker`/
  `--egress-opt-in` flags on `hotato run` and `--diarize`/`--diarizer` on `hotato
  trust`; documented in `docs/DIARIZE.md`. The `LIMITS.does_not_do` /
  `METHODOLOGY.md` "no diarization" framing is superseded: two channels is the
  gold reference; mono is scorable via the opt-in, quality-gated front-end,
  labeled indicative below the bar. Speaker IDENTIFICATION is still out of scope
  (a diarizer assigns anonymous SPEAKER_00/01; it never says who a person is).
- New optional extras `[diarize]`, `[diarize-sortformer]`, `[diarize-hosted]`
  (the `[diarize]` path raises the effective Python floor to >=3.10; the stdlib
  core stays >=3.9). Dependency licenses are logged in `docs/DIARIZE.md` and
  carried in the envelope's `diarization.licenses` block (pyannote-audio MIT;
  community-1 weights CC-BY-4.0; Sortformer streaming v2 CC-BY-4.0 -- the offline
  v1 is CC-BY-NC and is never shipped).

## [0.5.0] - 2026-07-09

### Added
- **`hotato trust --stereo call.wav`, the input-health "trust doctor" you run
  BEFORE scoring a call**: it inspects one recording and reports whether the
  audio is even SCORABLE, so a bad export (a mono file, a silent channel, a
  swapped channel map, a hot capture) is caught up front instead of producing a
  confident-looking but meaningless turn-taking verdict downstream. The report
  covers per-channel activity (caller expected on channel 0, agent on channel
  1), a possible channel-swap flag (the channel mapped as the caller holding the
  floor far longer than the one mapped as the agent, the reverse of the usual
  pattern), sample rate, duration, clipping (per-channel peak dBFS and full-scale
  fraction), leading silence, crosstalk risk (cross-channel echo coherence), and
  the three scorability checks (separated tracks, enough caller activity, enough
  agent activity), then recommends `safe to scan` or `NOT SCORABLE` with the
  specific reason AND the next step (e.g. `caller channel has no detected speech`
  -> `verify channel mapping or export dual-channel again`). Three input defects
  are not scorable and exit `2` (mono, identical channels, a silent required
  channel); clipping, long leading silence, crosstalk, and a possible swap are
  non-blocking warnings. By construction it NEVER labels intent and NEVER emits a
  turn-taking verdict (no yield/hold, no pass/fail): it answers exactly one
  question -- is this audio good enough to score? Offline, stdlib-only, and
  reuses the existing hardened WAV reader, reference framing, energy VAD, and
  cross-channel echo coherence (no DSP is reimplemented). `--format json` emits
  one agent-parseable report (`kind: "input-health"`; branch on `scorable`, read
  `not_scorable_reason` / `next_step` on a defect). New module `hotato.trust`
  (`trust_report`, `render_text`); new `trust` subcommand in `hotato.cli`;
  documented in `docs/TRUST.md`. Exit codes: `0` safe to scan, `2` not scorable
  or usage error.
- **`hotato apply PATCH_JSON --clone --name NAME`, the guarded, clone-only
  staged apply -- the one command that can mutate external platform state, and
  the most conservative in the codebase**: it reads a `hotato patch` artifact and
  either PRINTS the fresh staging clone it would create (the default, fully
  offline dry run) or, only with `--yes` and credentials, creates a NEW staging
  assistant that is the source config with the patch applied. Five rules hold by
  construction. (1) CLONE-ONLY: there is no production-apply path; a non-`--clone`
  invocation is a clean usage error (`production apply is not supported; use
  --clone to apply to a fresh staging assistant`), and nothing ever `PUT`/`PATCH`es
  the source (the one writing call is a `POST` that creates a NEW assistant).
  (2) REFUSAL-FIRST: a both-axes threshold-funnel patch
  (`do_not_tune_single_threshold`) is REFUSED before anything, printing the exact
  vendor-neutral recommendation (`No config patch will be applied` / `Reason: both
  missed real interruption and false stop on backchannel, one threshold cannot
  safely fix both` / `Recommended: enable or add engagement-control /
  backchannel-aware turn detection`) and exiting a distinct, documented code (`3`)
  so a script can tell "refused by design" apart from a usage error -- the refusal
  is the feature. (3) OPPOSITE-RISK REQUIRED: apply refuses unless `--battery`
  carries BOTH a yield and a hold fixture, so a fix is never applied blind.
  (4) GATED SIDE EFFECT: the default dry run prints exactly the clone it would
  create and the patch it would apply, touching no network; only `--yes` with
  credentials reaches the platform, and the create is the only networked function
  (`apply.create_clone`) -- it reads the source (`GET`), applies the patch to a
  copy, and creates a NEW assistant (`POST`), never mutating the source. (5) NAME
  REQUIRED. Clone-appliable stacks are vapi (`POST /assistant`) and retell (`POST
  /create-agent`); LiveKit/Pipecat keep config in source, so apply points at the
  source edit from `hotato patch`. New module `hotato.apply` (`build_apply`,
  `apply_patch_to_config`, `build_clone_config`, `create_clone`,
  `battery_classes`); docs/APPLY.md; the `apply` subcommand and its `Exit codes:`
  epilog + `hotato describe` manifest entry.
- **`hotato verify --policy hotato.verify.yaml`, the anti-bandaid gate that
  fails a fix which moves one axis while regressing (or never testing) the
  other**: a policy file declares two things and BOTH must hold for verify to
  pass. `target.improve` is the success criteria (the failure the fix set out to
  move) -- a signed number is a required delta, so `talk_over_sec_p95: -0.5`
  means the pooled talk-over p95 must drop by at least 0.5s, and a keyword
  (`decrease`, `increase`, `no_worse`, `no_better`, `unchanged`) states
  direction, so `failed_count: decrease` means fewer fixtures may fail.
  `guardrails` are HARD fail conditions: `max_new_false_yields` and
  `max_not_scorable` cap what a threshold bandaid would silently trade in, and
  `require_hold_fixture` / `require_yield_fixture` refuse to certify a battery
  that does not even test the opposite axis. verify exits 1 unless EVERY
  guardrail holds AND EVERY target is met, so a patch that cuts talk-over by
  making the agent yield to everything meets the talk-over target but trips
  `max_new_false_yields` on the hold fixtures and the whole check fails. The
  guardrails and targets are shown in the `verify.html` proof (a `Policy check:
  PASSED`/`FAILED` headline plus an ok/violated and met/unmet table) and in the
  text and JSON output, reading ONLY the numbers `verify_sides` already measured
  -- nothing is re-scored. The report still states COINCIDENCE, never causation.
  The policy is parsed with the standard library alone (Hotato's core carries no
  third-party runtime dependency, PyYAML included), over the small documented
  subset the shipped `examples/verify-policy/hotato.verify.yaml` uses; an
  unknown key, a wrong-typed value, an empty policy, a tab indent, or a list is
  a clean exit-2 usage error, never a silent misread. Supported target metrics:
  `talk_over_sec_p95`, `seconds_to_yield_p95`, `failed_count`,
  `false_yield_count`. New: `verify.load_policy`, `verify.evaluate_policy`.
- **`hotato verify --out verify.html`, the flagship fix-verification proof as
  one self-contained offline HTML report**: a `.html`/`.htm` path passed to
  `hotato verify --out` now writes a single, zero-external-asset page (any other
  extension keeps writing the proof JSON, unchanged). The page reuses the
  report/analyze house style and reads ONLY the numbers `verify_sides` already
  measured; it re-scores nothing. Its headline is `Fix verification:
  PASSED`/`FAILED`, tied to the SAME honesty bar the text/JSON claim enforces (a
  battery-scale claim needs `--min-n` previously-failing fixtures AND at least
  one now passing AND no regression, so a low-n or regressed battery never earns
  a PASSED stamp). A TARGET section shows the failure it set out to move
  (pooled talk-over p95 and the failing-fixture count, before to after) and an
  OPPOSITE-RISK section shows what a naive threshold bandaid would silently
  break (hold/backchannel fixtures and the false-yield count, before to after,
  with the new-false-yield and not-scorable guardrails flagged ok/violated), so
  a "fix" that just makes the agent yield to everything is caught. Every paired
  fixture is listed with its machine-stable compare word, and unpaired fixtures
  are reported, never dropped. The conclusion states COINCIDENCE, never
  causation ("Hotato reports coincidence, not causation."), names what the
  artifact does not prove (timing only; no controlled experiment, no cause, no
  semantic-correctness judgement), and the whole page carries no em or en
  dashes. The verdict/target/opposite-risk numbers are derived by a pure
  `verify.verdict_model(v)` and the page by `verify.render_html(v)`.
- **`hotato pr create --fixtures DIR --repo OWNER/REPO --title T [--yes]`, a
  directory of promoted fixtures turned into a pull request that adds them as
  regression tests**: reads a hotato fixtures directory (the `--out DIR` that
  `hotato fixture promote` wrote, with `scenarios/` and `audio/`) and renders a
  plain markdown PR body: a title from the caller, a line per fixture (its id,
  the `yield`/`hold` label a maintainer chose, the call it was promoted from,
  and the clip onset), and the exact `hotato run --scenarios DIR/scenarios
  --audio DIR/audio` command that scores every added fixture. The fixtures are
  described as MEASURED CANDIDATE moments saved as tests, never verdicts and
  never intent. Two boundaries are structural, not prose: the body renderer
  (`prcmd.build_pr`) is a pure, offline function -- it emits the body and the
  exact `git` and `gh` argv it *would* run and touches no network and no
  subprocess (the one filesystem read, loading the scenarios, is isolated in
  `prcmd.load_fixtures` exactly as `issuecmd` isolates its sweep read); and the
  actual side effect runs only from `pr create` AND only under `--yes` with an
  explicit `--repo`. The default is a dry run that prints the body and the exact
  commands (cut a feature branch, stage the fixture files, commit, push, `gh pr
  create`) and changes nothing. Two safety invariants hold even under `--yes`:
  the change lands on a NEW feature branch (`hotato/<title slug>` by default,
  never the default branch directly, refused if it is a protected/default branch
  or equals `--base`) and the push is never a force-push. It reuses the same
  fixture schema `hotato fixture create`/`promote` writes, so the PR that lands
  the fixtures and the command that scores them read the same files. `git` and
  `gh` are required only for `--yes`; a missing binary or a non-zero exit
  surfaces as the standard exit-2 structured error, and a failing git step never
  proceeds to `gh`.
- **`hotato issue create SWEEP_JSON --repo OWNER/REPO --top N --label L [--yes]`,
  a sweep result turned into a confirm-or-ignore GitHub issue**: renders a
  `hotato sweep --format json` (or `hotato analyze --format json`) result into a
  plain markdown issue body with a title from the run, a worst-candidate block
  (call id, time, kind, the measured overlap/gap number, the report it came
  from), and, for each of the top `--top` candidates, a confirm-or-ignore
  section carrying the exact `hotato fixture promote FILE#N` command for BOTH a
  `--expect yield` and a `--expect hold` label plus a close-it line for when the
  moment is not a turn-taking failure at all. The moments are described as
  MEASURED CANDIDATES, never verdicts and never intent. Two boundaries are
  structural, not prose: the renderer (`issuecmd.build_issue`) is a pure,
  offline function -- it emits the body and the exact `gh` argv it *would* run
  and touches no network and no subprocess; and the actual side effect runs only
  from `issue create` AND only under `--yes` with an explicit `--repo`. The
  default is a dry run that prints the body and the exact `gh issue create`
  command and creates nothing, mirroring the project default
  `github_issue_on_candidate = false`: Hotato never opens an issue on your behalf
  unless you ask for it in that exact call. The candidate parsing reuses the
  SAME parser `hotato fixture promote` uses, and the per-candidate promote
  commands and the measured-number headline reuse the SAME renderers the
  sweep/analyze dashboard draws, so a ref in the issue resolves byte-for-byte to
  the ref on the page. `gh` is required only for `--yes`; a missing binary or a
  non-zero `gh` exit surfaces as the standard exit-2 structured error.
- **`hotato init webhook --stack vapi|retell|twilio --target fastapi --out DIR`,
  a generated set-and-forget webhook worker**: scaffolds a small, self-hostable
  FastAPI worker that turns a voice platform's call-ended webhook into a passive
  turn-taking regression monitor. The worker verifies the webhook secret, then
  hands the payload to `hotato ingest` (the same composable primitive) which
  fetches the dual-channel recording READ-ONLY and scans it for CANDIDATE
  moments; it adds no vendor call of its own. It writes a candidate report and,
  when configured, posts a Slack summary and/or a GitHub notification (both off
  by default; it opens no GitHub issue unless you explicitly set
  `HOTATO_GITHUB_CREATE_ISSUES=1` with a repo and token). The scaffold emits
  eight files -- `README.md`, `hotato.yaml`, `app.py`, `requirements.txt`,
  `Dockerfile`, `.env.example`, `.github/workflows/deploy.yml`, and
  `tests/test_webhook_contract.py`. The contract test ships inside the generated
  project and pins the FOUR honesty invariants with an AST scan of `app.py`
  (never a substring match on prose): (1) it never calls a platform
  config-mutation endpoint -- all vendor I/O is delegated to `hotato ingest`, so
  the worker holds no vendor API host; (2) it never labels intent or emits a
  verdict -- the only `hotato` subcommand it may call is `ingest`, never `run`,
  `verify`, `fixture`, `--expect`, ...; (3) it verifies the webhook secret
  before any parse, fetch, or scan (constant-time `hmac.compare_digest`, 401 on
  mismatch, run first in the handler); (4) the recording fetch is read-only.
  Per-stack signature verification and event detection ship as verified template
  fragments under `hotato/templates/webhook/` and render into the worker; only
  stacks with a verified webhook and a read-only fetch are offered. The three
  offered stacks are Vapi (shared-secret `X-Vapi-Secret`; event
  `end-of-call-report`), Retell (HMAC-SHA256 over the raw body in
  `X-Retell-Signature`; event `call_ended`, webhook-driven only since Retell has
  no list endpoint), and Twilio (HMAC-SHA1 over url + sorted params in
  `X-Twilio-Signature`; the `recordingStatusCallback` `completed` status, with
  dual-channel handled read-only by `hotato ingest`). Vapi is the reference
  worker and Retell and Twilio reuse its skeleton, differing only where the
  platform genuinely differs (verification, event name/shape, recording-fetch
  channel handling). Scaffolding is offline: no network and no credentials are
  needed to generate. Gated by `tests/test_init_webhook.py`, which asserts the
  eight files land, `app.py` parses (syntax + AST), `hotato.yaml` matches the
  worker schema, and the four invariants hold under an AST scan, for all three
  stacks, and drives each stack's worker end-to-end (a bad secret is rejected
  401 before anything runs, a non-terminal event is ignored, a call-ended event
  invokes only `hotato ingest --stack STACK`).
- **Promote actions on every analyze/sweep dashboard card**: each ranked
  candidate card now carries three actions. "Promote as yield fixture" and
  "Promote as hold fixture" copy the exact `hotato fixture promote
  REPORT_JSON#RANK --expect yield|hold --id SUGGESTED_ID --out tests/hotato`
  command to the clipboard (`navigator.clipboard`, with a hidden-textarea
  `execCommand` fallback for `file://` pages); "Ignore" hides the card on
  the page only, client side, no state, pausing its embedded player first.
  The ref number is the card's own #N rank chip; the suggested id is call id
  + kind + rank, kebab-cased to the same slug rule `fixture create --id`
  enforces; the report-json name is the producing command's DEFAULT json
  result name (`hotato-analyze.json`, `hotato-sweep-STACK.json`), never the
  --out path, so the dashboard stays byte-identical whatever the page was
  saved as, and a caption names that file and how to write it. You pick the
  label; the page never does. No animation anywhere, so reduced-motion needs
  no gating: feedback is an instant text swap. Gated by
  `tests/test_analyze_promote_buttons.py`, which parses the DOM attributes
  (never substring-matches), pins the exact copied payloads, rank refs, and
  slug ids, and runs one copied command verbatim against the json it names.
- **`hotato fixture promote CANDIDATE_REF`, the confirm step of the monitor
  loop**: promote one candidate moment from a `hotato sweep --format json` or
  `hotato analyze --format json` result straight into a permanent regression
  fixture. The ref names the result file and the candidate --
  `hotato-sweep.json#3` (the Nth candidate, 1-based in rank order, the same
  #N the report shows) or `analyze.json#call_abc123:2` (the Nth candidate
  from one call, matched by source path, file name, extension-stripped stem,
  or pulled call id). The candidate carries the recording, the onset, and the
  kind, so no `--stereo` and no `--onset` is retyped; you add the
  `--expect yield|hold` label and promote reuses the exact `fixture create`
  path: the clipped two-channel `DIR/audio/ID.example.wav` plus
  `DIR/scenarios/ID.json`, scored immediately, with a not-scorable candidate
  refused with the honest reason (exit 2) and partial outputs removed. The
  scenario provenance records `created_by: hotato fixture promote`, the
  candidate ref, and the candidate kind. To make refs resolvable from
  anywhere, sweep/analyze envelopes now also record `folder_path` (the
  analyzed folder's absolute path); a moved result still resolves via
  `--folder DIR`, and an unresolvable recording names every path tried.
  Gated by `tests/test_fixture_promote.py`: both ref forms, end-to-end
  promotes from a real `sweep --demo` result and a real `analyze` result, the
  1-based rank contract, resolution fallbacks, and every refusal path.
- **`hotato sweep --demo`, the zero-setup first sweep**: runs the full
  pull -> analyze sweep pipeline over the two bundled real demo calls (the
  same recordings `hotato demo` scores), with no vendor account, no
  credentials, and no network. The output is a real sweep's output: the same
  HTML dashboard (default `hotato-sweep-demo.html`) and the same
  `--format json` envelope, pull summary block included, produced by the
  identical analyze code path. `--demo` names a source of calls, so combining
  it with `--stack`, `--call-id`, `--since`, `--allow-mono`, `--dir`, or any
  credential flag is a clean exit-2 usage error that names each offending
  flag. Gated by `tests/test_sweep_demo.py`, which runs every test under a
  network guard (`urllib.request.urlopen` plus raw socket connects) and
  asserts the demo envelope's key set equals a real sweep's.

## [0.4.1] - 2026-07-08

### Fixed
- **`hotato describe` and `hotato --version` self-reported "0.3.1" in the
  0.4.0 wheel**: the 0.4.0 release bump missed the `__version__` literal in
  `src/hotato/__init__.py`, so every runtime self-report -- the `--version`
  banner, the `describe` manifest's `version` field (json and text), and
  stackbench's `provenance.hotato_version` -- carried the previous release's
  number. Scores and envelopes were unaffected; only the reported version
  string was wrong. Now gated by `tests/test_version_lockstep.py`, which
  anchors `hotato.__version__`, the `describe` manifest, and installed dist
  metadata against `pyproject.toml` (the previous describe test compared the
  manifest to `__version__` itself, which agreed even when both were wrong),
  and the release checklist enumerates every version lockstep site.

## [0.4.0] - 2026-07-08

### Fixed
- **Softer unsourced wording in `docs/WHY.md` and `README.md`**: "about one in
  five reported barge-in bugs" is now "in our observed reports, many alleged
  barge-in bugs," and "the two highest-frequency complaints" is now "two
  common complaints"; no percentage or frequency ranking is claimed. Same
  treatment for "four timing failures dominate real transcripts" in
  `docs/WHY.md`, now "show up again and again."

### Added
- **`hotato describe`, the generated capability manifest**: `hotato describe
  [--format json|text]` walks `build_parser()`'s own subparsers (including the
  nested `benchmark compare` / `fixture create`) and emits every subcommand's
  name, purpose, argument list (name, type, required, default, help), and
  documented exit codes, plus the tool version and the two schema URLs read
  straight from the shipped schema files' `$id`. One call for an agent to
  learn the whole CLI instead of scraping `--help` across ~18 subparsers;
  generated from the live parser, so it can never drift from the real flags;
  pure and deterministic. Alongside it: `hotato doctor --format json` (the
  machine envelope on stdout, `report: <path>` on stderr, mirroring `demo`),
  and a single `_EXIT_CODES` source of truth that templates a uniform "Exit
  codes:" epilog into every subparser (previously only six carried one, with
  inconsistent wording); `describe`'s manifest reads the same table, so the
  help text and the manifest cannot disagree.
- **`hotato patch` / `verify` / `loop`, the closed loop (find -> fix -> prove it
  is fixed)**: three commands that carry a fix plan the rest of the way, with the
  two irreversible decisions kept in human hands. `hotato patch <fixplan.json>`
  renders a plan's abstract `{field, from, to}` into a LITERAL, paste-ready
  artifact per platform: a JSON merge-patch body plus a ready `curl` against the
  platform's real config-update endpoint (Vapi `PATCH /assistant/{id}`, Retell
  `PATCH /update-agent/{agent_id}`), using the exact field names the plan carries
  from fixmap's verified knob catalogue; for LiveKit/Pipecat, whose config lives
  in agent source, it emits the exact constructor-kwarg source edit instead of a
  fabricated endpoint; for an unknown stack it names the knob family and asks for
  a target. For the genuine both-axes `do_not_tune_single_threshold` plan it
  emits NO config patch and prints the vendor-neutral, numbers-free
  engagement-control pointer instead (which fires ONLY on that case, never as a
  generic upsell). HONEST: patch PRODUCES the change; it never applies it to your
  platform and makes no network call (`applies_change` is pinned false).
  `hotato verify --before <old-run> --after <new-run>` gives a battery-scale
  before/after proof after you apply the change and re-capture the failing
  fixtures: "N of M fixtures that used to fail now pass, and K of L hold fixtures
  still pass". It reuses the `hotato compare` taxonomy per fixture (`fixed`,
  `regressed`, `improved`, `worse`, `unchanged`, `still_pass`, `not_scorable`)
  and aggregate's pooled-distribution definitions for the talk-over / time-to-
  yield shift. It reports COINCIDENCE, never causation; refuses the headline
  claim under low n (`--min-n`, default 3) while still printing the per-fixture
  facts; treats an unjudgeable side as `not_scorable` and an unpaired fixture as
  reported, never silently dropped; `--fail-on-regression` exits 1 on a
  regressed/worse fixture. `hotato loop [FOLDER]` is one-command orchestration
  with memory: the first run discovers candidate moments (`analyze` -> `scan` ->
  rank) into `.hotato/loop-state.json`; once you have labeled fixtures with
  `fixture create`, the next run plans a guarded fix and points at `hotato patch`
  then `hotato verify`. It tracks state across runs and NEVER auto-labels (you
  supply every yield/hold intent) or auto-applies. New docs: `docs/FIX-LOOP.md`;
  wired into `hotato describe` and the exit-code epilogs. See also
  `docs/FIX-PLANS.md`.
- **`hotato connect` / `pull` / `sweep`, the connect-once bulk pull-and-analyze
  flow**: `hotato connect <stack>` captures a stack's credentials once, runs a
  lightweight live auth check, and stores them at `~/.hotato/connections.json`
  with file mode 0600 (directory 0700) -- local only, never sent anywhere but
  the vendor's own API, never logged. After connecting, `--stack` and the key
  are optional for `pull`/`sweep` when exactly one stack is connected (they also
  fall back to the stack's environment variable). `hotato pull [--stack X]
  [--since 7d] [--limit 50] [--out DIR] [--allow-mono]` bulk-fetches recent
  recordings by looping the existing single-call `capture` fetch over each
  platform's list endpoint into a local folder; a recording that cannot be
  fetched is a clean per-file skip with its reason, never a crash. `hotato sweep`
  runs `pull` then the same zero-config `analyze` over the pulled folder = the
  flagship "connect once, see every turn-taking problem across all your real
  calls" flow, writing one self-contained offline dashboard. Every list/fetch
  endpoint is used exactly as verified verbatim in the integration spec: Vapi
  `GET /call`, Twilio `GET .../Recordings.json`, Bland `GET /v1/calls`,
  ElevenLabs `GET /v1/convai/conversations`, Synthflow `GET /v2/calls`, Millis
  `GET /call-logs`, Cartesia `GET /agents/calls`. Where the spec marks a
  list-calls endpoint unconfirmed (Retell) or a platform capture-in-your-infra
  (LiveKit, Pipecat), no endpoint is fabricated -- Retell pulls from explicit
  `--call-id` values and the limitation is documented honestly. Platform
  payloads are treated strictly as untrusted data (a malformed payload is a
  clean error, never an action from the payload).
- **`--allow-mono` capture/pull adapters for Bland AI, ElevenLabs Conversational
  AI, Synthflow, Millis AI, and Cartesia (Line)**: each with the exact
  spec-verified list + fetch endpoint and the honest mono caveat. These
  platforms produce a single combined recording with no documented per-party
  channel, so scoring is degraded and gated behind `--allow-mono` /
  `HOTATO_ALLOW_MONO=1`, labelled indicative only. `capture --stack` now accepts
  these stacks in addition to the original five.
- **`docs/CONNECT.md`** (the connect/pull/sweep recipe with per-stack
  credentials) and a comprehensive, honest rewrite of **`docs/ADAPTER-STATUS.md`**
  from the integration spec: build-now dual-channel (Vapi, Retell, Twilio,
  LiveKit, Pipecat), mono-only `--allow-mono` (Bland, ElevenLabs, Synthflow,
  Millis, Cartesia), not integrable (Deepgram Voice Agent, PlayAI), and
  unconfirmed (Regal, Thoughtly, Sindarin, Grok, and channel-layout edge cases),
  each with the exact verified endpoint and honest notes.
- **`hotato analyze <folder>`, the zero-config drop-a-folder flagship**: point
  it at a folder of real dual-channel call recordings and it walks EVERY
  recording label-free with the existing whole-call scanner (`hotato scan`),
  aggregates the candidate turn-taking moments across all calls, and ranks them
  by the scanner's own salience (overlap seconds / gap seconds / echo coherence)
  so the worst moments float to the top. No scenarios, no labels, no onset, no
  flags required. It writes ONE self-contained, offline HTML dashboard reusing
  the `report.py` house style and its timeline SVG renderer: each top moment
  shows the call file, the timestamp, the candidate kind, the measured number,
  and a to-scale caller/agent timeline. For the top moments (`--audio-top`,
  default 8) the REAL audio around the moment is embedded inline as a base64 WAV
  data URI (nothing uploaded, zero external requests) with the **hear-the-bug
  player**: press play and a playhead sweeps that moment's timeline in sync with
  `audio.currentTime` (via `requestAnimationFrame`), landing on the measured
  overlap or gap; reduced-motion safe (the playhead rides `timeupdate` instead
  of the animation loop). `--format json` emits the ranked candidates plus their
  metadata for an agent to drive. Framed honestly throughout as MEASURED
  candidate timing moments you review and label with `hotato fixture create`,
  never a pass/fail, a failure count, an intent claim, or an accuracy number.
  Non-dual-channel or unreadable files are reported cleanly as skipped with
  their reason, never a crash. A bare `hotato <folder>` (a directory as the
  first argument) routes to `analyze`. Exit codes: 0 ran (candidate moments
  listed, possibly zero), 2 usage/IO error. Docs: `docs/ANALYZE.md`. Reuses
  `scan.py`, `report.py`, and the stdlib WAV reader; adds `scan.activity_tracks`
  (the exact per-frame tracks the scanner walks, for drawing the per-moment
  timelines).
- **`hotato ingest`, the composable passive on-ramp**: wire a webhook to invoke
  `hotato ingest` once and every completed call is scanned for candidate
  turn-taking moments automatically, so you never have to remember to run a CLI
  after a bad call. It composes existing primitives and adds only a per-stack
  webhook parser: it parses the platform payload for the call id / recording
  locator (`message.call.id` for Vapi, top-level `event` + `call.call_id` for
  Retell, `RecordingSid` for Twilio's form-encoded `recordingStatusCallback`,
  `egressInfo.fileResults[].location` for LiveKit egress, and a user-supplied
  `recording_path` / `recording_url` for Pipecat; field paths verified against
  live vendor docs on 2026-07-07, parsed defensively where unconfirmable), reuses
  the same per-stack fetch as `hotato capture` for the dual-channel recording,
  then runs `hotato scan` for candidates. Ingest is discovery, never a pass/fail
  and never an intent claim: exit 0 means it ran (candidates possibly zero), exit
  2 means a parse/fetch/IO error or not-scorable input. It never auto-labels,
  auto-fixtures, or auto-tunes; you promote a candidate with `hotato fixture
  create`. It is not a daemon (you own the trigger), the only network is the same
  recording fetch `capture` does, and a webhook payload is treated strictly as
  untrusted data and never executed. `--out` writes an HTML candidate report; new
  guide in `docs/INGEST.md` with the webhook-to-ingest recipe per stack.
- **CI sdist guard**: a new `sdist-guard` job in
  `.github/workflows/tests.yml`, separate from the `pytest` job, builds the
  sdist, extracts it to a clean directory, installs only that extracted tree
  into a fresh venv, and runs the full pytest suite from inside it. Fails the
  build on any collection error. This is the failure mode that shipped
  unguarded in 0.2.3 and 0.3.0: a green wheel masked a broken sdist.
- **MCP registry manifest and docs**: `server.json` at the repo root (name
  `io.github.attenlabs/hotato`, matching the `mcp-name:` marker already in
  `README.md`), pointing at the `hotato[mcp]` PyPI extra and the
  `hotato-mcp` console script. `.github/workflows/release.yml` wires a
  `publish-mcp-registry` job (`mcp-publisher publish`) for the eventual
  first publish, hard-gated (`if: false`, plus workflow_dispatch-only) until
  an operator explicitly lifts it; nothing publishes today. New
  `docs/MCP.md`: copy-paste `mcpServers` configs for Claude Desktop, Cursor,
  and Codex CLI, and the `uvx --from "hotato[mcp]" hotato-mcp` footgun
  (`uvx hotato-mcp` alone fails) called out explicitly. `mcp_server.py`'s
  tool description now states the envelope schema URL and the correct
  zero-install command. `ci/github_action.yml` now says plainly it is
  hotato's own dev CI, not a copy-paste template (that footgun pointed a
  copying agent at the wrong workflow); the real drop-in stays
  `.github/workflows/hotato.yml`. `llms.txt` is reconciled to the real
  command surface: the commands shipped in 0.3.1 that it was missing
  (`capture`, `setup`, and the rest) plus `ingest` and `describe`, which are
  unreleased until this release (they were previously mislabelled here as
  shipped in 0.3.1), and both schema URLs and the MCP one-liner; new `llms-full.txt`
  concatenates README + every `docs/*.md` + `METHODOLOGY.md` + the envelope
  schema with file-boundary headers, built deterministically by
  `scripts/build_llms_full.py`. New `CITATION.cff`.

### Changed
- **`hotato demo` (and the hosted demo report) now score two real recorded
  calls**: the packaged battery's audio was two synthetic shaped-noise
  renders; it is now two real probe calls against a voice agent running a
  provider's default interruption settings -- a missed real interruption
  (`did_yield` false, 0.25 s talk-over) and a false stop on a soft
  backchannel (yielded 0.34 s in). The battery still fails on both funnel
  axes, fires the funnel pointer, and maps to the same two fix classes; the
  audio under each timeline is the exact scored WAV, and two runs remain
  byte-identical. Provenance, plainly: the recordings were made by the
  project itself, per its own recording runbook -- operator-recorded probe
  calls against a scripted fictional-pharmacy assistant. The only human
  voice is the recording operator's own; the agent side is a synthetic TTS
  voice, so no third-party speaker is present. No real names, numbers, or
  identifiers are spoken, and the clips are released under MIT with full
  per-scenario provenance and attestation carried in the scenario metadata.
  Demo copy reworded off "intentionally bad agent" to the honest real-call
  framing.

### Fixed
- **Atomic writes for downloads, sweep, and every `--out`**: `capture`'s
  recording download goes through a temp file + `os.replace`, so a local
  write failure after a successful fetch can never clobber a pre-existing
  file at `--out`; `sweep`'s HTML dashboard and every CLI `--out` writer use
  the same atomic helper, so an interrupted run cannot truncate a
  previously-good artifact.
- **JSON NaN safety**: every JSON emitter (cli, capture, ingest, patch,
  inspect) goes through `safe_json_dumps` with `allow_nan=False`, so
  NaN/Infinity can never ship as RFC-8259-invalid bare tokens; a non-finite
  value surfaces as the clean exit-2 usage error, and `patch` rejects
  non-finite `from`/`to`/bounds up front.
- **Loop-state validation**: `.hotato/loop-state.json` is validated on load
  (run/stage/discovery/planning/history field types, plus the stage-specific
  keys the renderer reads), so a well-typed but incomplete or hand-edited
  state file is a clean exit-2 error, never a KeyError.
- **WAV / onset / flag validation**: a `sample_rate=0` header or a malformed
  RIFF sub-chunk is a clean exit-2 error (never a ZeroDivisionError or a raw
  stdlib traceback); identical caller/agent channels are refused everywhere
  (comparing a channel against itself produced a confident, meaningless
  verdict and could mint a bogus permanent fixture); onset validity,
  out-of-range channels, and the global scan flags are validated up front so
  a bad flag is a top-level usage error, never a per-file skip that degrades
  into a false clean "found nothing"; a truncated recording is skipped with
  an honest reason; and one bad file never aborts an `analyze`/`loop` batch.
  (These landed across five overnight hardening rounds, each fix with a
  regression test; the vendored `_engine` stayed untouched and golden
  envelopes byte-identical throughout.)

### Security
- **Default-deny SSRF IP-block**: every vendor-response download URL in
  `capture` and `ingest` is restricted to http(s), and the resolved host is
  refused when it is loopback, private, link-local (cloud-metadata), or
  reserved -- re-checked on every redirect target. `HOTATO_ALLOW_PRIVATE_URLS`
  is the explicit opt-out. Credential headers are stripped on cross-host
  redirects, so a Bearer/Basic secret is never carried off-domain.
- **MCP input sandbox**: the MCP server's `voice_eval_run` stereo/caller/agent
  input paths are sandboxed (`HOTATO_MCP_INPUT_DIR`, else OS temp / CWD /
  bundled fixtures), mirroring the existing `report_path` sandbox.
- **Ingest sandbox, fail-closed**: `hotato ingest`'s local `recording_path`
  handling requires `HOTATO_INGEST_DIR` and fails closed, so a forged webhook
  payload cannot point the scanner at an arbitrary local file.
- **Scenario ids as safe path segments**: scenario ids are validated (no
  separators, no `..`, not absolute) before any path join in
  `run_suite`/`stackbench`/the bundled-audio fallback, plus a realpath
  containment check under `--audio`, so a crafted scenario pack can no longer
  read (or, via `--embed-audio`, exfiltrate) an arbitrary local WAV.

## [0.3.1] - 2026-07-07

### Fixed
- **Self-consistent source distribution**: the sdist now ships the small,
  non-audio test dependencies (scenario labels, manifests, the corpus
  validator, and the deterministic builders under `corpus/` and `examples/`),
  so an extracted sdist collects and runs the full test suite instead of
  hitting collection errors. The heavy real and rendered audio stays pruned;
  suite and class audio is regenerated deterministically by
  `tests/conftest.py` (seed = sha256(id)) when absent, and the tests that
  depend on genuinely-absent heavy real audio skip cleanly rather than error.
- **Honest fix-plan wording in `README.md` and `docs/WHY.md`**: reworded the
  claim that every failing event returns a fix that "names the exact setting to
  change in your stack." A `plan()` may correctly refuse to tune a single
  threshold, report insufficient coverage, or emit a checklist, so the copy now
  says a fix class is always returned and, when the failure maps cleanly to
  stack config, the setting family and direction to investigate.

### Added
- **`hotato scan` self-truncation candidate**: a new candidate kind,
  `agent_stop_no_caller`, surfaces the agent going from active to quiet with
  zero caller energy anywhere nearby, a drop nothing on the caller channel
  explains (not a barge-in, not a caller-driven handoff). Additive; existing
  candidate kinds and scoring are unchanged.
- **Response-gap percentiles and a latency SLA gate**: `hotato team` and
  `hotato export` now also report p95 (alongside the existing mean/median/p90)
  for talk-over and time-to-yield, and pool `response_gap_sec` (dead air
  before the agent speaks) into the same distribution shape. `--max-response-gap
  SECONDS` on both commands gates the pooled p95 and fails (exit 1) exactly
  when it is exceeded, the same pass/fail contract as `--max-talk-over` /
  `--max-time-to-yield`. A plain export's `envelope.json` stays byte-identical
  to before; the new numbers live only in the printed summary and the
  returned manifest.
- **Echo detector**: a deterministic cross-channel coherence signal
  (`signals.echo`: `coherence`, `lag_sec`, `echo_suspected`) on every scored
  event, flagging when the caller channel looks like a lag-shifted copy of
  the agent's own audio (leaked TTS), so a stop the agent makes because it
  heard itself is no longer indistinguishable from a clean yield. New `scan`
  candidate kind `echo_correlated_activity`; a loud WARNING in `hotato
  diagnose` and the single-run text output for every echo-suspected yield;
  opt-in `--echo-gate` on `hotato run` holds a bleed-induced yield out of the
  verdict (`scorable: false`) instead of counting it as a pass. Default run
  behavior, and every existing golden verdict, is unchanged; `--echo-gate` is
  off by default.
- **Resume/restart detector**: on events where the agent yielded, an additive
  `signals.resume` block (`resumed`, `resume_gap_sec`, `restart_suspected`)
  measures from the agent's own VAD track whether it came back after the
  yield, how fast, and whether the post-resume run is long enough to look
  like a restart-from-the-top rather than a short continuation. Whether the
  resumed words repeat the earlier ones is out of scope (a transcript
  question, not a timing one).
- **Four corpus scenario classes** under `corpus/classes/` (deterministic,
  additive, same seeded-render/`--check` contract as `corpus/suites/`):
  `mid-utterance-pause`, `backchannel-multilingual`, `noise-hold`, and
  `telephony-degraded`. Detail: `corpus/classes/README.md`.
- **"Is this even a turn-taking bug?" triage** in `docs/WHY.md` and `README.md`:
  names the failure modes commonly conflated with turn-taking bugs (STT
  hallucination, client-side audio buffering, LLM verbosity/tool-selection,
  safety false-refusal, wrong-language STT) and which tool class to reach for
  instead, alongside a plain statement of the flagship case Hotato covers
  (agent-talks-over-caller and false-stop-on-backchannel).
- **Report accessibility**: the generated HTML report carries aria-labels on
  its inline SVGs and audio elements, a `main` landmark, and measured titles,
  so the artifact is navigable by assistive tech. Visual output is unchanged.
- **Startup lazy-imports**: `importlib.resources`, the report renderer, and the
  numpy accelerator used only inside `scan` are deferred to first use, so plain
  CLI paths import less at startup. Behavior and output are unchanged.

All of the above are additive optional fields or opt-in flags: `did_yield`
and `passed` for every existing fixture are unchanged, and the vendored
`src/hotato/_engine` is untouched (verify with `python3 sync_engine.py
--check`).

## [0.3.0] - 2026-07-06

### Added
- **`hotato fixture create`**: the missing piece of the regression loop. One
  bad call moment (a recording, an `--onset`, and YOUR `--expect yield|hold`
  label) becomes a permanent fixture: `scenarios/<id>.json` in the existing
  scenario schema shape (with provenance: source file, original onset,
  `created_by`) plus a two-channel `audio/<id>.example.wav`. Clips around the
  event by default (`--pre` 2.0 s / `--post` 6.0 s) and re-bases the onset to
  the clip; `--no-clip` keeps the full recording. The created fixture is
  validated by scoring it through the suite runner immediately; a not-scorable
  input is refused with the honest reason (exit 2) and partial outputs are
  removed unless `--force`. Overwrite refused without `--force`. Round-trips
  through `hotato run --scenarios DIR --audio DIR` as written.
- **`hotato compare`**: the shareable before/after on one fixed moment. Scores
  both takes with the identical expectation, bounds, and reference config and
  reports the per-signal movement plus one machine-stable result word:
  `fixed`, `regressed`, `improved`, `worse`, `unchanged`, `still_pass`, or
  `not_scorable`. Marks come from real measurements only; a not-scorable side
  renders NOT SCORABLE (exit 2), never an invented verdict.
  `--before-onset` / `--after-onset` handle the moment shifting between takes;
  `--out report.html` writes the HTML report with the before take as the base
  comparison; `--fail-on-worse` exits 1 on `regressed`/`worse`.
- **`hotato scan`**: candidate extraction across a WHOLE recording, as timing
  facts only. Walks the two VAD activity tracks (windowed pass, so long files
  never load fully into memory) and surfaces `overlap_while_agent_talking`
  (with overlap length and whether/when the agent went silent),
  `agent_start_during_caller`, and `long_response_gap` candidates, sorted by
  salience with `--top` (default 20). No intent claims anywhere: the output
  header states that you decide the expected behavior and label the moment
  with `hotato fixture create`.
- **`hotato diagnose` / `hotato inspect` / `hotato plan`**: the guarded,
  read-only fix ladder (landed on main unreleased; first release here).
  Diagnose explains a finished run per failing event plus a battery-level
  decision; inspect reads and normalizes the live turn-taking config (GET or
  static parse, never executed); plan combines them into a
  `hotato.fixplan.v1` proposal: at most one bounded step on one setting,
  refusal on the threshold funnel, checklist on ambiguous evidence,
  `insufficient_coverage` without a passing opposite-risk fixture, and
  `production_apply` pinned to false.
- `hotato plan` also accepts the result JSON as a positional argument
  (`hotato plan result.json`), and every plan now carries `kind: "fix-plan"`,
  a `platform_mutation` block (`performed` always false), the measured
  `evidence`, `risks`, `next_commands` (including the `hotato compare`
  verification step), and not-scorable events as `input_issues`. New Twilio
  rule: `--stack twilio` never yields agent-config advice; the plan points at
  channel assignment and the upstream voice-agent stack.
- `docs/BAD-CALL-TO-CI.md`: the five-step loop from one bad call to a CI
  gate, with the label semantics up top and explicit use/do-not-use lists;
  runnable walkthrough in `examples/bad-call-to-ci/`.

### Changed
- Label wording, stated wherever the backchannel story is told (README,
  METHODOLOGY, WHY, and the `run`/`doctor`/`demo`/`fixture create` help):
  Hotato does not infer intent. You label the expected behavior for the
  event: yield means the agent should stop for the caller. hold means the
  agent should keep speaking through a backchannel/noise/acknowledgement.
  Hotato then measures whether the timing matched that label.
- README positioning line sharpened: Hotato turns bad voice-agent call
  moments into offline regression tests.
- Input wording unified: Hotato's main scorer requires separated caller and
  agent tracks; a single mixed mono call is not enough to attribute talk-over
  reliably.

## [0.2.3] - 2026-07-06

### Changed
- Documentation clarity pass across README, docs, and the PyPI page. Every
  turn-taking term (talk-over, barge-in, yield, backchannel, endpointing, not
  scorable) is defined in plain language at first use, and fix descriptions
  name the concrete setting to change. No code or scoring behaviour changed.

## [0.2.2] - 2026-07-06

### Fixed
- Report not-scorable rendering: the HTML and Markdown reports now render a not-scorable event with a NOT SCORABLE chip (or verdict cell) and its reason, never PASS or FAIL. The overall verdict is REGRESSION when any scorable event failed, else NOT SCORABLE when any input could not be judged, else ALL PASS. Not-scorable events are excluded from the failure clusters and from the time-to-yield and talk-over distributions, and are listed in their own "Not scorable inputs" section with id and reason (never under "Failures and fixes"). The summary counts line in both formats gains `not_scorable=N` when N > 0, mirroring the CLI text. Reports for fully-scorable inputs are byte-identical to 0.2.1.

### Changed
- License metadata modernization: `pyproject.toml` now uses the SPDX string form `license = "MIT"` with `license-files = ["LICENSE"]`, replacing the deprecated license table and the OSI license classifier. Built wheels carry `License-Expression: MIT` and builds emit no license deprecation warnings. Building from source now needs setuptools 77 or newer.
- Package docstring: the fix-pointer wording is softened. Discrimination failures are not solvable by one timing threshold; where your stack provides an interruption/backchannel classifier, use it, otherwise a learned engagement-control / addressee-detection layer is needed.

## [0.2.1] - 2026-07-06

### Fixed
- Sdist completeness: `MANIFEST.in` now ships everything the packaged tests need (corpus suites and manifest, `corpus/validate.py`, `sync_engine.py`, `scripts/pr_comment.py`, golden files, docs and assets, `SECURITY.md`, adapters, examples, ci, `.github`). Rendered suite audio is pruned from the sdist; it regenerates deterministically at test session start. `python -m pytest -q` now runs fully green inside an extracted sdist.
- Not-scorable rendering: the text output renders a not-scorable event as `[NOT SCORABLE] <id>` with its reason on the next line, never as `[FAIL]`, and the summary line gains `not_scorable=N` when any event is not scorable. Applies to `run`, `doctor`, `demo`, and `capture` text output. JSON envelopes are unchanged.
- Capture exit mapping: `hotato capture` now returns `process_exit_code(env)`, so a single capture whose every event is not scorable exits 2 (unusable input) instead of 0. When the process exit code differs from the envelope `exit_code`, the trailing text line prints `process_exit_code=N`; fully-scorable runs keep the exact `exit_code=N` line.
- Defensive Vapi recording parsing: the stereo URL chain also accepts `artifact.recording.stereoRecordingUrl` and `artifact.recording.stereo.url` (when `stereo` is an object), between the current field and the deprecated legacy fields.

### Changed
- Retell copy: the first-run guide and `capture --help` now show the real self-serve path, `hotato capture --stack retell --call-id <id>` with `RETELL_API_KEY`, replacing the stale "no self-serve stereo export yet" line.
- `SECURITY.md` states the network posture precisely: network access is used only when you explicitly run hosted-stack capture commands; core scoring, reports, exports, benchmarks, and demos run offline.
- `adapters/README.md` describes separated caller/agent channels as the required input for attributable overlap measurement, with speech activity still measured by the configured VAD thresholds and frame-inspectable via `--dump-frames`.

## [0.2.0] - 2026-07-06

### Added
- `hotato demo`: packaged intentionally bad agent battery with the visual report; `--fail` returns the real regression code.
- `python -m hotato` entry point.
- Retell capture is a real multichannel fetch (prefers the scrubbed recording); Vapi capture reads the current `artifact.recording.stereoUrl` with legacy fallbacks; Twilio capture requests dual-channel media explicitly and handles the mono case cleanly; `--allow-mono` degraded opt-in.
- Not scorable semantics: a silent caller or an agent that was not talking at onset is reported as not scorable with a plain reason, never as a normal verdict; single-recording runs exit 2.
- Report audio embedding (`--embed-audio`) with a size guard; the demo report on hotato.dev carries playable synthetic audio.
- Neural cross-check verified against the real Silero model; measured properties documented.
- `docs/ADAPTER-STATUS.md` with per-stack API basis and last-verified dates; `SECURITY.md`; `docs/WHY.md`.

### Changed
- `hotato run` default output is human-readable text; CI examples use `--format json` explicitly.
- README rebuilt demo-first with a real screenshot and the live CI badge.
- Fix-map knob text refreshed to current LiveKit `TurnHandlingOptions` and Pipecat user-turn strategy APIs.

## [0.1.0] - 2026-07-05

Initial release, published on PyPI. Offline, MIT, zero-install turn-taking eval
for voice agents. It scores one narrow thing well and is honest about the rest.

### Added

- **Core scorer**: one recording or the bundled battery, returning a single
  machine-readable envelope (`schema_version` "1"). Three objective timing signals
  measured from audio energy over aligned caller/agent channels: `did_yield`,
  `seconds_to_yield` (time to yield), `talk_over_sec`. Delegated unchanged to the
  vendored MIT `barge_scoring` engine (`src/hotato/_engine`), kept byte-identical
  to upstream by `sync_engine.py` (a CI drift gate).
- **Signal bus**: a namespaced `signals` block. `barge_in` mirrors the three
  originals byte-for-byte; `latency` adds `response_gap_sec` and
  `premature_start_sec`, pure endpointing timing on the same two VAD tracks.
- **Fix map**: every failing event carries a concrete fix. `fix_class: "config"`
  is a stack-specific knob with direction and honest trade-off.
  `fix_class: "engagement-control"` is a both-axes discrimination failure a single
  sensitivity dial cannot solve, pointed high-level and numbers-free at an
  engagement-control / addressee-detection layer. Plus a suite-level funnel that
  fires only when both axes fail at once.
- **CLI** (`hotato`): `run` (suite or single recording), `capture` and `setup` for
  stack adapters (Vapi, Twilio, LiveKit, Pipecat; Retell status stated honestly),
  `--format json`, non-zero exit for CI, and `--dump-frames` for per-frame,
  re-derivable evidence behind every number.
- **One-tool MCP server** (`hotato-mcp`): exposes exactly one tool,
  `voice_eval_run`, returning the identical JSON envelope, so an AI agent can run
  the eval mid-task.
- **Bundled synthetic battery**: an eight-scenario `barge-in` self-test
  (interruptions, backchannels, telephony 8 kHz, double-talk, echo bleed, rapid
  turn-taking) with exact rendered ground truth. A floor and a regression guard.
- **Honest scope, everywhere**: a `limits` block in every result and in the MCP
  schema. Reproducible timing against published thresholds, an explicit ceiling,
  energy-is-not-intent, and the best input (two-channel) stated up front. Offline
  by design.
- **JSON Schema** (`src/hotato/schema/envelope.v1.json`), `METHODOLOGY.md`,
  `CONTRIBUTING.md`, `docs/CORPUS-GOVERNANCE.md`, `llms.txt`, and a stdlib-only
  core with zero required third-party dependencies.
- **`hotato doctor`**: the 5-minute path in one command. Scores your recording
  (or the bundled self-test when none is given), writes the self-contained visual
  HTML report, and opens it in a browser (best effort; `--no-open` prints the
  path). A convenience wrapper over the existing scorer and report; nothing new
  is claimed.
- **`hotato report --format {html,md} [--base BASE.json]`**: a self-contained
  visual report (inline CSS and SVG, zero external requests, opens offline). Per
  event it draws a to-scale caller/agent timeline from the real frame data with
  the overlap shaded, onset and yield markers, measured talk-over, expected vs
  actual, and the exact `ScoreConfig` thresholds used. An analytics block
  computed from the same measurements: a time-to-yield distribution strip, a
  talk-over histogram, and failure clustering by fix class. A collapsible
  per-event frame inspector puts the full frame dump behind every timeline.
  `--base` renders per-scenario talk-over and time-to-yield deltas against a
  previous envelope. Ships print CSS, so print-to-PDF from any browser.
  `--format md` renders the same content as Markdown tables.
- **`hotato team DIR [--order mtime|name] [--html team.html] [--out agg.json]`**:
  aggregates a directory of run envelopes into one trend view. Reports runs,
  mean/median/p90 talk-over and time-to-yield pooled across all events, pass rate
  per run over time, the most common failure class, and a pass-rate trend line in
  the HTML page. Fewer than 2 runs is stated plainly, never padded into a trend.
- **`hotato export ... --out DIR`**: research-grade output. Writes `events.csv`
  (one row per event, every measured signal plus verdict), `frames.csv` (one row
  per VAD frame, the evidence behind every number), and `envelope.json`. Column
  meanings are documented in comment lines at the top of each CSV. Stdlib only,
  offline.
- **`hotato benchmark`** (`src/hotato/stackbench.py`): comparable stack runs.
  Scores your captured battery per stack and compares result files side by side
  with `hotato benchmark compare`. Measurements only; no vendor numbers, no
  leaderboard, no ranking.
- **Pytest plugin**, auto-registered on install (`pytest11` entry point): a
  `hotato_score` fixture that scores a recording or suite inside any test, and an
  opt-in `--hotato-suite` session gate that runs the battery after the tests and
  fails the session on a regression. `--hotato-suite-scenarios` and
  `--hotato-suite-audio` point the gate at your own labelled sets.
- **MCP: optional `report_path`** on the one `voice_eval_run` tool. When set, the
  server also writes the self-contained HTML report there and the returned
  envelope carries `report_path` (absolute). Additive; the envelope is otherwise
  unchanged.
- **Tiered corpus suites** (`corpus/suites/`): `silver`, `silver-defects`,
  `gold`, and `gold-defects`, 112 scenarios total, all synthetic shaped noise
  rendered deterministically from each scenario's own labelled timings (seed
  `sha256(id)`). The defect suites exist to fail: every scenario in them fails on
  its labelled axis, so `exit_code 1` there proves the scorer catches what it
  claims to catch. Built and byte-verified by `corpus/suites/build_suites.py`
  (`--check` regenerates to a temp dir and byte-compares); `manifest.json` is the
  machine-readable inventory. Run any suite via
  `hotato run --suite barge-in --scenarios DIR --audio DIR`.
- **GitHub PR check** (`.github/workflows/hotato.yml` plus
  `scripts/pr_comment.py`): scores the suite on every pull request, posts one
  sticky comment (results table, pass/fail line, regressions with deltas against
  the base branch when scorable), and fails the job on a regression. See
  `docs/CI.md`.
- **Documented distribution statistics** (`src/hotato/_stats.py`): mean, median,
  and p90 used by `report` and `team` have one stdlib implementation with the
  definition in the docstring. p90 is linear interpolation between closest ranks.
  Rates are reported as fractions, never a percentage. Empty input returns null,
  never a fabricated number.
- **New docs pages**: `docs/REPORTS.md` (doctor, report, team, export),
  `docs/PYTEST.md` (the plugin and the gate), `docs/SUITES.md` (the tiered
  corpus suites), and `docs/SUBMITTING.md` (the corpus submission walkthrough).
- **Reproducible benchmark harness** (`src/hotato/benchmark.py`, run it with
  `python3 -m hotato.benchmark`). Takes labelled dual-channel recordings and
  reports per-signal measurement error in milliseconds (onset, time-to-yield,
  response-gap) plus a `did_yield` confusion matrix against the
  should_yield / should_not_yield labels. It keeps the missed-yield and
  false-yield cells separate so neither hides behind an average. Runs on the
  bundled and example synthetic fixtures out of the box and writes a JSON and
  markdown report. It is a standalone measurement tool you point at fixtures, not
  a CLI command.
- **`docs/BENCHMARK.md`**: the methodology. What is measured, why
  milliseconds-and-a-matrix is the honest metric, how to reproduce it, and the
  bring-your-own-labelled-recordings path to real validity.
- **Corpus pipeline** (`corpus/`): the tooling for the real-recording corpus. A
  labelling schema (`corpus/label.schema.json`) that extends the scenario shape
  with provenance, consent, PII, and attestation fields. A standalone stdlib
  validator (`corpus/validate.py`) that checks a `(recording, label)` pair
  conforms (two-channel WAV, required fields, timings in range, attestation). And
  a `corpus/README.md` that defers to `docs/CORPUS-GOVERNANCE.md` for consent and
  PII. Ships one clearly-labelled synthetic example and no real audio.
- **Community scaffolding**: this changelog, GitHub issue templates (bug report
  and feature request), and a pull-request template whose checklist carries the
  project's honesty rules (no accuracy %, no fabricated numbers, energy is not
  intent, do not edit the vendored `_engine`, keep the drift gate green).

### Note

- The bundled and example fixtures remain synthetic and labelled as such. This
  release ships no fabricated recording, real-model number, benchmark result,
  leaderboard, or star count. The synthetic fixtures are a floor and a regression
  guard; real validity comes from contributed, consented, human-labelled calls.

[Unreleased]: https://github.com/attenlabs/hotato/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/attenlabs/hotato/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/attenlabs/hotato/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/attenlabs/hotato/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/attenlabs/hotato/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/attenlabs/hotato/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/attenlabs/hotato/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/attenlabs/hotato/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/attenlabs/hotato/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/attenlabs/hotato/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/attenlabs/hotato/releases/tag/v0.1.0
