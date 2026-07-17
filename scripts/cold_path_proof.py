#!/usr/bin/env python3
"""Cold-path proof: from a clean install of the RELEASED hotato package, run the
credentialless offline first-run in a fresh empty working dir, with no API key
and no configured stack, and record share-safe evidence only.

Share-safe by construction:
  * The recorded command is the LOGICAL invocation ("hotato start --demo") -- never
    the absolute path of the clean-install executable or the interpreter, so no
    install location leaks.
  * Every string written to the evidence (output tails, artifact names) is passed
    through ``_redact``, which recursively replaces the ephemeral sandbox paths
    (the temp HOME and the temp project dir) with stable tokens. No absolute path
    reaches the evidence.
  * Artifacts are split into two lists so a clean rerun REPRODUCES:
      - ``artifacts``           deterministic, content-addressable: name + size +
                                sha256. A clean rerun matches these byte-for-byte.
      - ``volatile_artifacts``  timestamp/signature-bearing (attestation.json,
                                provenance.json, contract.json embed a signing
                                time): recorded by name + size only, with NO digest
                                asserted -- so their run-to-run drift never makes
                                the deterministic block spuriously mismatch.
  * Wall-clock time is isolated under a per-battery ``timing`` block, kept out of
    the deterministic evidence for the same reason.

No audio, no transcript, no identifiers are recorded.
"""
import json, os, sys, subprocess, tempfile, hashlib, time, shutil, shlex

# argv[1] is the hotato invocation (default "hotato"); shlex so a source-tree run
# like "python3 -m hotato" works too. Only the SUBCOMMAND is ever recorded, so the
# recorded command is identical no matter how the tool was launched.
HOTATO = shlex.split(sys.argv[1]) if len(sys.argv) > 1 else ["hotato"]

# Artifacts whose CONTENT carries a wall-clock timestamp or signature, so their
# digest legitimately differs from one clean install to the next. They are recorded
# by name + size only (never with a digest asserted as reproducible), so the
# deterministic evidence stays byte-stable across reruns.
_VOLATILE_BASENAMES = frozenset(
    {"attestation.json", "provenance.json", "contract.json"}
)


def _is_volatile(rel_name):
    return os.path.basename(rel_name) in _VOLATILE_BASENAMES


def _redact(obj, replacements):
    """Recursively replace every ephemeral absolute path with a stable token.

    Walks strings, lists, and dicts alike, so argv, output tails, and artifact
    names are all covered. ``replacements`` is an ordered list of
    ``(needle, token)`` pairs (longest paths first); a falsy needle is skipped so
    an empty root can never blanket-replace the whole string.
    """
    if isinstance(obj, str):
        s = obj
        for needle, token in replacements:
            if needle:
                s = s.replace(needle, token)
        return s
    if isinstance(obj, (list, tuple)):
        return [_redact(x, replacements) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact(v, replacements) for k, v in obj.items()}
    return obj


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()[:16]


def _split_artifacts(root):
    """Split produced files into (deterministic, volatile).

    Deterministic artifacts carry a reproducible ``sha256_16``; volatile
    (timestamp/signature-bearing) artifacts are recorded by name + size only so a
    clean rerun's deterministic evidence is byte-identical.
    """
    deterministic, volatile = [], []
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if _is_volatile(rel):
                volatile.append({"name": rel, "bytes": size})
            else:
                deterministic.append(
                    {"name": rel, "bytes": size, "sha256_16": sha256(p)}
                )
    deterministic.sort(key=lambda x: x["name"])
    volatile.sort(key=lambda x: x["name"])
    return deterministic, volatile


def _battery_record(battery, sub, exit_code, seconds, stdout, stderr,
                    deterministic, volatile, replacements):
    """Assemble one battery's share-safe evidence and redact every path out of it.

    The reproducible portion is ``command`` + ``exit`` + ``artifacts``; wall-clock
    ``timing`` and any ``volatile_artifacts`` are deliberately separated so a clean
    rerun matches the deterministic portion exactly.
    """
    record = {
        "battery": battery,
        # logical, path-free command -- never the exe/interpreter absolute path
        "command": "hotato " + " ".join(sub),
        "exit": exit_code,
        "artifacts": deterministic,
        "volatile_artifacts": volatile,
        "stdout_tail": stdout[-400:],
        "stderr_tail": stderr[-400:],
        # wall-clock, non-reproducible: isolated out of the deterministic evidence
        "timing": {"seconds": round(seconds, 2)},
    }
    return _redact(record, replacements)


def run_cold(battery, sub, cwd, env):
    # Longest / most-specific paths first so the sandbox dirs win before the
    # system-tempdir catch-all (which redacts any OTHER temp path the tool writes,
    # e.g. a demo report under the system temp root).
    _tmp = tempfile.gettempdir()
    replacements = [
        (os.path.realpath(cwd), "<sandbox>"),
        (cwd, "<sandbox>"),
        (os.path.realpath(env["HOME"]), "<sandbox-home>"),
        (env["HOME"], "<sandbox-home>"),
        (os.path.realpath(_tmp), "<tmp>"),
        (_tmp, "<tmp>"),
    ]
    t0 = time.monotonic()
    r = subprocess.run(
        HOTATO + sub, cwd=cwd, env=env, capture_output=True, text=True, timeout=300
    )
    seconds = time.monotonic() - t0
    deterministic, volatile = _split_artifacts(cwd)
    return _battery_record(
        battery, sub, r.returncode, seconds, r.stdout, r.stderr,
        deterministic, volatile, replacements,
    )


def main():
    # A cold user's empty project dir. No key, no connected stack, fresh HOME so
    # there is no prior hotato state.
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and not k.startswith("HOTATO_")}
    env["HOME"] = tempfile.mkdtemp(prefix="coldhome-")
    results = {
        "proof": "machine-cold credentialless first-run of the released hotato package",
        "package": "hotato",
        "reproducibility": (
            "Each battery's 'artifacts' (name + sha256_16) reproduce byte-for-byte "
            "on any clean install. 'volatile_artifacts' embed a signing timestamp, "
            "so only their name + size are recorded (no digest asserted). 'timing' "
            "is wall-clock and not reproducible. All paths are redacted to "
            "<sandbox> / <sandbox-home>."
        ),
        "batteries": [],
    }
    # Battery 1: credentialless guided first run
    d1 = tempfile.mkdtemp(prefix="coldproj-")
    results["batteries"].append(
        run_cold("credentialless_first_run (hotato start)", ["start", "--demo"], d1, env)
    )
    shutil.rmtree(d1, ignore_errors=True)
    # Battery 2: synthetic self-test (no input needed, fully offline)
    d2 = tempfile.mkdtemp(prefix="coldproj2-")
    results["batteries"].append(
        run_cold("bundled_failing_battery (hotato demo)", ["demo"], d2, env)
    )
    shutil.rmtree(d2, ignore_errors=True)
    shutil.rmtree(env["HOME"], ignore_errors=True)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
