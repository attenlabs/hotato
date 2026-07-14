"""Engine drift guard: the vendored ``_engine`` must be byte-identical to its
upstream ``barge_scoring`` source, and must score the bundled fixtures
identically through both import paths. When no upstream source is present (a
fresh public clone where the vendored copy is canonical), these skip cleanly.
"""

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import sync_engine  # noqa: E402


def _source():
    src = sync_engine.resolve_source()
    if src is None:
        pytest.skip("no upstream barge_scoring source present; vendored copy is canonical")
    return src


def test_vendored_engine_byte_identical_to_upstream():
    src = _source()
    drift = sync_engine.check(src)
    assert drift == [], f"vendored _engine drifted from upstream: {drift}"


def _load_upstream(src):
    """Import the upstream barge_scoring.score from a path, without polluting the
    package namespace of the vendored engine."""
    path = os.path.join(src, "score.py")
    spec = importlib.util.spec_from_file_location("_upstream_barge_score", path)
    mod = importlib.util.module_from_spec(spec)
    # score.py imports `from .vad import ...`; give it a package context.
    pkg_spec = importlib.util.spec_from_file_location(
        "_upstream_barge", os.path.join(src, "__init__.py")
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["_upstream_barge"] = pkg
    pkg_spec.loader.exec_module(pkg)
    return pkg


def test_scoring_parity_across_import_paths():
    src = _source()
    # Upstream, imported fresh from the source tree.
    up = _load_upstream(src)
    # Vendored.
    from importlib import resources

    from hotato import _engine as vend

    audio_dir = resources.files("hotato").joinpath("data", "audio")
    scen_dir = resources.files("hotato").joinpath("data", "scenarios")

    import json

    mismatches = []
    for entry in sorted(scen_dir.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".json") or entry.name == "manifest.json":
            continue
        sc = json.loads(entry.read_text(encoding="utf-8"))
        sid = sc["id"]
        wav = str(audio_dir.joinpath(sid + ".example.wav"))

        sig_v = vend.read_wav(wav)
        sig_u = up.read_wav(wav)
        onset = sc.get("caller_onset_sec")

        r_v = vend.score_stereo(sig_v, 0, 1, caller_onset_sec=onset)
        r_u = up.score_stereo(sig_u, 0, 1, caller_onset_sec=onset)

        for field in ("did_yield", "time_to_yield_sec", "talk_over_sec",
                      "caller_onset_sec", "agent_talking_at_onset", "hop_sec"):
            if getattr(r_v, field) != getattr(r_u, field):
                mismatches.append((sid, field, getattr(r_v, field), getattr(r_u, field)))

    assert mismatches == [], f"scoring diverged between vendored and upstream: {mismatches}"
