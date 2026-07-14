// Pure decode tests: captured hotato 1.6.2 JSON (test/fixtures/) through
// decodeCliJson, with no CLI spawned. This half of the suite runs on any
// machine with Node alone; cli.test.mjs covers the same contract live.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { decodeCliJson, HotatoError, RefusalError } from "../dist/index.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = (name) => readFileSync(path.join(here, "fixtures", name), "utf8");

/** Run fn, assert it throws an instance of Type, and hand back the error. */
const capture = (fn, Type) => {
  try {
    fn();
  } catch (err) {
    assert.ok(
      err instanceof Type,
      `expected ${Type.name}, got ${err?.constructor?.name}: ${err?.message}`,
    );
    return err;
  }
  assert.fail(`expected ${Type.name} to be thrown`);
};
const outcome = (name, exitCode) => ({
  command: ["hotato", "(fixture)", name],
  exitCode,
  stdout: name ? fixture(name) : "",
  stderr: "",
});

test("suite fixture, exit 0: passing battery decodes with typed summary", () => {
  /** @type {import("../dist/index.js").SuiteResult} */
  const suite = decodeCliJson(outcome("suite-passing.json", 0));
  assert.equal(suite.tool, "hotato");
  assert.equal(suite.mode, "suite");
  assert.equal(suite.suite, "barge-in");
  assert.equal(suite.exit_code, 0);
  assert.equal(suite.summary.regression, false);
  assert.equal(suite.summary.events, 8);
  assert.equal(suite.summary.passed, 8);
  assert.equal(suite.events.length, 8);
  assert.deepEqual(suite.fix_map, []);
  assert.equal(suite.funnel, null);
  assert.equal(suite.engine.name, "barge_scoring (vendored, MIT)");
  for (const event of suite.events) {
    assert.equal(typeof event.verdict.passed, "boolean");
    assert.equal(typeof event.signals.echo.echo_suspected, "boolean");
    assert.ok(["should_yield", "should_not_yield"].includes(event.category));
  }
});

test("suite fixture, exit 1: failing battery is decoded, never thrown", () => {
  /** @type {import("../dist/index.js").SuiteResult} */
  const suite = decodeCliJson(outcome("suite-failing.json", 1));
  assert.equal(suite.exit_code, 1);
  assert.equal(suite.summary.regression, true);
  assert.equal(suite.summary.failed, 2);
  const first = suite.events[0];
  assert.equal(first.event_id, "fd-01-missed-interruption");
  assert.equal(first.verdict.passed, false);
  assert.equal(first.verdict.did_yield, false);
  assert.equal(first.verdict.seconds_to_yield, null);
  assert.equal(first.verdict.talk_over_sec, 0.25);
  assert.ok(first.verdict.reasons.length > 0);
  assert.equal(suite.fix_map.length, 2);
  assert.equal(typeof suite.fix_map[0].event_id, "string");
  assert.ok(suite.funnel);
  assert.equal(typeof suite.funnel.pointer.layer, "string");
});

test("contract-verify fixture, exit 1: per-contract results decode", () => {
  /** @type {import("../dist/index.js").ContractVerifyReport} */
  const report = decodeCliJson(outcome("contract-verify-failing.json", 1));
  assert.equal(report.kind, "contract-verify");
  assert.equal(report.exit_code, 1);
  assert.equal(report.count, 1);
  assert.equal(report.summary.passed, 0);
  assert.equal(report.summary.failed, 1);
  const result = report.results[0];
  assert.equal(result.id, "demo-missed-interruption");
  assert.equal(result.expect, "yield");
  assert.equal(result.passed, false);
  assert.equal(result.scorable, true);
  assert.equal(result.measurement.did_yield, false);
  assert.equal(result.measurement.talk_over_sec, 0.25);
  assert.equal(result.assertions, null);
});

test("counterexample-compile fixture, exit 0: one_minimal capsule decodes", () => {
  /** @type {import("../dist/index.js").CounterexampleCompileResult} */
  const compiled = decodeCliJson(outcome("counterexample-compile.json", 0));
  assert.equal(compiled.kind, "counterexample-compile");
  assert.equal(compiled.exit_code, 0);
  assert.equal(compiled.minimality, "one_minimal");
  assert.equal(compiled.target.assertion_id, "refund-posted");
  assert.equal(compiled.target.authority, "deterministic");
  assert.equal(compiled.target.required_status, "FAIL");
  assert.equal(compiled.target.failure_atom.code, "state-field-value-mismatch");
  assert.ok(compiled.reduction.final.bytes < compiled.reduction.initial.bytes);
  assert.equal(compiled.reduction.termination, "one_minimal");
  assert.ok(compiled.output.endsWith(".hotato-repro"));
});

test("counterexample-compile fixture, exit 1: budget_exhausted still resolves", () => {
  /** @type {import("../dist/index.js").CounterexampleCompileResult} */
  const compiled = decodeCliJson(outcome("counterexample-compile-budget.json", 1));
  assert.equal(compiled.exit_code, 1);
  assert.equal(compiled.minimality, "budget_exhausted");
  assert.equal(compiled.reduction.budget, 5);
});

test("counterexample-verify fixture: verified capsule decodes", () => {
  /** @type {import("../dist/index.js").CounterexampleVerifyResult} */
  const verified = decodeCliJson(outcome("counterexample-verify.json", 0));
  assert.equal(verified.kind, "counterexample-verify");
  assert.equal(verified.ok, true);
  assert.equal(verified.status, "verified");
  assert.equal(verified.minimality, "one_minimal");
  assert.equal(verified.evaluator_match, true);
});

test("refusal fixtures, exit 2: RefusalError carries the parsed envelope", () => {
  for (const [name, errorCode] of [
    ["refusal-usage-error.json", "usage_error"],
    ["refusal-file-not-found.json", "file_not_found"],
  ]) {
    const err = capture(() => decodeCliJson(outcome(name, 2)), RefusalError);
    assert.equal(err.name, "RefusalError");
    assert.equal(err.exitCode, 2);
    assert.equal(err.errorCode, errorCode);
    assert.equal(err.refusal.ok, false);
    assert.equal(err.refusal.exit_code, 2);
    assert.equal(typeof err.refusal.message, "string");
    assert.ok(err instanceof HotatoError, "RefusalError extends HotatoError");
  }
});

test("non-JSON stdout on a result exit code throws HotatoError", () => {
  const err = capture(
    () => decodeCliJson({ command: ["hotato"], exitCode: 0, stdout: "plain text", stderr: "" }),
    HotatoError,
  );
  assert.equal(err.exitCode, 0);
  assert.match(err.message, /without JSON/);
});

test("undocumented exit code throws HotatoError with stderr attached", () => {
  const err = capture(
    () => decodeCliJson({ command: ["hotato"], exitCode: 3, stdout: "", stderr: "boom" }),
    HotatoError,
  );
  assert.equal(err.exitCode, 3);
  assert.equal(err.stderr, "boom");
  assert.match(err.message, /exited 3/);
});
