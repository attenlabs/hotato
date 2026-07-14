/**
 * Thin typed client over the hotato CLI.
 *
 * Every function spawns `hotato <command> --format json`, waits for it, and
 * returns the CLI's JSON parsed into the interfaces in `types.ts`. All
 * scoring, reduction, and verification logic runs inside the CLI; this
 * module only launches it and types what it says.
 */

import { execFile } from "node:child_process";
import { HotatoError, RefusalError, type CliOutcome } from "./errors.js";
import type {
  ContractVerifyReport,
  ContractVerifyResult,
  CounterexampleCompileResult,
  CounterexampleVerifyResult,
  ErrorEnvelope,
  HotatoStack,
  PredicateResult,
  SuiteResult,
} from "./types.js";

const DEFAULT_TIMEOUT_MS = 300_000;
const MAX_STDOUT_BYTES = 64 * 1024 * 1024;

/** Options shared by every call: how to launch the CLI and where. */
export interface HotatoOptions {
  /**
   * The hotato launcher. A string is split on whitespace, so "hotato"
   * (resolved via PATH, the default), "/opt/venv/bin/hotato", and
   * "uvx hotato" all work; pass an array (e.g. ["uvx", "hotato"]) when a
   * segment contains spaces.
   */
  hotatoBin?: string | string[];
  /** Working directory for the CLI process. */
  cwd?: string;
  /** Extra environment variables, merged over process.env. */
  env?: Record<string, string>;
  /** Kill the CLI after this long. Default 300000 (5 minutes). */
  timeoutMs?: number;
}

function launcherArgv(options: HotatoOptions): string[] {
  const bin = options.hotatoBin ?? "hotato";
  const argv = Array.isArray(bin)
    ? [...bin]
    : bin.trim().split(/\s+/).filter((part) => part.length > 0);
  if (argv.length === 0) {
    throw new TypeError("hotatoBin must name a command");
  }
  return argv;
}

/**
 * Run one hotato invocation and capture its outcome. Resolves for every
 * exit code; rejects with `HotatoError` only when the process could not
 * run at all (missing binary, signal kill, timeout).
 */
export function execHotato(
  args: readonly string[],
  options: HotatoOptions = {},
): Promise<CliOutcome> {
  const argv = [...launcherArgv(options), ...args];
  const [file, ...rest] = argv;
  return new Promise((resolve, reject) => {
    execFile(
      file as string,
      rest,
      {
        cwd: options.cwd,
        env: options.env ? { ...process.env, ...options.env } : process.env,
        timeout: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
        maxBuffer: MAX_STDOUT_BYTES,
        windowsHide: true,
      },
      (error, stdout, stderr) => {
        if (error === null) {
          resolve({ command: argv, exitCode: 0, stdout, stderr });
          return;
        }
        const failure = error as NodeJS.ErrnoException & {
          signal?: NodeJS.Signals | null;
        };
        if (typeof failure.code === "number") {
          resolve({ command: argv, exitCode: failure.code, stdout, stderr });
          return;
        }
        const detail =
          failure.signal != null
            ? `terminated by ${failure.signal}`
            : failure.code === "ENOENT"
              ? `command not found: ${file}`
              : failure.message;
        reject(
          new HotatoError(
            `hotato did not run (${detail})`,
            { command: argv, exitCode: null, stdout, stderr },
            { cause: error },
          ),
        );
      },
    );
  });
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function asErrorEnvelope(doc: unknown): ErrorEnvelope | undefined {
  if (
    typeof doc === "object" &&
    doc !== null &&
    (doc as { ok?: unknown }).ok === false &&
    typeof (doc as { error_code?: unknown }).error_code === "string" &&
    typeof (doc as { message?: unknown }).message === "string"
  ) {
    return doc as ErrorEnvelope;
  }
  return undefined;
}

/**
 * Decode one CLI outcome under hotato's exit-code contract. Pure: it never
 * spawns anything, so captured CLI output (a CI log, a fixture file) decodes
 * the same way a live run does.
 *
 * - exit code in `resultExitCodes` (default 0 and 1): parse stdout as the
 *   command's JSON document and return it.
 * - exit code 2 with an `ErrorEnvelope` on stdout: throw `RefusalError`.
 * - anything else: throw `HotatoError`.
 */
export function decodeCliJson<T>(
  outcome: CliOutcome,
  resultExitCodes: readonly number[] = [0, 1],
): T {
  if (outcome.exitCode !== null && resultExitCodes.includes(outcome.exitCode)) {
    const doc = parseJson(outcome.stdout);
    if (doc !== undefined) {
      return doc as T;
    }
    throw new HotatoError(
      `hotato exited ${outcome.exitCode} without JSON on stdout; pass --format json (stdout: ${outcome.stdout.trim().slice(0, 200) || "empty"})`,
      outcome,
    );
  }
  if (outcome.exitCode === 2) {
    const refusal = asErrorEnvelope(parseJson(outcome.stdout));
    if (refusal !== undefined) {
      throw new RefusalError(refusal, outcome);
    }
  }
  const summary =
    outcome.stderr.trim().slice(0, 400) ||
    outcome.stdout.trim().slice(0, 400) ||
    "no output";
  throw new HotatoError(
    `hotato exited ${outcome.exitCode ?? "on a signal"}: ${summary}`,
    outcome,
  );
}

/* ------------------------------------------------------------------ *
 * Suite runs                                                          *
 * ------------------------------------------------------------------ */

export interface RunSuiteOptions extends HotatoOptions {
  /** Labelled battery name. The CLI default is "barge-in". */
  suite?: string;
  /** Directory of scenario JSON labels. Default: the bundled battery. */
  scenarios?: string;
  /** Directory of scenario audio. Default: the bundled fixtures. */
  audio?: string;
  /** Voice stack the recordings came from; tunes the fix-knob names. */
  stack?: HotatoStack;
}

/**
 * `hotato run --suite [NAME] [--scenarios DIR --audio DIR] --format json`.
 *
 * Resolves with the full suite document on exit 0 and on exit 1: a failing
 * battery is a scored result (`summary.regression` true, `exit_code` 1),
 * carrying per-event verdicts and the fix map. Exit 2 (unusable input)
 * rejects with `RefusalError`.
 */
export async function runSuite(
  options: RunSuiteOptions = {},
): Promise<SuiteResult> {
  const args = ["run", "--suite"];
  if (options.suite !== undefined) {
    args.push(options.suite);
  }
  if (options.scenarios !== undefined) {
    args.push("--scenarios", options.scenarios);
  }
  if (options.audio !== undefined) {
    args.push("--audio", options.audio);
  }
  if (options.stack !== undefined) {
    args.push("--stack", options.stack);
  }
  args.push("--format", "json");
  return decodeCliJson<SuiteResult>(await execHotato(args, options));
}

/* ------------------------------------------------------------------ *
 * Failure contracts                                                   *
 * ------------------------------------------------------------------ */

export interface VerifyContractsOptions extends HotatoOptions {
  /**
   * Transcript JSON file (hotato assert's --transcript shape) used as
   * context for every contract's embedded assertions block, if any.
   */
  transcript?: string;
}

/**
 * `hotato contract verify DIR --format json`: re-score every contract's
 * bundled audio against the policy recorded in its own contract.json.
 *
 * Exit 1 (at least one contract regressed) is a verification result, so it
 * resolves: `passed` false, `exitCode` 1, and the CLI's batch document on
 * `report`. Exit 2 (no contracts found, corrupt bundle) rejects with
 * `RefusalError`.
 */
export async function verifyContracts(
  dir: string,
  options: VerifyContractsOptions = {},
): Promise<ContractVerifyResult> {
  const args = ["contract", "verify", dir, "--format", "json"];
  if (options.transcript !== undefined) {
    args.push("--transcript", options.transcript);
  }
  const outcome = await execHotato(args, options);
  const report = decodeCliJson<ContractVerifyReport>(outcome);
  return {
    passed: outcome.exitCode === 0,
    exitCode: outcome.exitCode as 0 | 1,
    report,
  };
}

/* ------------------------------------------------------------------ *
 * Counterexample capsules                                             *
 * ------------------------------------------------------------------ */

export interface CompileCounterexampleOptions extends HotatoOptions {
  /** Deterministic hotato.scenario JSON/YAML-subset file. */
  scenario: string;
  /** Conversation-test file containing the target deterministic assertion. */
  test: string;
  /** Unique supported assertion id in assertions.deterministic. */
  target: string;
  /** New .hotato-repro directory; must not exist yet. */
  out: string;
  /** Root both input files must resolve inside. Default: their common parent. */
  workspace?: string;
  /** Maximum uncached candidate evaluations. CLI default 512. */
  budget?: number;
  /** Scripted replay seed. CLI default: scenario.seed or 0. */
  seed?: number;
}

/**
 * `hotato counterexample compile --scenario F --test F --target ID --out DIR`.
 *
 * Resolves on exit 0 (`minimality` "one_minimal") and on exit 1 (the exact
 * failure is preserved in a capsule and the candidate budget ended first:
 * `minimality` "budget_exhausted"). Refusals (unsupported target, workspace
 * escape, existing output) reject with `RefusalError`.
 */
export async function compileCounterexample(
  options: CompileCounterexampleOptions,
): Promise<CounterexampleCompileResult> {
  const args = [
    "counterexample",
    "compile",
    "--scenario",
    options.scenario,
    "--test",
    options.test,
    "--target",
    options.target,
    "--out",
    options.out,
  ];
  if (options.workspace !== undefined) {
    args.push("--workspace", options.workspace);
  }
  if (options.budget !== undefined) {
    args.push("--budget", String(options.budget));
  }
  if (options.seed !== undefined) {
    args.push("--seed", String(options.seed));
  }
  args.push("--format", "json");
  return decodeCliJson<CounterexampleCompileResult>(
    await execHotato(args, options),
  );
}

/**
 * `hotato counterexample verify DIR --format json`: independently verify a
 * capsule's integrity, exact failure replay, and claimed minimality.
 * Resolves on exit 0 and exit 1 (capsule intact, target no longer
 * reproduces); malformed or tampered capsules reject with `RefusalError`.
 */
export async function verifyCounterexample(
  dir: string,
  options: HotatoOptions = {},
): Promise<CounterexampleVerifyResult> {
  return decodeCliJson<CounterexampleVerifyResult>(
    await execHotato(["counterexample", "verify", dir, "--format", "json"], options),
  );
}

/**
 * `hotato counterexample predicate DIR`: the git-bisect predicate.
 * Exit 1 means the target failure reproduces under the current evaluator
 * (bisect bad) and resolves `{ failurePresent: true, exitCode: 1 }`;
 * exit 0 means it is absent (bisect good). Exit 125 (untestable, bisect
 * skip) throws `HotatoError` carrying the exit code.
 */
export async function predicate(
  dir: string,
  options: HotatoOptions = {},
): Promise<PredicateResult> {
  const outcome = await execHotato(["counterexample", "predicate", dir], options);
  if (outcome.exitCode === 0) {
    return { failurePresent: false, exitCode: 0 };
  }
  if (outcome.exitCode === 1) {
    return { failurePresent: true, exitCode: 1 };
  }
  if (outcome.exitCode === 125) {
    throw new HotatoError(
      "capsule or evaluator state is untestable for this revision (git-bisect skip, exit 125)",
      outcome,
    );
  }
  throw new HotatoError(
    `hotato counterexample predicate exited ${outcome.exitCode ?? "on a signal"}`,
    outcome,
  );
}
