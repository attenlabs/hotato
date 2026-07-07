"""`hotato scan`: candidate extraction across a whole recording.

Pinned here, on a synthetic long multi-event WAV built from the bundled
fixtures (whose true onsets are known): the known overlap onsets are found
within tolerance; a long response gap is found; every candidate kind is
timing vocabulary only and NO intent word appears anywhere in the output;
--top caps the listing; zero candidates still exits 0 with the count; and
the windowed (chunked) RMS pass is byte-equal to the reference frame_rms
over the whole file.
"""

import json
import math
import struct
import wave
from importlib import resources

import pytest

from hotato import cli
from hotato import scan as scan_mod
from hotato._engine.audio import frame_rms, read_wav, write_wav
from hotato.scan import KINDS, scan_recording, windowed_frame_rms


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _concat_wav(path, parts, gap_sec=0.0):
    """Concatenate bundled two-channel fixtures (plus optional silence gaps)
    into one long recording; returns the start offset of each part in
    seconds."""
    chans = [[], []]
    offsets = []
    sample_rate = None
    t = 0.0
    for sid in parts:
        s = read_wav(_bundled(sid))
        sample_rate = s.sample_rate
        offsets.append(t)
        chans[0].extend(s.get(0))
        chans[1].extend(s.get(1))
        t += s.num_samples / sample_rate
        if gap_sec:
            pad = [0.0] * int(gap_sec * sample_rate)
            chans[0].extend(pad)
            chans[1].extend(pad)
            t += gap_sec
    write_wav(str(path), sample_rate, chans)
    return offsets


@pytest.fixture(scope="module")
def long_call(tmp_path_factory):
    """01-hard (caller onset 2.40) + 3 s silence + 02-backchannel (caller
    onsets 2.10 / 3.20 / 4.30 while the agent talks throughout)."""
    path = tmp_path_factory.mktemp("scan") / "long-call.wav"
    offsets = _concat_wav(path, ["01-hard-interruption",
                                 "02-backchannel-mhm"], gap_sec=3.0)
    return str(path), offsets


def _overlap_times(result):
    return [c["t_sec"] for c in result["candidates"]
            if c["kind"] == "overlap_while_agent_talking"]


# --- finds the known moments --------------------------------------------------

def test_finds_the_known_overlap_onsets(long_call):
    path, offsets = long_call
    result = scan_recording(path)
    times = _overlap_times(result)
    for expected in (offsets[0] + 2.40, offsets[1] + 2.10,
                     offsets[1] + 3.20, offsets[1] + 4.30):
        assert any(abs(t - expected) <= 0.15 for t in times), (
            f"no overlap candidate near {expected:.2f}s in {times}")


def test_finds_the_long_response_gap(long_call):
    path, offsets = long_call
    result = scan_recording(path)
    gaps = [c for c in result["candidates"]
            if c["kind"] == "long_response_gap"]
    assert gaps, "the 3 s silence between the parts must surface as a gap"
    g = gaps[0]
    # Caller turn ends near 4.70 (+ hangover); the next agent onset is the
    # second part's agent start near offsets[1] + 0.20.
    assert g["t_sec"] == pytest.approx(4.85, abs=0.2)
    assert g["durations"]["gap_sec"] > 2.0
    assert g["agent_reaction"]["next_agent_onset_sec"] == pytest.approx(
        offsets[1] + 0.20, abs=0.2)


def test_overlap_candidates_report_the_agent_reaction(long_call):
    path, offsets = long_call
    result = scan_recording(path)
    near = [c for c in result["candidates"]
            if c["kind"] == "overlap_while_agent_talking"
            and abs(c["t_sec"] - (offsets[0] + 2.40)) <= 0.15]
    (c,) = near
    r = c["agent_reaction"]
    assert r["went_silent_within_search"] is True
    # 01-hard: the agent yields ~0.5 s after the caller onset.
    assert r["after_sec"] == pytest.approx(0.5, abs=0.1)
    assert c["durations"]["overlap_sec"] > 0.2


# --- honesty: timing vocabulary only ------------------------------------------

def test_kinds_are_timing_vocabulary_only(long_call):
    path, _ = long_call
    result = scan_recording(path)
    assert result["candidates"], "the synthetic long call has candidates"
    for c in result["candidates"]:
        assert c["kind"] in KINDS
        assert set(c) == {"t_sec", "kind", "durations", "agent_reaction"}


def test_no_intent_words_anywhere_in_the_output(long_call, capsys):
    path, _ = long_call
    for fmt in ("text", "json"):
        assert cli.main(["scan", "--stereo", path, "--format", fmt]) == 0
        out = capsys.readouterr().out.lower()
        assert "backchannel" not in out
        assert "interruption" not in out
        assert "intent" not in out.replace("timing events", "")


def test_header_states_the_labeling_contract(long_call, capsys):
    path, _ = long_call
    assert cli.main(["scan", "--stereo", path]) == 0
    out = capsys.readouterr().out
    assert ("Candidates are timing events. You decide the expected behavior; "
            "label with: hotato fixture create --onset <t> "
            "--expect yield|hold") in out


# --- agent self-truncation with a silent caller ---------------------------

def _write_stereo_segments(path, caller_segments, agent_segments,
                            duration_sec, sr=16000):
    """Two-channel PCM WAV: caller on channel 0, agent on channel 1. Each
    channel is a pure sine inside its active segments and exact digital
    silence outside them."""
    n = int(duration_sec * sr)

    def _on(segments, t):
        return any(start <= t < end for start, end in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        c = (int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr))
             if _on(caller_segments, t) else 0)
        a = (int(0.35 * 32767 * math.sin(2 * math.pi * 330.0 * i / sr))
             if _on(agent_segments, t) else 0)
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def test_agent_stops_mid_run_with_silent_caller_is_flagged(tmp_path):
    # The agent talks 0.5s-3.0s then goes quiet for the rest of the
    # recording; the caller channel is silent throughout. Nothing on the
    # caller track explains the drop.
    path = _write_stereo_segments(
        tmp_path / "mid-run-stop.wav", [], [(0.5, 3.0)], duration_sec=4.0
    )
    result = scan_recording(path)
    assert result["total_candidates"] == 1
    (c,) = result["candidates"]
    assert c["kind"] == "agent_stop_no_caller"
    assert set(c) == {"t_sec", "kind", "durations", "agent_reaction"}
    assert c["durations"]["trailing_silence_sec"] > 0.5
    assert c["durations"]["caller_proximity_sec"] == pytest.approx(0.5)
    assert c["agent_reaction"] is None


def test_normal_end_of_turn_is_not_flagged(tmp_path):
    # The agent talks 0.5s-3.0s, goes quiet, and the caller takes the floor
    # a beat later (well inside the proximity window): a real hand-off, not
    # a self-truncation.
    path = _write_stereo_segments(
        tmp_path / "normal-handoff.wav", [(3.3, 3.9)], [(0.5, 3.0)],
        duration_sec=4.3,
    )
    result = scan_recording(path)
    kinds = {c["kind"] for c in result["candidates"]}
    assert "agent_stop_no_caller" not in kinds


# --- caps, counts, exits -------------------------------------------------------

def test_top_caps_the_listing_and_reports_the_total(long_call, capsys):
    path, _ = long_call
    assert cli.main(["scan", "--stereo", path, "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "[ 1]" in out and "[ 2]" in out and "[ 3]" not in out
    assert "showing 2 of" in out

    assert cli.main(["scan", "--stereo", path, "--top", "2",
                     "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["shown"] == 2
    assert len(data["candidates"]) == 2
    assert data["total_candidates"] >= 4

    # --top still works when the mixed candidate set includes the new kind.
    full = scan_recording(path)
    assert full["total_candidates"] >= 6
    assert {c["kind"] for c in full["candidates"]} >= {
        "overlap_while_agent_talking", "long_response_gap",
        "agent_stop_no_caller",
    }


def _salience_value(c):
    d = c["durations"]
    for key in ("gap_sec", "overlap_sec", "trailing_silence_sec"):
        if key in d:
            return d[key]
    raise AssertionError(f"no known salience key in {d!r} for kind {c['kind']}")


def test_candidates_are_sorted_by_salience(long_call):
    path, _ = long_call
    result = scan_recording(path)
    assert any(c["kind"] == "agent_stop_no_caller" for c in result["candidates"])
    saliences = [_salience_value(c) for c in result["candidates"]]
    assert saliences == sorted(saliences, reverse=True)


def test_out_writes_every_candidate_even_when_top_caps_stdout(long_call,
                                                              tmp_path,
                                                              capsys):
    path, _ = long_call
    out_file = tmp_path / "candidates.json"
    assert cli.main(["scan", "--stereo", path, "--top", "1",
                     "--out", str(out_file)]) == 0
    capsys.readouterr()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(data["candidates"]) == data["total_candidates"] >= 4
    assert data["note"].startswith("Candidates are timing events.")


def test_zero_candidates_is_exit_0_with_the_count(tmp_path, capsys):
    # Both channels silent: no caller activity and no agent activity, so
    # none of the four candidate kinds has anything to find.
    src = read_wav(_bundled("01-hard-interruption"))
    quiet = tmp_path / "silence.wav"
    write_wav(str(quiet), src.sample_rate,
              [[0.0] * src.num_samples, [0.0] * src.num_samples])
    assert cli.main(["scan", "--stereo", str(quiet)]) == 0
    out = capsys.readouterr().out
    assert "0 candidate moments" in out


def test_usage_errors_exit_2(tmp_path):
    assert cli.main(["scan", "--stereo", "/nonexistent/x.wav"]) == 2
    src = read_wav(_bundled("01-hard-interruption"))
    mono = tmp_path / "mono.wav"
    write_wav(str(mono), src.sample_rate, [src.get(0)])
    assert cli.main(["scan", "--stereo", str(mono)]) == 2
    long_ok = _bundled("01-hard-interruption")
    assert cli.main(["scan", "--stereo", long_ok, "--min-gap", "0"]) == 2


# --- the windowed pass equals the reference -----------------------------------

def test_windowed_rms_equals_reference_frame_rms(long_call, monkeypatch):
    path, _ = long_call
    # Force many small chunks so the windowed path is genuinely exercised.
    monkeypatch.setattr(scan_mod, "_CHUNK_FRAMES", 1000)
    rms_c, rms_a, hop_sec, sample_rate, duration = windowed_frame_rms(path)
    sig = read_wav(path)
    ref_c, ref_hop = frame_rms(sig.get(0), sig.sample_rate)
    ref_a, _ = frame_rms(sig.get(1), sig.sample_rate)
    assert sample_rate == sig.sample_rate
    assert hop_sec == ref_hop
    assert duration == pytest.approx(sig.duration_sec, abs=1e-9)
    assert len(rms_c) == len(ref_c) and len(rms_a) == len(ref_a)
    assert rms_c == ref_c
    assert rms_a == ref_a
