// Fixture replayer used by spawn.test.mjs: prints a captured hotato CLI
// document on stdout and exits with the captured exit code, so the spawn +
// decode path is exercised end to end with zero external dependencies.
//   FAKE_HOTATO_FIXTURE: path of the fixture file to print (optional)
//   FAKE_HOTATO_EXIT:    exit code to return (default 0)
import { readFileSync } from "node:fs";

const fixture = process.env.FAKE_HOTATO_FIXTURE;
if (fixture) {
  process.stdout.write(readFileSync(fixture, "utf8"));
}
process.exit(Number(process.env.FAKE_HOTATO_EXIT ?? "0"));
