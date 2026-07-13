#!/usr/bin/env python3
"""Run the reference-agent suite OFFLINE and leave a browsable workspace.

Executes the full 375-run reference suite through the deterministic simulator
(``hotato.simulate.run_matrix`` via ``hotato.suite_run.run_suite``): NO live
agent, NO network, NO model. It records the Release / Suite / Scenario / Run /
Conversation / Evaluation rows into a local fleet registry under ``--registry``
(default ``examples/reference-agent/.workspace``), writes the per-test simulated
conversation artifacts + suite report under ``--out``, and prints the REAL counts
(runs, valid, pass/fail/inconclusive per dimension, SIMULATOR_INVALID) and the
wall time.

Browse the result:  ``hotato serve --workspace reference --registry <registry>``

Usage:
    python examples/reference-agent/run_reference.py
    python examples/reference-agent/run_reference.py --parallel 8 --release rc1
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Make the in-repo package importable when run from a source checkout.
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))

from hotato import suite_run as SR  # noqa: E402
from hotato.fleet.registry import Registry  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the reference-agent suite offline.")
    ap.add_argument("--registry", default=os.path.join(HERE, ".workspace"),
                    help="fleet registry home (default examples/reference-agent/.workspace)")
    ap.add_argument("--workspace", default="reference", help="workspace id (default 'reference')")
    ap.add_argument("--release", default="reference-agent-v1",
                    help="release id to record the runs under")
    ap.add_argument("--out", default=os.path.join(HERE, ".out"),
                    help="artifact + report output dir (default examples/reference-agent/.out)")
    ap.add_argument("--parallel", type=int, default=None, help="max worker threads")
    ap.add_argument("--generate", action="store_true",
                    help="regenerate the scenario/test/suite files first")
    args = ap.parse_args(argv)

    if args.generate:
        import generate  # noqa: E402  (sibling module)
        generate.main()

    suite_path = os.path.join(HERE, "suite.json")
    suite_doc, base_dir = SR.load_suite_file(suite_path)

    reg = Registry(args.registry)
    t0 = time.time()
    try:
        result = SR.run_suite(
            suite_doc, base_dir, agent_id="reference-agent-v1",
            release_id=args.release, workspace=args.workspace,
            registry=reg, out_dir=args.out, max_workers=args.parallel,
        )
    finally:
        reg.close()
    elapsed = time.time() - t0

    c = result["counts"]
    print(SR.render_summary_text(result), end="")
    print()
    print(f"REAL COUNTS: tests={c['tests']} runs={c['runs']} valid={c['valid']} "
          f"simulator_invalid={c['simulator_invalid']} "
          f"passed_tests={c['passed_tests']} failed_tests={c['failed_tests']}")
    dims = result["dimensions"]
    for d in ("outcome", "policy", "conversation", "speech", "reliability"):
        b = dims[d]
        print(f"  {d:<13} {b['pass']} pass / {b['fail']} fail / {b['inconclusive']} inconclusive")
    print(f"WALL TIME: {elapsed:.2f}s for {c['runs']} offline simulated runs")
    print(f"exit_code={result['exit_code']}")
    print()
    print(f"browse: hotato serve --workspace {args.workspace} --registry {args.registry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
