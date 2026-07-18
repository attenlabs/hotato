import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


import pathlib
import subprocess

_SUITES = pathlib.Path(_ROOT) / "corpus" / "suites"
_CLASSES = pathlib.Path(_ROOT) / "corpus" / "classes"
_EXAMPLES = pathlib.Path(_ROOT) / "examples"
_RENDER = _EXAMPLES / "render_examples.py"

def pytest_sessionstart(session):
    """Render suite/class/example audio when absent. Deterministic (seed =
    sha256(id)), so a fresh render is byte-identical to the committed audio.
    This is what lets the sdist prune the heavy rendered wavs and still run the
    full suite from an extracted tree: the labels and builders ship, the audio
    is reconstructed on first collection."""
    if _SUITES.is_dir():
        missing = [d for d in _SUITES.iterdir()
                   if d.is_dir() and (d / "scenarios").is_dir() and not (d / "audio").is_dir()]
        if missing:
            subprocess.run([sys.executable, str(_SUITES / "build_suites.py")], check=True, cwd=_ROOT)
    if _CLASSES.is_dir():
        missing = [d for d in _CLASSES.iterdir()
                   if d.is_dir() and (d / "scenarios").is_dir() and not (d / "audio").is_dir()]
        if missing:
            subprocess.run([sys.executable, str(_CLASSES / "build_classes.py")], check=True, cwd=_ROOT)
    # examples/audio and examples/funnel-demo/audio, rendered in place by the
    # canonical generator when either is missing.
    if _RENDER.is_file():
        example_scen_dirs = [
            _EXAMPLES / "scenarios",
            _EXAMPLES / "funnel-demo" / "scenarios",
            _EXAMPLES / "full-duplex" / "scenarios",
        ]
        if any(s.is_dir() and not (s.parent / "audio").is_dir()
               for s in example_scen_dirs):
            subprocess.run([sys.executable, str(_RENDER)], check=True, cwd=_ROOT)
