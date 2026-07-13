"""Cold-path proof generator: path-hygiene + reproducibility.

The generator records credentialless first-run evidence. Two invariants it MUST
hold and used not to:

  1. NO ABSOLUTE-PATH LEAK. The old generator wrote ``cmd`` (argv, whose argv[0]
     is the absolute clean-install executable path) and the raw output tails
     verbatim into the evidence, so an ephemeral install/home/project path leaked
     even though the docstring promised redaction. Every string it now records is
     recursively redacted to a stable token.
  2. A CLEAN RERUN REPRODUCES. Deterministic (content-addressable) artifacts carry
     a sha256; timestamp/signature-bearing artifacts and wall-clock time are held
     in separate blocks with no digest asserted, so the deterministic evidence is
     byte-identical run to run.

These exercise the pure helpers directly -- no hotato subprocess -- so they run
offline and fast.
"""

import importlib.util
import json
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "cold_path_proof.py")


def _load():
    spec = importlib.util.spec_from_file_location("cold_path_proof", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cpp = _load()

# Representative ephemeral sandbox paths, as a real run would produce them.
SANDBOX = "/tmp/coldproj-abc123"
SANDBOX_HOME = "/tmp/coldhome-xyz789"
EXE = "/opt/venv-clean-install/bin/hotato"
REPLACEMENTS = [
    (SANDBOX, "<sandbox>"),
    (SANDBOX_HOME, "<sandbox-home>"),
]


def _no_absolute_path(blob: str) -> bool:
    # No ephemeral sandbox path survived redaction.
    return SANDBOX not in blob and SANDBOX_HOME not in blob and EXE not in blob


def test_redact_recursively_strips_absolute_paths():
    payload = {
        "cmd": [EXE, "start", "--demo"],
        "stdout_tail": f"wrote {SANDBOX}/contracts/x.json under {SANDBOX_HOME}/.cache",
        "nested": [{"path": f"{SANDBOX}/evidence/frames.jsonl"}],
    }
    reds = REPLACEMENTS + [(EXE, "<hotato>")]
    out = cpp._redact(payload, reds)
    blob = json.dumps(out)
    assert _no_absolute_path(blob)
    assert "<sandbox>" in blob and "<sandbox-home>" in blob
    # recursion reached the nested dict-inside-list
    assert out["nested"][0]["path"] == "<sandbox>/evidence/frames.jsonl"


def test_split_artifacts_separates_volatile_from_deterministic(tmp_path):
    root = tmp_path / "proj"
    (root / "contracts" / "x.hotato" / "evidence").mkdir(parents=True)
    # deterministic (content-addressable)
    (root / "contracts" / "x.hotato" / "evidence" / "frames.jsonl").write_text("{}\n")
    (root / "hotato-sweep.json").write_text('{"ok": true}')
    # volatile (timestamp / signature bearing)
    (root / "contracts" / "x.hotato" / "attestation.json").write_text('{"t": 1}')
    (root / "contracts" / "x.hotato" / "provenance.json").write_text('{"t": 2}')
    (root / "contracts" / "x.hotato" / "contract.json").write_text('{"t": 3}')

    deterministic, volatile = cpp._split_artifacts(str(root))
    det_names = {a["name"] for a in deterministic}
    vol_names = {a["name"] for a in volatile}

    assert "hotato-sweep.json" in det_names
    assert any(n.endswith("frames.jsonl") for n in det_names)
    assert all(n.endswith(("attestation.json", "provenance.json", "contract.json"))
               for n in vol_names)
    assert len(vol_names) == 3
    # deterministic entries carry a reproducible digest; volatile ones must NOT
    assert all("sha256_16" in a for a in deterministic)
    assert all("sha256_16" not in a for a in volatile)


def test_battery_record_isolates_timing_and_leaks_no_path():
    rec = cpp._battery_record(
        battery="credentialless_first_run (hotato start)",
        sub=["start", "--demo"],
        exit_code=0,
        seconds=1.2345,
        stdout=f"report: {SANDBOX}/reports/initial.html\n",
        stderr=f"[start] wrote under {SANDBOX_HOME}\n",
        deterministic=[{"name": "hotato-sweep.json", "bytes": 12, "sha256_16": "deadbeefdeadbeef"}],
        volatile=[{"name": "contracts/x.hotato/attestation.json", "bytes": 8}],
        replacements=REPLACEMENTS,
    )
    # command is the logical, path-free invocation -- no exe/interpreter path
    assert rec["command"] == "hotato start --demo"
    # wall-clock is isolated, not a top-level reproducible field
    assert rec["timing"]["seconds"] == 1.23
    assert "seconds" not in rec
    # every recorded string is redacted
    assert _no_absolute_path(json.dumps(rec))
    # volatile artifact still carries no asserted digest
    assert "sha256_16" not in rec["volatile_artifacts"][0]
