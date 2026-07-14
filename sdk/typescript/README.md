# @hotato/sdk

Typed TypeScript client for [hotato](https://github.com/attenlabs/hotato) conversation QA. Each call spawns the `hotato` CLI with `--format json` and returns its document parsed into strict interfaces. The CLI's JSON output is the contract: one schema across your terminal, your CI logs, and this SDK. Scoring, reduction, and verification run inside the CLI; the SDK types what it says.

## Install

Build from source in this repo (npm publish is a separate step):

```bash
cd sdk/typescript
npm install && npm run build
```

Depend on it with `"@hotato/sdk": "file:../hotato/sdk/typescript"`.

The client launches `hotato` from PATH (`pip install hotato`). `hotatoBin` accepts any launcher:

```ts
const suite = await runSuite({ hotatoBin: "uvx hotato" });
```

## Score a battery

```ts
import { runSuite } from "@hotato/sdk";

const suite = await runSuite({
  scenarios: "tests/hotato/scenarios",
  audio: "tests/hotato/audio",
});
for (const event of suite.events) {
  if (!event.verdict.passed) console.log(event.event_id, event.verdict.reasons.join("; "));
}
```

A failing battery resolves with `exit_code: 1`, per-event verdicts, and the fix map.

## Gate CI on failure contracts

```ts
import { verifyContracts } from "@hotato/sdk";

const { passed, exitCode, report } = await verifyContracts("contracts/");
for (const r of report.results) {
  if (!r.passed) console.log(r.id, r.measurement);
}
process.exit(exitCode);
```

Counterexample capsules ride the same contract: `compileCounterexample`, `verifyCounterexample`, and `predicate` (git-bisect semantics; exit 1 resolves `{ failurePresent: true }`).

Exit code 2 is a refusal and rejects with `RefusalError` carrying the CLI's parsed error JSON; unexpected failures reject with `HotatoError` (exit code, stderr, full argv). Zero runtime dependencies: `node:child_process` and the type layer only.
