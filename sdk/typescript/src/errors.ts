/**
 * Typed errors for the hotato CLI client.
 *
 * The CLI's exit-code contract drives the split:
 *   - 0 / 1 on the commands this SDK wraps are results and resolve.
 *   - 2 is a refusal: the CLI prints an `ErrorEnvelope` JSON document and
 *     declines to act. That becomes `RefusalError`.
 *   - Everything else (missing binary, signal kill, unparseable stdout,
 *     undocumented exit code) becomes `HotatoError`.
 */

import type { ErrorEnvelope } from "./types.js";

/** One finished (or failed-to-start) CLI invocation, as the SDK saw it. */
export interface CliOutcome {
  /** Full argv, launcher included, e.g. ["uvx", "hotato", "run", ...]. */
  command: readonly string[];
  /** Process exit code; null when the process was killed or did not start. */
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

/**
 * A genuinely unexpected CLI failure: the binary was missing, the process
 * died on a signal or timeout, stdout was not the promised JSON, or the
 * exit code sits outside the command's documented result set. Carries the
 * full invocation so a caller (or an agent reading a CI log) can replay it.
 */
export class HotatoError extends Error {
  readonly command: readonly string[];
  readonly exitCode: number | null;
  readonly stdout: string;
  readonly stderr: string;

  constructor(message: string, outcome: CliOutcome, options?: ErrorOptions) {
    super(message, options);
    this.name = "HotatoError";
    this.command = outcome.command;
    this.exitCode = outcome.exitCode;
    this.stdout = outcome.stdout;
    this.stderr = outcome.stderr;
  }
}

/**
 * The CLI refused the request (exit code 2) and said why in JSON:
 * a malformed input file, an unresolvable path, an unsupported target.
 * `refusal` is the CLI's parsed `ErrorEnvelope` verbatim.
 */
export class RefusalError extends HotatoError {
  readonly refusal: ErrorEnvelope;
  /** Convenience mirror of `refusal.error_code`. */
  readonly errorCode: string;

  constructor(refusal: ErrorEnvelope, outcome: CliOutcome) {
    super(`hotato refused (${refusal.error_code}): ${refusal.message}`, outcome);
    this.name = "RefusalError";
    this.refusal = refusal;
    this.errorCode = refusal.error_code;
  }
}
