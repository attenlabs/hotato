"""Gap #5: the staged degradation/robustness battery.

Seeded perturbations (dropout gaps, constant clock-skew resample) stay
byte-deterministic; `hotato battery robustness` renders + scores every stage
of the ladder on the bundled demo wav and emits the stability table as a
standard JSON envelope. Same seed -> same bytes, every time.
"""

import json
import wave
from importlib import resources

from hotato import cli, synth
from tests import _trial_audio as ta


def _demo_wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _frames(path: str) -> int:
    with wave.open(path, "rb") as wf:
        return wf.getnframes()


# --- seeded perturbations stay deterministic ------------------------------

def test_dropout_positions_are_seeded_and_deterministic(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    r = {"transform": "dropout", "count": 3, "gap_ms": 60}
    a = synth.perturb(src, r, out_path=str(tmp_path / "a.wav"), seed=11)
    b = synth.perturb(src, r, out_path=str(tmp_path / "b.wav"), seed=11)
    c = synth.perturb(src, r, out_path=str(tmp_path / "c.wav"), seed=12)
    assert a["output"]["pcm_sha256"] == b["output"]["pcm_sha256"]
    assert a["output"]["pcm_sha256"] != c["output"]["pcm_sha256"]
    # length-preserving: gaps are zeroed in place, never cut out
    assert _frames(str(tmp_path / "a.wav")) == _frames(src)


def test_jitter_resample_is_deterministic_and_stretches_the_timeline(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    r = {"transform": "jitter_resample", "factor": 1.005}
    a = synth.perturb(src, r, out_path=str(tmp_path / "a.wav"), seed=1)
    b = synth.perturb(src, r, out_path=str(tmp_path / "b.wav"), seed=99)
    # no PRNG in the constant-skew path: even a different seed is identical
    assert a["output"]["pcm_sha256"] == b["output"]["pcm_sha256"]
    # the declared rate is KEPT while the timeline stretches by the factor
    assert a["output"]["sample_rate"] == ta.RATE
    assert _frames(str(tmp_path / "a.wav")) == int(_frames(src) * 1.005)


def test_battery_same_seed_same_bytes(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    one = synth.robustness_battery(src, str(tmp_path / "one"), seed=5,
                                   onset_sec=2.0)
    two = synth.robustness_battery(src, str(tmp_path / "two"), seed=5,
                                   onset_sec=2.0)
    assert one["stages"] == two["stages"]   # clips (hashes), metrics, deltas
    # a different seed moves the seeded stages (noise, dropout) ...
    other = synth.robustness_battery(src, str(tmp_path / "three"), seed=6,
                                     onset_sec=2.0)
    by_stage = {r["stage"]: r for r in other["stages"]}
    for row in one["stages"]:
        if row["recipe"] and row["recipe"]["transform"] in ("noise", "dropout"):
            assert (row["clip"]["pcm_sha256"]
                    != by_stage[row["stage"]]["clip"]["pcm_sha256"])
        elif row["recipe"] is not None:   # clip/jitter carry no PRNG
            assert (row["clip"]["pcm_sha256"]
                    == by_stage[row["stage"]]["clip"]["pcm_sha256"])


# --- the battery on the bundled demo wav ----------------------------------

def test_battery_runs_on_the_bundled_demo_wav(tmp_path):
    res = synth.robustness_battery(_demo_wav(), str(tmp_path / "out"),
                                   seed=1, onset_sec=2.4)
    stages = res["stages"]
    assert [r["stage"] for r in stages] == [
        "baseline", "noise-snr20db", "noise-snr10db", "noise-snr5db",
        "clip-0.5", "dropout-3x60ms", "jitter-1.005x"]
    base = stages[0]
    assert base["recipe"] is None and base["clip"] is None
    assert base["scorable"] and base["metrics"]["did_yield"] is True
    for row in stages[1:]:
        assert row["clip"]["pcm_sha256"] and row["clip"]["path"]
        if row["scorable"]:
            assert set(row["delta"]) == {
                "did_yield_flipped", "seconds_to_yield_delta",
                "talk_over_sec_delta", "response_gap_sec_delta"}
    assert res["summary"]["stage_count"] == len(stages)
    assert res["synthetic"] is True and res["axis"] == "synthetic"


# --- CLI: envelope shape + text table -------------------------------------

def test_cli_battery_robustness_json_envelope(tmp_path, capsys):
    code = cli.main(["battery", "robustness", "--wav", _demo_wav(),
                     "--out", str(tmp_path / "out"), "--onset", "2.4",
                     "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "hotato"
    assert payload["kind"] == "robustness-battery"
    assert payload["schema_version"] == synth.SCHEMA_VERSION
    assert payload["axis"] == "synthetic"
    assert payload["designation"] == "synthetic-derived"
    assert payload["stages"][0]["stage"] == "baseline"
    assert payload["summary"]["stage_count"] == 7


def test_cli_battery_robustness_text_table(tmp_path, capsys):
    code = cli.main(["battery", "robustness", "--wav", _demo_wav(),
                     "--out", str(tmp_path / "out"), "--onset", "2.4"])
    assert code == 0
    out = capsys.readouterr().out
    assert "robustness battery:" in out
    assert "baseline" in out and "did_yield=" in out
    assert "response_gap=" in out
    assert "did_yield flipped at:" in out


def test_cli_battery_robustness_missing_wav_exits_2(tmp_path):
    assert cli.main(["battery", "robustness", "--wav",
                     "/nonexistent/definitely-not-here.wav",
                     "--out", str(tmp_path / "out")]) == 2
