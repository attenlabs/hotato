// Spawn-layer tests: the full runSuite/verifyContracts path driven end to end
// against test/fake-hotato.mjs, a replayer that prints a captured hotato
// document and exits with the captured code. Runs on Node alone; the same
// calls hit the installed CLI in cli.test.mjs.
import { test } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { HotatoError, RefusalError, runSuite, verifyContracts } from "../dist/index.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const fakeCli = path.join(here, "fake-hotato.mjs");
const fixturePath = (name) => path.join(here, "fixtures", name);

const replay = (name, exitCode) => ({
  env: {
    FAKE_HOTATO_FIXTURE: fixturePath(name),
    FAKE_HOTATO_EXIT: String(exitCode),
  },
});

test("hotatoBin as an argv array spawns and decodes", async () => {
  const suite = await runSuite({
    hotatoBin: [process.execPath, fakeCli],
    ...replay("suite-passing.json", 0),
  });
  assert.equal(suite.exit_code, 0);
  assert.equal(suite.summary.passed, 8);
});

test("hotatoBin as a multi-word string splits like 'uvx hotato'", async () => {
  const suite = await runSuite({
    hotatoBin: `node ${fakeCli}`,
    ...replay("suite-failing.json", 1),
  });
  assert.equal(suite.exit_code, 1);
  assert.equal(suite.summary.regression, true);
});

test("verifyContracts wraps the report with passed/exitCode", async () => {
  const result = await verifyContracts("contracts/", {
    hotatoBin: [process.execPath, fakeCli],
    ...replay("contract-verify-failing.json", 1),
  });
  assert.equal(result.passed, false);
  assert.equal(result.exitCode, 1);
  assert.equal(result.report.kind, "contract-verify");
  assert.equal(result.report.results[0].passed, false);
});

test("exit 2 from the CLI rejects with RefusalError", async () => {
  await assert.rejects(
    runSuite({
      hotatoBin: [process.execPath, fakeCli],
      ...replay("refusal-file-not-found.json", 2),
    }),
    (err) => {
      assert.ok(err instanceof RefusalError);
      assert.equal(err.errorCode, "file_not_found");
      assert.equal(err.exitCode, 2);
      return true;
    },
  );
});

test("missing binary rejects with HotatoError, exitCode null", async () => {
  await assert.rejects(
    runSuite({ hotatoBin: "/no/such/dir/hotato-missing-binary" }),
    (err) => {
      assert.ok(err instanceof HotatoError);
      assert.ok(!(err instanceof RefusalError));
      assert.equal(err.exitCode, null);
      assert.match(err.message, /did not run/);
      assert.ok(err.command.length > 0);
      return true;
    },
  );
});
