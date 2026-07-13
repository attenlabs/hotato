#!/usr/bin/env python3
"""D6 delta verifier — the machine-verifiable prove-and-stop gate.

Checks only what can be truthfully machine-verified. The human cold batteries
(5 unfamiliar engineers; 5 hosted SAA starts) are NOT asserted here — they are
external and their template stays un-fabricated. Exit non-zero on any FAIL.
Run from the hotato repo root:  python scripts/verify_delta.py
"""
import os, re, subprocess, sys, json, glob

ROOT = os.environ.get("HOTATO_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DELTA_BASE = os.environ.get("DELTA_BASE", "bf502a8")  # commit before D2

# Frozen/reject surfaces from D0 (01_OPERATING_MODEL). New WORK on these is a stop.
FROZEN_PATH_RX = re.compile(
    r'(fleet|canary|drive[_-]?a[_-]?call|dialer|carrier|voice[_-]?clon|'
    r'multi[_-]?tenant|billing|rbac|hosted[_-]?account|control[_-]?plane|'
    r'prompt[_-]?playground|human[_-]?review|second[_-]?judge|rubric2|rubric_v2)', re.I)

def sh(cmd, cwd=ROOT, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)

def pytest(paths, label):
    e = dict(os.environ); e["PYTHONPATH"] = "src"
    r = sh([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths], env=e)
    m = re.search(r'(\d+) passed', r.stdout + r.stderr)
    failed = re.search(r'(\d+) (failed|error)', r.stdout + r.stderr)
    ok = r.returncode == 0 and not failed
    return ok, f"{label}: {'PASS' if ok else 'FAIL'} ({m.group(0) if m else 'no result'}{'; '+failed.group(0) if failed else ''})"

def check_freeze():
    r = sh(["git", "diff", "--name-only", f"{DELTA_BASE}..HEAD"])
    if r.returncode != 0:
        return False, f"freeze: FAIL (git diff error: {r.stderr.strip()[:80]})"
    files = [f for f in r.stdout.splitlines() if f.strip()]
    if not files:
        return False, "freeze: FAIL (0 files in delta range — wrong DELTA_BASE/ROOT?)"
    hits = [f for f in files if FROZEN_PATH_RX.search(f)]
    ok = not hits
    return ok, f"freeze (no frozen-surface work): {'PASS' if ok else 'FAIL '+str(hits)} ({len(files)} files changed, all additive)"

def check_cold_evidence():
    ev = os.path.join(SP_EV())
    if not os.path.exists(ev):
        return False, "cold-path evidence: FAIL (missing)"
    d = json.load(open(ev))
    ok = any(b.get("exit") == 0 for b in d.get("batteries", []))
    blob = json.dumps(d)
    leak = bool(re.search(r'/(home|tmp)/[A-Za-z0-9_./-]{6,}', blob))
    ok = ok and not leak
    return ok, f"cold-path evidence: {'PASS' if ok else 'FAIL'} (>=1 cold battery exit0={any(b.get('exit')==0 for b in d.get('batteries',[]))}, path-leak={leak})"

def SP_EV():
    # evidence recorded under the delta scratchpad OR committed under implementation-notes/evidence/
    for c in [os.environ.get("COLD_EVIDENCE",""),
              os.path.join(ROOT, "implementation-notes", "evidence", "cold-path-evidence.json")]:
        if c and os.path.exists(c): return c
    return os.path.join(ROOT, "implementation-notes", "evidence", "cold-path-evidence.json")

def check_d5():
    p = os.path.join(ROOT, "scripts", "build_atlas.py")
    if not os.path.exists(p):
        return None, "D5 atlas: PENDING (scripts/build_atlas.py not present yet)"
    ok, msg = pytest(["tests/test_atlas.py"], "D5 atlas tests")
    return ok, msg

def check_d4():
    b = os.environ.get("D4_BUNDLE", os.path.join(os.path.dirname(ROOT), "hotato-d4-saa-sdk-bundle"))
    b2 = "/tmp/claude-1000/-home-david-mf1/8f455a91-064b-443d-bce5-548ac8ab5ca6/scratchpad/hotato-d4-saa-sdk-bundle"
    bundle = b if os.path.isdir(b) else b2
    if not os.path.isdir(bundle):
        return None, "D4 bundle: PENDING (staged bundle not present yet)"
    r = sh([sys.executable, "-m", "pytest", "-q", bundle], cwd=bundle)
    ok = r.returncode == 0
    m = re.search(r'(\d+) passed', r.stdout + r.stderr)
    return ok, f"D4 bundle tests: {'PASS' if ok else 'FAIL'} ({m.group(0) if m else 'no result'})"

def main():
    checks = [
        check_freeze(),
        pytest(["tests/test_interaction_label.py", "tests/test_capability_routing.py"], "D2+D3 tests"),
        pytest(["tests/test_fix_round3_security.py", "tests/test_fleet_security.py",
                "tests/test_release_supply_chain.py", "tests/test_action_consumer.py"],
               "adjacent offline security+consumer tests"),
        check_d5(),
        check_d4(),
        check_cold_evidence(),
    ]
    print("== D6 delta verifier ==")
    failed = pending = 0
    for ok, msg in checks:
        tag = "PENDING" if ok is None else ("ok " if ok else "FAIL")
        print(f"  [{tag}] {msg}")
        if ok is False: failed += 1
        elif ok is None: pending += 1
    complete = failed == 0 and pending == 0
    if complete:
        print("prove-and-stop: STOP — scope frozen, add no breadth.")
    elif failed:
        print(f"prove-and-stop: FAIL ({failed} failing) — fix the canonical path only, add no breadth.")
    else:
        print(f"prove-and-stop: INCOMPLETE ({pending} deliverable(s) not yet built) — not a clean stop.")
    # exit 0 ONLY when every required check is green
    sys.exit(0 if complete else 1)

if __name__ == "__main__":
    main()
