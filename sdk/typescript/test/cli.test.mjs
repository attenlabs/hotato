// Integration tests against the installed hotato CLI, fully offline on the
// fixtures bundled inside the package (src/hotato/data, examples/).
//
// CLI resolution order: $HOTATO_BIN, then the repo venv .v/bin/hotato
// (README SETUP: `python3 -m venv .v && .v/bin/pip install -e .` at the repo
// root). Without a CLI these tests skip; decode.test.mjs and spawn.test.mjs
// keep the JSON contract covered on Node alone.
import { after, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  HotatoError,
  RefusalError,
  compileCounterexample,
  predicate,
  runSuite,
  verifyContracts,
  verifyCounterexample,
} from "../dist/index.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..", "..");
const venvCli = path.join(repoRoot, ".v", "bin", "hotato");
const hotatoBin = process.env.HOTATO_BIN ?? (existsSync(venvCli) ? venvCli : undefined);
const cliTest = hotatoBin
  ? {}
  : { skip: "no hotato CLI: set HOTATO_BIN or create the repo venv .v" };

// Bundled fixtures the CLI ships with.
const demoFailing = path.join(repoRoot, "src", "hotato", "data", "demo", "failing");
const demoWav = path.join(demoFailing, "audio", "fd-01-missed-interruption.example.wav");
const cxExample = path.join(repoRoot, "examples", "counterexample");

const hotatoArgv = (hotatoBin ?? "hotato").split(/\s+/);
const cli = (args, opts = {}) =>
  execFileSync(hotatoArgv[0], [...hotatoArgv.slice(1), ...args], {
    encoding: "utf8",
    ...opts,
  });

const tempDirs = [];
const tempDir = () => {
  const dir = mkdtempSync(path.join(tmpdir(), "hotato-sdk-ts-"));
  tempDirs.push(dir);
  return dir;
};
after(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

test("runSuite: bundled self-test battery passes", cliTest, async () => {
  const suite = await runSuite({ hotatoBin });
  assert.equal(suite.tool, "hotato");
  assert.equal(suite.mode, "suite");
  assert.equal(suite.exit_code, 0);
  assert.equal(suite.summary.failed, 0);
  assert.equal(suite.summary.events, suite.events.length);
  assert.ok(suite.events.length > 0);
  assert.equal(suite.summary.regression, false);
});

test("runSuite: demo failing battery resolves with the failures", cliTest, async () => {
  const suite = await runSuite({
    hotatoBin,
    scenarios: path.join(demoFailing, "scenarios"),
    audio: path.join(demoFailing, "audio"),
  });
  assert.equal(suite.exit_code, 1);
  assert.equal(suite.summary.regression, true);
  assert.equal(suite.summary.failed, 2);
  const missed = suite.events.find((e) => e.event_id === "fd-01-missed-interruption");
  assert.ok(missed);
  assert.equal(missed.verdict.passed, false);
  assert.equal(missed.verdict.did_yield, false);
  assert.ok(missed.verdict.reasons.length > 0);
  assert.equal(suite.fix_map.length, 2);
});

test("verifyContracts: demo failing contract resolves passed:false, exitCode:1", cliTest, async () => {
  const contractsDir = path.join(tempDir(), "contracts");
  cli([
    "contract", "create",
    "--stereo", demoWav,
    "--onset", "2.0",
    "--expect", "yield",
    "--id", "demo-missed-interruption",
    "--out", contractsDir,
    "--format", "json",
  ]);

  const result = await verifyContracts(contractsDir, { hotatoBin });
  assert.equal(result.passed, false);
  assert.equal(result.exitCode, 1);
  assert.equal(result.report.kind, "contract-verify");
  assert.equal(result.report.exit_code, 1);
  assert.equal(result.report.count, 1);
  assert.equal(result.report.summary.failed, 1);
  const contract = result.report.results[0];
  assert.equal(contract.id, "demo-missed-interruption");
  assert.equal(contract.passed, false);
  assert.equal(contract.scorable, true);
  assert.equal(contract.measurement.did_yield, false);
});

test("counterexample: compile, verify, predicate agree on the failure", cliTest, async () => {
  const work = tempDir();
  const scenario = path.join(work, "refund-not-posted.scenario.json");
  const testFile = path.join(work, "refund-not-posted.test.json");
  copyFileSync(path.join(cxExample, "refund-not-posted.scenario.json"), scenario);
  copyFileSync(path.join(cxExample, "refund-not-posted.test.json"), testFile);
  const out = path.join(work, "repro.hotato-repro");

  const compiled = await compileCounterexample({
    hotatoBin,
    scenario,
    test: testFile,
    target: "refund-posted",
    out,
  });
  assert.equal(compiled.kind, "counterexample-compile");
  assert.equal(compiled.exit_code, 0);
  assert.equal(compiled.minimality, "one_minimal");
  assert.equal(compiled.target.assertion_id, "refund-posted");
  assert.equal(compiled.output, out);

  const verified = await verifyCounterexample(out, { hotatoBin });
  assert.equal(verified.kind, "counterexample-verify");
  assert.equal(verified.ok, true);
  assert.equal(verified.status, "verified");
  assert.equal(verified.counterexample_id, compiled.counterexample_id);
  assert.equal(verified.evaluator_match, true);

  const bisect = await predicate(out, { hotatoBin });
  assert.deepEqual(bisect, { failurePresent: true, exitCode: 1 });
});

test("predicate: untestable capsule (exit 125) throws HotatoError", cliTest, async () => {
  await assert.rejects(
    predicate(path.join(tempDir(), "not-a-capsule"), { hotatoBin }),
    (err) => {
      assert.ok(err instanceof HotatoError);
      assert.ok(!(err instanceof RefusalError));
      assert.equal(err.exitCode, 125);
      assert.match(err.message, /untestable/);
      return true;
    },
  );
});

test("refusal: verifyContracts on a dir with no contracts rejects RefusalError", cliTest, async () => {
  const empty = path.join(tempDir(), "no-contracts-here");
  mkdirSync(empty);
  await assert.rejects(
    verifyContracts(empty, { hotatoBin }),
    (err) => {
      assert.ok(err instanceof RefusalError);
      assert.equal(err.exitCode, 2);
      assert.equal(err.errorCode, "usage_error");
      assert.equal(err.refusal.ok, false);
      assert.match(err.refusal.message, /no hotato contracts/);
      return true;
    },
  );
});

test("refusal: compileCounterexample with an unknown target rejects RefusalError", cliTest, async () => {
  const work = tempDir();
  const scenario = path.join(work, "refund-not-posted.scenario.json");
  const testFile = path.join(work, "refund-not-posted.test.json");
  copyFileSync(path.join(cxExample, "refund-not-posted.scenario.json"), scenario);
  copyFileSync(path.join(cxExample, "refund-not-posted.test.json"), testFile);
  await assert.rejects(
    compileCounterexample({
      hotatoBin,
      scenario,
      test: testFile,
      target: "no-such-assertion",
      out: path.join(work, "refused.hotato-repro"),
    }),
    (err) => {
      assert.ok(err instanceof RefusalError);
      assert.equal(err.errorCode, "usage_error");
      assert.match(err.refusal.message, /no-such-assertion/);
      return true;
    },
  );
});
