"""M0 correctness + determinism gates for the bundled battery and envelope.

These run with zero external files and zero third-party deps: they exercise the
package exactly as a fresh `uvx` install would.
"""

import json
import wave

import pytest

from hotato import core as _core
from hotato.core import LIMITS, run_single, run_suite

REQUIRED_TOP_LEVEL = {
    "tool", "schema_version", "mode", "stack", "offline", "engine",
    "limits", "summary", "events", "fix_map", "funnel", "exit_code",
}


def _bundled_stereo_path(scenario_id):
    from importlib import resources
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", scenario_id + ".example.wav"
        )
    )


def test_bundled_suite_all_pass():
    env = run_suite(suite="barge-in")
    assert env["summary"]["events"] == 8
    assert env["summary"]["failed"] == 0
    assert env["summary"]["passed"] == 8
    assert env["summary"]["regression"] is False
    assert env["exit_code"] == 0


def test_run_suite_scenarios_dir_deeply_nested_json_raises_cleanly(tmp_path):
    """A --scenarios-dir file that is pathologically deeply nested JSON makes
    CPython's json decoder raise a bare RecursionError, not a
    json.JSONDecodeError. run_suite must turn that into a clean ValueError
    (-> exit 2 upstream), never let a RecursionError propagate raw."""
    scen = tmp_path / "scen"
    scen.mkdir()
    (scen / "deep.json").write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        run_suite(scenarios_dir=str(scen), audio_dir=str(tmp_path / "audio"))
    assert not isinstance(excinfo.value, RecursionError)
    assert "not valid JSON" in str(excinfo.value)


def test_load_bundled_scenarios_deeply_nested_json_raises_cleanly(monkeypatch):
    """Same defense as the --scenarios-dir branch, applied for consistency to
    the bundled-battery loader: a corrupt/deeply-nested bundled scenario file
    must raise a clean ValueError, never propagate a raw RecursionError."""

    class _FakeEntry:
        name = "deep.json"

        def read_text(self, encoding="utf-8"):
            return "[" * 200000 + "]" * 200000

    class _FakePkg:
        def joinpath(self, *_parts):
            return self

        def iterdir(self):
            return [_FakeEntry()]

    def _fake_files(_name):
        return _FakePkg()

    import importlib.resources as importlib_resources
    monkeypatch.setattr(importlib_resources, "files", _fake_files)

    with pytest.raises(ValueError) as excinfo:
        _core._load_bundled_scenarios()
    assert not isinstance(excinfo.value, RecursionError)
    assert "not valid JSON" in str(excinfo.value)


@pytest.mark.parametrize("bad_scenario", [
    {},                                  # no 'id' at all
    {"id": ""},                          # empty string id
    {"id": 123},                         # non-string id
    "not-a-dict",                        # scenario itself is not an object
])
def test_run_suite_scenario_missing_id_raises_cleanly(tmp_path, bad_scenario):
    """A scenarios-dir JSON file with no (or a non-string/empty) 'id' field --
    valid JSON, but not a valid scenario -- must raise a clean ValueError, not
    an uncaught KeyError from ``sc["id"]`` breaking the exit-2 usage-error
    contract (docs/SUBMITTING.md invites third-party scenario submissions)."""
    scen = tmp_path / "scen"
    scen.mkdir()
    (scen / "bad.json").write_text(json.dumps(bad_scenario), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        run_suite(scenarios_dir=str(scen), audio_dir=str(tmp_path / "audio"))
    assert not isinstance(excinfo.value, KeyError)
    assert "valid string 'id' field" in str(excinfo.value)


def test_envelope_schema_shape():
    env = run_suite(suite="barge-in")
    assert REQUIRED_TOP_LEVEL.issubset(env.keys())
    assert env["tool"] == "hotato"
    assert env["schema_version"] == "1"
    assert env["offline"] is True
    for e in env["events"]:
        assert {"event_id", "expected_yield", "verdict", "measurements"}.issubset(e.keys())


def test_hard_negatives_hold():
    """02-backchannel and 07-echo-bleed are should_not_yield: the agent must NOT
    yield, and the event must still PASS. This is the proof the scorer measures
    real floor-taking, not any overlap."""
    env = run_suite(suite="barge-in")
    by_id = {e["scenario_id"]: e for e in env["events"]}
    for sid in ("02-backchannel-mhm", "07-echo-bleed"):
        e = by_id[sid]
        assert e["expected_yield"] is False, sid
        assert e["verdict"]["did_yield"] is False, sid
        assert e["verdict"]["passed"] is True, sid


def test_accuracy_claim_is_null():
    """The honesty invariant: no accuracy percentage is ever claimed."""
    assert LIMITS["accuracy_claim"] is None
    env = run_suite(suite="barge-in")
    assert env["limits"]["accuracy_claim"] is None


def test_determinism_two_runs_byte_identical():
    a = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    b = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    assert a == b


def test_numpy_and_stdlib_decode_agree():
    """The optional numpy WAV-decode path must produce the same result as the
    pure-stdlib path, so a published number never depends on whether numpy is
    installed."""
    from hotato._engine import audio as A

    with_numpy = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    saved = A._np
    A._np = None  # force the stdlib decode path
    try:
        without_numpy = json.dumps(run_suite(suite="barge-in"), sort_keys=True)
    finally:
        A._np = saved
    assert with_numpy == without_numpy


def test_numpy_and_stdlib_agree_on_fuzzed_random_wavs(tmp_path):
    """Defect (round 3): the bundled suite happens to round the same way on its
    fixed fixtures, which gives false confidence that the numpy and stdlib RMS
    paths are identical everywhere. numpy's np.mean uses pairwise summation while
    the stdlib path accumulates sequentially, so they can disagree in the last
    bit. This fuzzes many random 2-channel WAVs (varied amplitudes down to the
    16-bit quantization floor) and asserts the FULL exposed envelope -- every
    surfaced, rounded number and the verdict -- is byte-identical with numpy on
    vs forced off. If a future precision change ever breaks the rounded-output
    invariant at a threshold boundary, this catches it instead of the fixed
    suite silently masking it."""
    import random

    from hotato._engine import audio as A
    from hotato._engine.audio import write_wav

    saved = A._np
    if saved is None:
        pytest.skip("numpy not installed; nothing to compare against")

    sr = 16000
    rng = random.Random(20260708)
    paths = []
    for k in range(40):
        n = rng.randint(12000, 40000)
        amp = rng.choice([1.0, 0.5, 0.05, 1.0 / 32768.0, 3.0 / 32768.0])
        caller = [amp * rng.uniform(-1, 1) for _ in range(n)]
        agent = [amp * rng.uniform(-1, 1) for _ in range(n)]
        p = tmp_path / f"fuzz-{k}.wav"
        write_wav(str(p), sr, [caller, agent])
        paths.append(str(p))

    def _envs():
        out = []
        for p in paths:
            for expect in ("yield", "hold"):
                env = run_single(stereo=p, expect=expect, onset_sec=0.3)
                out.append(json.dumps(env, sort_keys=True))
        return out

    with_numpy = _envs()
    try:
        A._np = None  # force the pure-stdlib decode + RMS path
        without_numpy = _envs()
    finally:
        A._np = saved
    assert without_numpy == with_numpy


def test_regression_sets_exit_code_and_fix():
    """A failing verdict must flip exit_code to 1, mark regression, and attach a
    fix. Score a real interruption fixture but demand an impossible latency."""
    env = run_single(
        stereo=_bundled_stereo_path("01-hard-interruption"),
        onset_sec=None,
        expect="yield",
        stack="livekit",
        max_time_to_yield_sec=0.0,  # impossible -> must fail
    )
    assert env["exit_code"] == 1
    assert env["summary"]["regression"] is True
    assert len(env["fix_map"]) >= 1
    assert env["fix_map"][0]["fix_class"] in ("config", "engagement-control")


# --- audio provenance: identity of the exact bytes an event scored ----------

def test_run_single_stereo_carries_audio_provenance():
    env = run_single(
        stereo=_bundled_stereo_path("01-hard-interruption"), expect="yield")
    prov = env["events"][0]["audio_provenance"]
    assert prov["schema_version"] == "1"
    assert len(prov["sha256"]) == 64
    assert prov["sides"][0]["role"] == "stereo"
    assert prov["sides"][0]["sha256"] == prov["sha256"]
    assert prov["sides"][0]["sample_rate"] > 0
    assert prov["sides"][0]["num_samples"] > 0
    assert prov["sides"][0]["duration_sec"] > 0


def test_run_single_stereo_provenance_is_deterministic_for_the_same_file():
    a = run_single(stereo=_bundled_stereo_path("01-hard-interruption"),
                    expect="yield")
    b = run_single(stereo=_bundled_stereo_path("01-hard-interruption"),
                    expect="yield")
    assert (a["events"][0]["audio_provenance"]["sha256"]
            == b["events"][0]["audio_provenance"]["sha256"])


def test_run_single_stereo_provenance_differs_for_different_files():
    a = run_single(stereo=_bundled_stereo_path("01-hard-interruption"),
                    expect="yield")
    b = run_single(stereo=_bundled_stereo_path("02-backchannel-mhm"),
                    expect="hold")
    assert (a["events"][0]["audio_provenance"]["sha256"]
            != b["events"][0]["audio_provenance"]["sha256"])


def test_run_single_caller_agent_provenance_has_both_sides_and_a_combined_hash(
        tmp_path):
    from hotato._engine.audio import write_wav

    caller = tmp_path / "caller.wav"
    agent = tmp_path / "agent.wav"
    write_wav(str(caller), 16000, [[0.1] * 1600])
    write_wav(str(agent), 16000, [[0.2] * 1600])
    env = run_single(caller=str(caller), agent=str(agent), expect="yield")
    prov = env["events"][0]["audio_provenance"]
    roles = {s["role"]: s for s in prov["sides"]}
    assert set(roles) == {"caller", "agent"}
    assert roles["caller"]["sha256"] != roles["agent"]["sha256"]
    # The combined hash is order-stable and distinct from either side alone
    # (mirrors contract.py's _sha256_two_files), so a caller-only or
    # agent-only re-recording still changes the event's overall identity.
    assert prov["sha256"] not in (roles["caller"]["sha256"], roles["agent"]["sha256"])


def test_run_suite_events_all_carry_audio_provenance():
    env = run_suite(suite="barge-in")
    for e in env["events"]:
        prov = e.get("audio_provenance")
        assert prov is not None, e["event_id"]
        assert len(prov["sha256"]) == 64


def test_stream_sha256_never_reads_the_whole_file_in_one_call(tmp_path, monkeypatch):
    """The provenance hash must be safe on a multi-hour recording: it reads
    the file in fixed-size chunks, never loading the whole thing into memory
    in one ``read()``. Proven functionally (not just by code inspection): a
    spying ``open`` records the largest single ``read(n)`` size the hasher
    ever requests and asserts it never exceeds the chunk size, regardless of
    how large the underlying file is."""
    import wave

    from hotato import core

    path = tmp_path / "big.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        # ~6 MiB of silence: several chunk-size reads, fast to write in a
        # unit test.
        wf.writeframes(b"\x00\x00" * (3 * 1024 * 1024))

    real_open = open
    max_read = {"n": 0}
    target = str(path)

    def spying_open(file, mode="r", *a, **k):
        fh = real_open(file, mode, *a, **k)
        if str(file) == target and "b" in mode:
            orig_read = fh.read

            def spy_read(n=-1, *ra, **rk):
                if not (isinstance(n, int) and n > 0):
                    raise AssertionError(
                        "unbounded read() would load the whole file")
                max_read["n"] = max(max_read["n"], n)
                return orig_read(n, *ra, **rk)

            fh.read = spy_read
        return fh

    monkeypatch.setattr(core, "open", spying_open, raising=False)
    digest = core._stream_sha256(target)
    assert len(digest) == 64
    assert 0 < max_read["n"] <= (1 << 20)


# --- decoded-PCM identity: same conversation, re-exported ---------------

def _write_test_wav(path, n_samples=2000, sample_rate=16000):
    """A small deterministic mono wav, built the same way the rest of this
    file already synthesizes fixtures (``write_wav``, no external audio)."""
    from hotato._engine.audio import write_wav

    samples = [(i % 100) / 100.0 - 0.5 for i in range(n_samples)]
    write_wav(str(path), sample_rate, [samples])


def test_run_single_stereo_provenance_carries_pcm_sha256():
    env = run_single(
        stereo=_bundled_stereo_path("01-hard-interruption"), expect="yield")
    side = env["events"][0]["audio_provenance"]["sides"][0]
    assert len(side["pcm_sha256"]) == 64
    # Distinct from the raw-file digest: a different identity, not an alias.
    assert side["pcm_sha256"] != side["sha256"]


def test_pcm_sha256_deterministic_for_the_same_file(tmp_path):
    from hotato import core

    p = tmp_path / "a.wav"
    _write_test_wav(p)
    assert core._stream_pcm_sha256(str(p)) == core._stream_pcm_sha256(str(p))


def test_pcm_sha256_unchanged_by_a_header_only_edit(tmp_path):
    """A byte_rate/block_align-style edit inside the fmt chunk never touches
    the data chunk: the DECODED samples are identical, so pcm_sha256 must
    match, even though the raw-file sha256 (which hashes the whole
    container) differs. This is the exact re-export/repack shape a re-tagged
    or re-muxed copy of the same recording produces."""
    from hotato import core

    orig = tmp_path / "orig.wav"
    edited = tmp_path / "edited.wav"
    _write_test_wav(orig)
    data = bytearray(orig.read_bytes())
    # Canonical 44-byte stdlib-``wave`` header: byte_rate is the 4 bytes at
    # offset 28. Corrupting it (to a value inconsistent with this file's
    # sample_rate/channels/width) changes no sample byte -- getframerate()/
    # getnframes()/readframes() never read this field.
    assert data[36:40] == b"data"  # sanity: this is the canonical layout
    data[28:32] = (999).to_bytes(4, "little")
    edited.write_bytes(bytes(data))

    assert core._stream_pcm_sha256(str(orig)) == core._stream_pcm_sha256(str(edited))
    assert core._stream_sha256(str(orig)) != core._stream_sha256(str(edited))


def test_pcm_sha256_unchanged_by_a_trailing_byte_append(tmp_path):
    """Bytes appended after the declared data chunk (a common artifact of a
    naive re-save/re-mux) are never read by ``readframes()`` -- bounded by
    the header's own frame count -- so the decoded identity is unchanged
    while the raw-file identity moves."""
    from hotato import core

    orig = tmp_path / "orig.wav"
    appended = tmp_path / "appended.wav"
    _write_test_wav(orig)
    data = orig.read_bytes()
    appended.write_bytes(data + b"\x7f" * 137)

    assert core._stream_pcm_sha256(str(orig)) == core._stream_pcm_sha256(str(appended))
    assert core._stream_sha256(str(orig)) != core._stream_sha256(str(appended))


def test_pcm_sha256_changes_on_a_single_sample_edit(tmp_path):
    """The inverse of the two invariance tests above: editing even one
    sample byte inside the data chunk must move pcm_sha256, so the digest is
    a substantive content check, not a constant that happens to survive the
    two crafted edits above."""
    from hotato import core

    orig = tmp_path / "orig.wav"
    edited = tmp_path / "edited.wav"
    _write_test_wav(orig)
    data = bytearray(orig.read_bytes())
    data[44] ^= 0xFF  # first byte of the first PCM sample, past the header
    edited.write_bytes(bytes(data))

    assert core._stream_pcm_sha256(str(orig)) != core._stream_pcm_sha256(str(edited))


def test_pcm_sha256_streams_in_bounded_chunks(tmp_path, monkeypatch):
    """Proven functionally, mirroring
    ``test_stream_sha256_never_reads_the_whole_file_in_one_call``: a spying
    ``readframes`` records every chunk size requested while hashing a file
    that needs several chunks under a (test-lowered) chunk size, and asserts
    more than one call happened and none exceeded the bound -- never a
    single ``readframes(n_frames)`` covering the whole file."""
    from hotato import core

    monkeypatch.setattr(core, "_PCM_HASH_CHUNK_FRAMES", 500)

    p = tmp_path / "big.wav"
    _write_test_wav(p, n_samples=1800)  # > 3 chunks of 500 frames

    calls = []
    real_readframes = wave.Wave_read.readframes

    def spy_readframes(self, n):
        assert n <= 500, "requested more than the bounded chunk size"
        calls.append(n)
        return real_readframes(self, n)

    monkeypatch.setattr(wave.Wave_read, "readframes", spy_readframes)
    digest = core._stream_pcm_sha256(str(p))
    assert len(digest) == 64
    assert len(calls) > 1, "expected multiple bounded reads, not one bulk read"


def test_mono_stereo_file_rejected():
    """A single-channel file passed as --stereo must raise, never silently
    mis-score as if it had separated channels."""
    import os
    import struct
    import tempfile
    import wave

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        with wave.open(tmp.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<" + "h" * 1600, *([0] * 1600)))
        with pytest.raises(ValueError):
            run_single(stereo=tmp.name, expect="yield")
    finally:
        os.unlink(tmp.name)
