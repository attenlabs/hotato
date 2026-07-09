#!/usr/bin/env python3
"""Diarized-mono benchmark v2: FIXED + RECALIBRATED code vs a REAL backend.

Re-runs the exact methodology of
`hotato-launch/DIARIZE-BENCHMARK-2026-07-09.md` (spec 8) against the current
`hotato.diarize` module, after the two shipped-code defects that benchmark
surfaced were fixed and the confidence gate was recalibrated with a 7th
signal (yield-boundary confidence). Throwaway harness, no product code here;
calls only the SHIPPED `hotato.diarize` public functions, in the same order
`prepare_diarized_mono` uses internally (broken apart so this script can also
pull the raw `DiarizationResult` for DER, which the `DiarizedMono` wrapper
does not expose).

Requires the `[diarize]` extra (torch, pyannote.audio>=4.0, pyannote.metrics)
and a Hugging Face token with `speaker-diarization-community-1`'s conditions
accepted (`HF_TOKEN` / `HUGGINGFACE_TOKEN` / `HUGGING_FACE_HUB_TOKEN`). See
README.md for a known-good CPU install recipe.

Corpus: the 13 vendored AMI dual-channel fixtures in `corpus/real/audio/`
(each fixture's two headset channels ARE the ground truth: the two parties
are already isolated). For each fixture:
  1. TRUTH = score_channels(caller_channel, agent_channel)  -- the real
     dual-channel verdict, auto-detected onset, exactly as a genuine
     two-channel call would be scored (no label lookahead).
  2. mono = caller_channel + agent_channel (summed, clipped to [-1, 1]).
  3. Diarize the mono with the real `pyannote` backend -> DiarizationResult.
  4. assign_speakers -> propose caller/agent; separation_confidence -> tier.
  5. reconstruct_tracks -> two masked tracks (Option A, the shipped
     reconstruction) -> score_channels -> D, the diarized-mono verdict.
  6. DER: hypothesis = run-length segments from the diarizer's own per-frame
     timelines; reference = the AMI manual word-alignment `reference_render`
     segments (the corpus's true gold, independent of any channel VAD).
     Two conventions: NIST/CALLHOME (collar=0.25, overlap ignored) and strict
     (collar=0.0, overlap scored), both via `pyannote.metrics`.

Writes `bench_results.json` (one record per fixture) for `aggregate.py`.
"""

from __future__ import annotations

import json
import os
import sys
import time
import wave

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.join(REPO, "src"))

from hotato import _engine  # noqa: E402
from hotato import diarize as D  # noqa: E402
from hotato._engine.score import ScoreConfig, score_channels  # noqa: E402
from hotato._engine.vad import BackendUnavailable  # noqa: E402

FIXTURE_IDS = [
    "ami-en2002b-bc-0859",
    "ami-en2002b-bc-1049",
    "ami-en2002b-bc-1114",
    "ami-en2002b-bc-1416",
    "ami-en2002b-take-0149",
    "ami-en2002b-take-0772",
    "ami-en2002b-take-0913",
    "ami-en2002b-take-0930",
    "ami-en2002b-take-1069",
    "ami-es2002a-bc-0526",
    "ami-es2002a-bc-0687",
    "ami-es2002a-bc-1049",
    "ami-es2002a-take-0677",
]

AUDIO_DIR = os.path.join(REPO, "corpus", "real", "audio")
SCEN_DIR = os.path.join(REPO, "corpus", "real", "scenarios")
OUT_PATH = os.path.join(HERE, "bench_results.json")


def _write_mono(path: str, mono, sample_rate: int) -> None:
    import struct

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for v in mono:
            v = max(-1.0, min(1.0, v))
            frames += struct.pack("<h", int(round(v * 32767)))
        w.writeframes(bytes(frames))


def _sum_to_mono(c, a):
    n = min(len(c), len(a))
    return [max(-1.0, min(1.0, c[i] + a[i])) for i in range(n)]


def _timeline_from_segments(segments_sec, hop_sec, n_frames):
    """A boolean activity timeline on the hop grid from [start, end] second
    pairs (the corpus's `reference_render` shape)."""
    tl = [False] * n_frames
    for s, e in segments_sec:
        lo = max(0, int(round(s / hop_sec)))
        hi = min(n_frames, int(round(e / hop_sec)))
        for k in range(lo, hi):
            tl[k] = True
    return tl


def _frame_overlap(a, b):
    n = min(len(a), len(b))
    return sum(1 for i in range(n) if a[i] and b[i])


def _iou(a, b):
    """Jaccard overlap of two boolean timelines. Raw intersection COUNT is the
    wrong metric for label identification here: the true 'agent' role is
    active almost the entire clip (floor-holder), so every diarizer label
    with any activity at all shares a large raw intersection with it purely
    because it is near-universal -- IoU normalizes that out."""
    n = min(len(a), len(b))
    inter = sum(1 for i in range(n) if a[i] and b[i])
    union = sum(1 for i in range(n) if a[i] or b[i])
    return inter / union if union else 0.0


def _run_length_segments(active, hop_sec):
    segs = []
    n = len(active)
    i = 0
    while i < n:
        if active[i]:
            j = i
            while j < n and active[j]:
                j += 1
            segs.append((i * hop_sec, j * hop_sec))
            i = j
        else:
            i += 1
    return segs


def _der(reference_by_role, hyp_result, cfg):
    """(nist, strict) DER, pyannote.metrics, hypothesis built from the
    diarizer's OWN per-frame timelines (run-length encoded), reference built
    from the AMI word-alignment `reference_render` (the corpus's true gold,
    independent of any channel VAD or of this pipeline's reconstruction)."""
    from pyannote.core import Annotation, Segment
    from pyannote.metrics.diarization import DiarizationErrorRate

    ref = Annotation()
    for role, segs in reference_by_role.items():
        for s, e in segs:
            if e > s:
                ref[Segment(s, e)] = role

    hyp = Annotation()
    hop = hyp_result.hop_sec
    for label, active in hyp_result.speaker_active.items():
        for s, e in _run_length_segments(active, hop):
            if e > s:
                hyp[Segment(s, e)] = label

    nist_metric = DiarizationErrorRate(collar=0.25, skip_overlap=True)
    strict_metric = DiarizationErrorRate(collar=0.0, skip_overlap=False)
    nist = float(nist_metric(ref, hyp))
    strict = float(strict_metric(ref, hyp))
    return nist, strict


def _identify_labels(result, reference_by_role):
    """Which diarizer label corresponds to which TRUE role (caller/agent),
    by frame-overlap against the AMI word-alignment reference timelines --
    independent of `assign_speakers`' floor-dominance heuristic, so the
    'assign ok' check is honest (not circular)."""
    hop = result.hop_sec
    n = max((len(v) for v in result.speaker_active.values()), default=0)
    ref_tl = {
        role: _timeline_from_segments(segs, hop, n)
        for role, segs in reference_by_role.items()
    }
    label_true_role = {}
    for label, active in result.speaker_active.items():
        best_role, best_iou = None, -1.0
        for role, tl in ref_tl.items():
            iou = _iou(active, tl)
            if iou > best_iou:
                best_role, best_iou = role, iou
        label_true_role[label] = best_role
    return label_true_role


def bench_one(fixture_id: str, cfg: ScoreConfig) -> dict:
    wav_path = os.path.join(AUDIO_DIR, fixture_id + ".example.wav")
    scen_path = os.path.join(SCEN_DIR, fixture_id + ".json")
    with open(scen_path, encoding="utf-8") as fh:
        scenario = json.load(fh)

    sig = _engine.read_wav(wav_path)
    assert sig.num_channels == 2, f"{fixture_id}: expected 2-channel fixture"
    c, a = sig.get(0), sig.get(1)
    n = min(len(c), len(a))
    c, a = c[:n], a[:n]
    sr = sig.sample_rate

    truth = score_channels(c, a, sr, caller_onset_sec=None, cfg=cfg)

    mono = _sum_to_mono(c, a)

    t0 = time.time()
    try:
        result = D.diarize_mono(mono, sr, backend="pyannote", num_speakers=2, cfg=cfg)
        backend_error = None
    except BackendUnavailable as exc:
        result = None
        backend_error = str(exc)
    diarize_sec = time.time() - t0

    record = {
        "id": fixture_id,
        "category": scenario["category"],
        "expected_yield": scenario["expected"]["yield"],
        "truth_did_yield": truth.did_yield,
        "truth_talk_over_sec": truth.talk_over_sec,
        "truth_time_to_yield_sec": truth.time_to_yield_sec,
        "diarize_sec": round(diarize_sec, 2),
        "backend_error": backend_error,
    }
    if result is None:
        record.update(tier="backend_unavailable", d_did_yield=None)
        return record

    reference_by_role = {
        "caller": scenario["reference_render"]["caller_segments_sec"],
        "agent": scenario["reference_render"]["agent_segments_sec"],
    }
    label_true_role = _identify_labels(result, reference_by_role)

    speaker_map = D.assign_speakers(result)
    assign_ok = label_true_role.get(speaker_map["caller"]) == "caller" and (
        label_true_role.get(speaker_map["agent"]) == "agent"
    )

    sep = D.separation_confidence(result, speaker_map, backend="pyannote", cfg=cfg)
    tier = sep["confidence_tier"]

    der_nist, der_strict = _der(reference_by_role, result, cfg)

    record.update(
        tier=tier,
        separation_confidence=sep["separation_confidence"],
        speaker_map=speaker_map,
        assign_ok=assign_ok,
        overlap_ratio=sep["signals"]["overlap_ratio"],
        embedding_margin=sep["signals"].get("embedding_margin"),
        segment_churn_per_sec=sep["signals"]["segment_churn_per_sec"],
        yield_boundary=sep["signals"].get("yield_boundary"),
        reason=sep.get("reason"),
        der_nist=round(der_nist, 4),
        der_strict=round(der_strict, 4),
    )

    if tier == "refuse":
        record.update(d_did_yield=None, d_talk_over_sec=None, d_time_to_yield_sec=None)
        return record

    caller_track, agent_track = D.reconstruct_tracks(
        mono, result, speaker_map["caller"], speaker_map["agent"],
        sample_rate=sr, cfg=cfg,
    )
    d = score_channels(caller_track, agent_track, sr, caller_onset_sec=None, cfg=cfg)
    record.update(
        d_did_yield=d.did_yield,
        d_talk_over_sec=d.talk_over_sec,
        d_time_to_yield_sec=d.time_to_yield_sec,
    )
    return record


def main() -> int:
    import hotato  # noqa: F401  -- registers the real diarizer factories

    cfg = ScoreConfig()
    records = []
    for fid in FIXTURE_IDS:
        print(f"== {fid} ==", file=sys.stderr)
        t0 = time.time()
        rec = bench_one(fid, cfg)
        print(f"   tier={rec.get('tier')} truth={rec['truth_did_yield']} "
              f"d={rec.get('d_did_yield')} ({time.time() - t0:.1f}s)",
              file=sys.stderr)
        records.append(rec)

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"records": records}, fh, indent=2)
    print(f"wrote {OUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
