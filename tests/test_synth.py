"""Deterministic synthetic perturbations stay separate from real evidence."""
import os

from hotato import synth, core
from tests import _trial_audio as ta


def test_matrix_generates_with_synthetic_provenance(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    for i, recipe in enumerate(synth.default_matrix()):
        prov = synth.perturb(src, recipe, out_path=str(tmp_path / f"p{i}.wav"), seed=7)
        assert prov["synthetic"] is True
        assert prov["designation"] == "synthetic-derived"
        assert prov["parent"]["pcm_sha256"] and prov["output"]["pcm_sha256"]
        assert prov["recipe"] == recipe


def test_perturbation_is_deterministic(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    r = {"transform": "noise", "snr_db": 20}
    a = synth.perturb(src, r, out_path=str(tmp_path / "a.wav"), seed=42)
    b = synth.perturb(src, r, out_path=str(tmp_path / "b.wav"), seed=42)
    c = synth.perturb(src, r, out_path=str(tmp_path / "c.wav"), seed=99)
    assert a["output"]["pcm_sha256"] == b["output"]["pcm_sha256"]
    assert a["output"]["pcm_sha256"] != c["output"]["pcm_sha256"]


def test_resampled_clip_still_scores(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    out = str(tmp_path / "r.wav")
    synth.perturb(src, {"transform": "resample", "rate": 8000}, out_path=out, seed=1)
    env = core.run_single(stereo=out, onset_sec=2.0, expect="yield")
    assert env["events"][0].get("scorable", True) is not False
