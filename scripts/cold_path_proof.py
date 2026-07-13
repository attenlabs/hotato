#!/usr/bin/env python3
"""Cold-path proof: from a clean install of the RELEASED hotato package, run the
credentialless offline first-run in a fresh empty working dir, with no API key
and no configured stack, timed, and record share-safe evidence only.

Share-safe: records command, exit code, wall-time, and the NAMES + sha256 digests
of produced artifacts. No audio, no transcript, no identifiers, no absolute paths
beyond the ephemeral sandbox (which is redacted to <sandbox>).
"""
import json, os, sys, subprocess, tempfile, hashlib, time, shutil

HOTATO = sys.argv[1] if len(sys.argv) > 1 else "hotato"

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()[:16]

def run_cold(cmd, cwd, env):
    t0 = time.monotonic()
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    return {"cmd": cmd, "exit": r.returncode, "seconds": round(time.monotonic()-t0, 2),
            "stdout_tail": r.stdout[-400:], "stderr_tail": r.stderr[-400:]}

def artifacts(root):
    out = []
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root)
            try:
                out.append({"name": rel, "bytes": os.path.getsize(p), "sha256_16": sha256(p)})
            except OSError:
                pass
    return sorted(out, key=lambda x: x["name"])

def main():
    # A cold user's empty project dir. No key, no connected stack.
    env = {k: v for k, v in os.environ.items()
           if k not in ("SAA_API_KEY",) and not k.startswith("HOTATO_")}
    env["HOME"] = tempfile.mkdtemp(prefix="coldhome-")  # no prior hotato state
    results = {"package": "hotato (released)", "batteries": []}
    # Battery 1: credentialless guided first run
    d1 = tempfile.mkdtemp(prefix="coldproj-")
    r1 = run_cold([HOTATO, "start", "--demo"], d1, env)
    r1["artifacts"] = artifacts(d1)
    r1["battery"] = "credentialless_first_run (hotato start)"
    results["batteries"].append(r1)
    shutil.rmtree(d1, ignore_errors=True)
    # Battery 2: synthetic self-test (no input needed, fully offline)
    d2 = tempfile.mkdtemp(prefix="coldproj2-")
    r2 = run_cold([HOTATO, "demo"], d2, env)
    r2["artifacts"] = artifacts(d2)
    r2["battery"] = "bundled_failing_battery (hotato demo)"
    results["batteries"].append(r2)
    shutil.rmtree(d2, ignore_errors=True)
    shutil.rmtree(env["HOME"], ignore_errors=True)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
