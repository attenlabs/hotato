/**
 * @hotato/sdk: typed TypeScript client for the hotato CLI JSON contract.
 *
 * The CLI's `--format json` output is the contract; these functions spawn
 * `hotato`, decode its exit code, and return that JSON typed. Start with
 * `runSuite`, `verifyContracts`, `compileCounterexample`,
 * `verifyCounterexample`, and `predicate`.
 */

export {
  compileCounterexample,
  decodeCliJson,
  execHotato,
  predicate,
  runSuite,
  verifyContracts,
  verifyCounterexample,
  type CompileCounterexampleOptions,
  type HotatoOptions,
  type RunSuiteOptions,
  type VerifyContractsOptions,
} from "./client.js";

export { HotatoError, RefusalError, type CliOutcome } from "./errors.js";

export type {
  AudioProvenance,
  AudioProvenanceSide,
  BargeInSignal,
  ContractMeasurement,
  ContractResult,
  ContractVerifyReport,
  ContractVerifyResult,
  CounterexampleCompileResult,
  CounterexampleMinimality,
  CounterexampleReduction,
  CounterexampleTarget,
  CounterexampleVerifyResult,
  EchoSignal,
  EngineInfo,
  ErrorEnvelope,
  EventFix,
  FailureAtom,
  FixKnob,
  FixMapEntry,
  FixPointer,
  HotatoStack,
  LatencySignal,
  PredicateResult,
  ReductionCounts,
  SuiteEvent,
  SuiteFunnel,
  SuiteLimits,
  SuiteMeasurements,
  SuiteResult,
  SuiteSignals,
  SuiteSummary,
  SuiteVerdict,
} from "./types.js";
