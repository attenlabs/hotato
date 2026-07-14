/**
 * Typed interfaces for the hotato CLI JSON contract.
 *
 * Every interface here mirrors, field for field, the JSON a specific
 * `hotato <command> --format json` invocation emits. The shapes were written
 * by hand from output captured against hotato 1.6.2 running the bundled demo
 * fixtures; the captured documents live in `test/fixtures/` and pin these
 * types in the test suite. Field names keep the CLI's snake_case so a value
 * seen in a terminal, a CI log, and this SDK reads identically.
 */

/* ------------------------------------------------------------------ *
 * Shared                                                              *
 * ------------------------------------------------------------------ */

/** Voice stacks the CLI accepts for `--stack`. */
export type HotatoStack =
  | "generic"
  | "vapi"
  | "twilio"
  | "livekit"
  | "pipecat"
  | "retell";

/**
 * The JSON document hotato prints on stdout for a refusal (exit code 2)
 * in `--format json` mode.
 */
export interface ErrorEnvelope {
  tool: "hotato";
  schema_version: string;
  ok: false;
  /** Machine-readable refusal class, e.g. "usage_error", "file_not_found". */
  error_code: string;
  /** Human-readable reason for the refusal. */
  message: string;
  exit_code: number;
}

/* ------------------------------------------------------------------ *
 * hotato run --suite --format json                                    *
 * ------------------------------------------------------------------ */

/** The scoring engine block on every suite envelope. */
export interface EngineInfo {
  name: string;
  version: string;
  upstream: string;
}

/** The self-describing measurement-scope block on every suite envelope. */
export interface SuiteLimits {
  method: string;
  accuracy_claim: null;
  reproducible: string;
  ceiling: string;
  best_input: string;
  does_not_do: string[];
  scope: string;
  offline: string;
}

export interface SuiteSummary {
  events: number;
  passed: number;
  failed: number;
  regression: boolean;
}

/** Pass/fail verdict for one scenario event. */
export interface SuiteVerdict {
  passed: boolean;
  did_yield: boolean;
  seconds_to_yield: number | null;
  talk_over_sec: number;
  reasons: string[];
}

/** Frame-level measurement detail for one scenario event. */
export interface SuiteMeasurements {
  caller_onset_sec: number;
  agent_talking_at_onset: boolean;
  hop_sec: number;
  notes: string;
  onset_requested_sec: number | null;
  onset_frame_index: number;
  onset_effective_sec: number;
  yield_frame_index: number | null;
  decision_margin_sec: number | null;
  decision_margin_hops: number | null;
  boundary_sensitive: boolean;
}

export interface BargeInSignal {
  did_yield: boolean;
  time_to_yield_sec: number | null;
  talk_over_sec: number;
}

export interface LatencySignal {
  response_gap_sec: number | null;
  premature_start_sec: number | null;
}

export interface EchoSignal {
  coherence: number | null;
  lag_sec: number | null;
  echo_suspected: boolean;
}

export interface SuiteSignals {
  barge_in: BargeInSignal;
  latency: LatencySignal;
  echo: EchoSignal;
}

/** A concrete config parameter a fix suggestion points at. */
export interface FixKnob {
  stack: string;
  parameter: string;
  direction: string;
  trade_off: string;
}

/**
 * Where a failure needs a capability layer rather than a config knob,
 * the fix carries this pointer instead of a `knob`.
 */
export interface FixPointer {
  layer: string;
  what: string;
  honest_scope: string;
}

/** Fix suggestion attached to a failing event (null on a passing event). */
export interface EventFix {
  fix_class: string;
  title: string;
  detail: string;
  knob: FixKnob | null;
  pointer: FixPointer | null;
}

/** One row of the suite-level `fix_map` (one entry per failing event). */
export interface FixMapEntry extends EventFix {
  event_id: string;
  scenario_id: string;
}

/** Battery-level diagnosis emitted when the failure pattern crosses events. */
export interface SuiteFunnel {
  reason: string;
  pointer: FixPointer;
}

export interface AudioProvenanceSide {
  role: string;
  path: string;
  sha256: string;
  pcm_sha256: string;
  sample_rate: number;
  num_samples: number;
  duration_sec: number;
}

export interface AudioProvenance {
  schema_version: string;
  sha256: string;
  sides: AudioProvenanceSide[];
}

/** One scored scenario event in a suite run. */
export interface SuiteEvent {
  event_id: string;
  scenario_id: string;
  title: string;
  category: "should_yield" | "should_not_yield";
  expected_yield: boolean;
  verdict: SuiteVerdict;
  measurements: SuiteMeasurements;
  signals: SuiteSignals;
  fix: EventFix | null;
  audio_provenance: AudioProvenance;
  expected_yield_explicit: boolean;
}

/**
 * `hotato run --suite [NAME] --scenarios DIR --audio DIR --format json`.
 *
 * Exit code 0 (every event passed) and exit code 1 (at least one event
 * failed, `summary.regression` true) both emit this document; the CLI
 * records which inside it as `exit_code`.
 */
export interface SuiteResult {
  tool: "hotato";
  schema_version: string;
  mode: "suite";
  stack: string;
  offline: boolean;
  engine: EngineInfo;
  limits: SuiteLimits;
  summary: SuiteSummary;
  events: SuiteEvent[];
  fix_map: FixMapEntry[];
  funnel: SuiteFunnel | null;
  exit_code: number;
  suite: string;
}

/* ------------------------------------------------------------------ *
 * hotato contract verify DIR --format json                            *
 * ------------------------------------------------------------------ */

export interface ContractMeasurement {
  did_yield: boolean;
  seconds_to_yield: number | null;
  talk_over_sec: number;
}

/** Re-verification result for one `<id>.hotato` contract bundle. */
export interface ContractResult {
  id: string;
  dir: string;
  expect: "yield" | "hold";
  passed: boolean;
  scorable: boolean;
  verdict_eligible: boolean;
  verdict_ineligible_reason: string | null;
  not_scorable_reason: string | null;
  measurement: ContractMeasurement;
  authenticity: string;
  authenticated: boolean;
  authenticity_reason: string;
  /**
   * Embedded-assertions block: null when the contract carries none, and the
   * per-assertion result object when it does (its shape follows the
   * contract's own `assertions` block; see docs/CONTRACTS.md).
   */
  assertions: unknown;
}

/**
 * The full batch document `hotato contract verify DIR --format json`
 * prints on stdout. `summary.passed` / `summary.failed` are counts.
 */
export interface ContractVerifyReport {
  tool: "hotato";
  kind: "contract-verify";
  schema_version: string;
  offline: boolean;
  dir: string;
  count: number;
  results: ContractResult[];
  summary: { passed: number; failed: number };
  tampered: number;
  refused: number;
  assertions_failed: number;
  exit_code: number;
}

/**
 * What `verifyContracts()` resolves with. Exit code 1 (a contract regressed)
 * is a verification result, so it resolves rather than throws: `passed` is
 * `exitCode === 0`, and `report` is the CLI's JSON document verbatim.
 */
export interface ContractVerifyResult {
  passed: boolean;
  exitCode: 0 | 1;
  report: ContractVerifyReport;
}

/* ------------------------------------------------------------------ *
 * hotato counterexample ... --format json                             *
 * ------------------------------------------------------------------ */

/**
 * Structured identity of one deterministic failure. `code` is always
 * present; the remaining detail fields vary by assertion kind (a `state`
 * assertion carries `field`, for example).
 */
export interface FailureAtom {
  code: string;
  [detail: string]: unknown;
}

/** The exact deterministic assertion failure a capsule preserves. */
export interface CounterexampleTarget {
  test_id: string;
  assertion_digest: string;
  assertion_id: string;
  kind: string;
  dimension: string;
  authority: "deterministic";
  required_status: "FAIL";
  failure_atom: FailureAtom;
  source_failure_atoms: FailureAtom[];
  fingerprint: string;
}

/** Size of a fixture at one end of the reduction. */
export interface ReductionCounts {
  bytes: number;
  turns: number;
  tools: number;
  state_leaves: number;
  transcript_segments: number;
  trace_spans: number;
}

/** How the reducer got from the source fixture to the final capsule. */
export interface CounterexampleReduction {
  algorithm: string;
  reducer_set: string;
  initial: ReductionCounts;
  final: ReductionCounts;
  attempts: number;
  candidate_evaluations: number;
  qualification_evaluations: number;
  total_evaluations: number;
  accepted: number;
  cache_hits: number;
  budget: number;
  termination: string;
}

/**
 * Minimality status of a capsule: `one_minimal` when the final deletion
 * pass completed (exit 0), `budget_exhausted` when the candidate budget
 * ended first (exit 1; the failure is still preserved).
 */
export type CounterexampleMinimality = "one_minimal" | "budget_exhausted";

/** `hotato counterexample compile ... --format json` (exit 0 or 1). */
export interface CounterexampleCompileResult {
  kind: "counterexample-compile";
  exit_code: number;
  counterexample_id: string;
  target: CounterexampleTarget;
  minimality: CounterexampleMinimality;
  reduction: CounterexampleReduction;
  /** Path of the compiled `.hotato-repro` capsule directory. */
  output: string;
  /** Path of the capsule's `reproduce.sh`. */
  reproduce: string;
  /** Path of the capsule's `predicate.sh`. */
  predicate: string;
}

/** `hotato counterexample verify DIR --format json` (exit 0 or 1). */
export interface CounterexampleVerifyResult {
  kind: "counterexample-verify";
  exit_code: number;
  ok: boolean;
  status: string;
  counterexample_id: string;
  failure_fingerprint: string;
  minimality: CounterexampleMinimality;
  single_unit_checks: number;
  source_replays: number;
  final_replays: number;
  accepted_steps_replayed: number;
  evaluator_match: boolean;
  output: string;
}

/**
 * `hotato counterexample predicate DIR` speaks pure git-bisect exit codes
 * and prints nothing: 1 = the target failure reproduces (bisect bad),
 * 0 = it is absent (bisect good). `predicate()` maps those to this result;
 * exit 125 (untestable, bisect skip) throws `HotatoError` instead.
 */
export interface PredicateResult {
  failurePresent: boolean;
  exitCode: 0 | 1;
}
