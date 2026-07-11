"""``synth.synth_battery`` runs the full default matrix over ONE real fixture and
returns derived-clip provenance on a SEPARATE synthetic axis.

Guards the plan §11 'fast version': the matrix carries the acoustic perturbation
kinds (including the new backchannel / agent-gap / packet-gap / trailing-silence
sweeps), every derived item carries full provenance (parent PCM hash, recipe,
seed, tool+version, output hashes) and is explicitly tagged synthetic, and the
whole battery is byte-for-byte deterministic across two runs at the same seed.
"""
import os

from hotato import synth
from tests import _trial_audio as ta

# The transform kinds the fast-version matrix must cover.
EXPECTED_KINDS = {
    "resample", "gain", "noise", "leakage", "invert_channels",
    "leading_silence", "trailing_silence", "onset_offset", "clip",
    "backchannel", "agent_gap", "packet_gap",
}


def test_default_matrix_covers_every_expected_transform_kind():
    kinds = {r["transform"] for r in synth.default_matrix()}
    assert EXPECTED_KINDS <= kinds
    # the four sweeps added for the fast version are all present
    assert {"backchannel", "agent_gap", "packet_gap", "trailing_silence"} <= kinds


def test_synth_battery_yields_kinds_with_full_synthetic_provenance(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    records = synth.synth_battery(src, str(tmp_path / "out"), seed=3)

    assert len(records) == len(synth.default_matrix())
    kinds = {r["recipe"]["transform"] for r in records}
    assert EXPECTED_KINDS <= kinds

    parent_pcm = records[0]["parent"]["pcm_sha256"]
    assert parent_pcm  # a real fixture parent hash
    for rec in records:
        # explicit SEPARATE-axis + synthetic designation on every derived clip
        assert rec["axis"] == "synthetic"
        assert rec["synthetic"] is True
        assert rec["designation"] == "synthetic-derived"
        # full provenance: parent hash + recipe + seed + tool+version + outputs
        assert rec["parent"]["pcm_sha256"] == parent_pcm
        assert rec["recipe"]["transform"] in EXPECTED_KINDS
        assert rec["seed"] == 3
        assert rec["tool"] == synth.TOOL and synth.__version__ in rec["tool"]
        assert rec["output"]["pcm_sha256"] and rec["output"]["raw_sha256"]
        assert os.path.exists(str(tmp_path / "out" /
                                  os.path.basename(rec["output"]["path"])))


def test_synth_battery_is_deterministic_across_two_runs(tmp_path):
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    a = synth.synth_battery(src, str(tmp_path / "a"), seed=5)
    b = synth.synth_battery(src, str(tmp_path / "b"), seed=5)
    assert [r["output"]["raw_sha256"] for r in a] == \
           [r["output"]["raw_sha256"] for r in b]
    assert [r["output"]["pcm_sha256"] for r in a] == \
           [r["output"]["pcm_sha256"] for r in b]
    # a different seed changes the seeded (noise) clips
    c = synth.synth_battery(src, str(tmp_path / "c"), seed=99)
    assert [r["output"]["pcm_sha256"] for r in a] != \
           [r["output"]["pcm_sha256"] for r in c]


def test_synthetic_clips_stay_off_the_real_axis(tmp_path):
    """Every derived record self-identifies as synthetic, so a renderer can never
    blend it into the real-call axis (plan §11: synthetic must never raise the
    evidentiary confidence of one real recapture)."""
    src = str(tmp_path / "src.wav"); ta.yielding_call(src)
    records = synth.synth_battery(src, str(tmp_path / "out"), seed=1)
    assert all(r["axis"] == "synthetic" for r in records)
    assert not any(r.get("axis") == "real" for r in records)
