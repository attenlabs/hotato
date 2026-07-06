import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


import pathlib
import subprocess

_SUITES = pathlib.Path(_ROOT) / "corpus" / "suites"

def pytest_sessionstart(session):
    """Render suite audio when absent. Deterministic (seed = sha256(id)), ~19s."""
    if _SUITES.is_dir():
        missing = [d for d in _SUITES.iterdir()
                   if d.is_dir() and (d / "scenarios").is_dir() and not (d / "audio").is_dir()]
        if missing:
            subprocess.run(["python3", str(_SUITES / "build_suites.py")], check=True, cwd=_ROOT)
