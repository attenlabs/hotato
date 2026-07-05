#!/usr/bin/env python3
"""Validate one contributed (recording, label) pair for the hotato corpus.

A contribution is a label JSON (see ``corpus/label.schema.json``) plus its
dual-channel WAV. This checks that the pair *conforms* -- structurally and
semantically -- before it ever reaches a human reviewer. It does NOT judge the
recording's quality, score turn-taking, or emit any accuracy figure. It answers
one question: is this a well-formed, honestly-labelled, two-channel contribution?

    python3 corpus/validate.py corpus/examples/sample-contribution.json
    python3 corpus/validate.py my-label.json my-recording.wav

Exit codes: 0 = conforms, 1 = does not, 2 = usage error.

What it enforces:
  - required label fields are present and well-typed (mirrors the JSON Schema);
  - category is should_yield / should_not_yield, and expected.* is consistent
    with it (a should_not_yield case must not carry yield bounds);
  - source_type is one of real-call / role-played / synthetic, and a synthetic
    clip is self-declared as such, never dressed up as real;
  - timings are in range: caller_onset and every labelled segment fall inside
    [0, duration], and each segment is start < end;
  - the attestation booleans hold (consent on file, PII removed, no PHI, right to
    release under MIT), per docs/CORPUS-GOVERNANCE.md;
  - the audio is a real, readable WAV with at least TWO channels, and its sample
    rate / duration match the label.

It is standard-library only. If the ``hotato`` package is importable it uses the
same WAV reader the scorer uses (so "readable" means "the tool can read it");
otherwise it falls back to the stdlib ``wave`` module. Either way, no third-party
dependency is required.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

CATEGORIES = ("should_yield", "should_not_yield")
SOURCE_TYPES = ("real-call", "role-played", "synthetic")
# Small tolerance for header duration vs the labelled duration_sec (seconds).
DURATION_TOL_SEC = 0.10


@dataclass
class Report:
    label_path: str
    audio_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


# --- audio ----------------------------------------------------------------

def _read_audio_meta(path: str):
    """Return (num_channels, sample_rate, duration_sec) or raise.

    Prefer the engine's reader (what the scorer actually sees); fall back to the
    stdlib ``wave`` header if the package is not importable.
    """
    try:
        from hotato._engine import read_wav  # type: ignore

        sig = read_wav(path)
        return sig.num_channels, sig.sample_rate, sig.duration_sec
    except Exception:
        import wave

        with wave.open(path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
        duration = (n_frames / sample_rate) if sample_rate else 0.0
        return n_channels, sample_rate, duration


# --- structural + semantic checks -----------------------------------------

def _require(report: Report, obj: dict, key: str, types) -> bool:
    if key not in obj:
        report.err(f"missing required field: {key!r}")
        return False
    if not isinstance(obj[key], types):
        report.err(f"field {key!r} has the wrong type (got {type(obj[key]).__name__})")
        return False
    return True


def _in_range(x, lo, hi) -> bool:
    return isinstance(x, (int, float)) and lo - 1e-9 <= x <= hi + 1e-9


def _check_segments(report: Report, name: str, segs, duration: float) -> None:
    if segs is None:
        return
    if not isinstance(segs, list):
        report.err(f"reference_render.{name} must be a list of [start, end] pairs")
        return
    for i, seg in enumerate(segs):
        if (not isinstance(seg, list)) or len(seg) != 2:
            report.err(f"reference_render.{name}[{i}] must be a [start, end] pair")
            continue
        s, e = seg
        if not (_in_range(s, 0, duration) and _in_range(e, 0, duration)):
            report.err(
                f"reference_render.{name}[{i}] = [{s}, {e}] falls outside [0, {duration}]"
            )
        elif not s < e:
            report.err(f"reference_render.{name}[{i}] = [{s}, {e}] is not start < end")


def validate(label_path: str, audio_path: Optional[str] = None) -> Report:
    """Validate one contribution. Returns a Report; never raises on bad content."""
    report = Report(label_path=label_path)

    # --- load the label ---------------------------------------------------
    if not os.path.exists(label_path):
        report.err(f"label file not found: {label_path}")
        return report
    try:
        with open(label_path, encoding="utf-8") as fh:
            label = json.load(fh)
    except json.JSONDecodeError as exc:
        report.err(f"label is not valid JSON: {exc}")
        return report
    if not isinstance(label, dict):
        report.err("label must be a JSON object")
        return report

    # --- required scalar fields ------------------------------------------
    _require(report, label, "id", str)
    _require(report, label, "title", str)
    _require(report, label, "sample_rate", int)
    _require(report, label, "duration_sec", (int, float))
    _require(report, label, "caller_onset_sec", (int, float))

    # category
    category = label.get("category")
    if category not in CATEGORIES:
        report.err(f"category must be one of {CATEGORIES}, got {category!r}")

    # source_type (honesty field)
    source_type = label.get("source_type")
    if source_type not in SOURCE_TYPES:
        report.err(f"source_type must be one of {SOURCE_TYPES}, got {source_type!r}")
    if source_type == "synthetic":
        report.warn(
            "source_type=synthetic: this clip is a self-declared synthetic example, "
            "not a real recording. It must never be presented as real."
        )

    # license
    if label.get("license") != "MIT":
        report.err("license must be \"MIT\" (the corpus is redistributed under MIT)")

    # audio filename
    audio_field = label.get("audio")
    if not isinstance(audio_field, str) or not audio_field.endswith(".wav"):
        report.err("field 'audio' must be a .wav filename")

    # --- expected block + consistency with category ----------------------
    expected = label.get("expected")
    if not isinstance(expected, dict):
        report.err("missing or malformed 'expected' block")
        expected = {}
    else:
        for k in ("yield", "max_time_to_yield_sec", "max_talk_over_sec"):
            if k not in expected:
                report.err(f"expected.{k} is required")
        want_yield = expected.get("yield")
        if not isinstance(want_yield, bool):
            report.err("expected.yield must be a boolean")
        if category == "should_not_yield":
            if want_yield is True:
                report.err("category should_not_yield but expected.yield is true")
            for k in ("max_time_to_yield_sec", "max_talk_over_sec"):
                if expected.get(k) is not None:
                    report.err(f"expected.{k} must be null when the agent should NOT yield")
        if category == "should_yield" and want_yield is False:
            report.err("category should_yield but expected.yield is false")

    # --- timings in range -------------------------------------------------
    duration = label.get("duration_sec")
    onset = label.get("caller_onset_sec")
    if isinstance(duration, (int, float)) and duration > 0:
        if isinstance(onset, (int, float)) and not _in_range(onset, 0, duration):
            report.err(f"caller_onset_sec={onset} falls outside [0, {duration}]")
        rr = label.get("reference_render")
        if isinstance(rr, dict):
            _check_segments(report, "caller_segments_sec", rr.get("caller_segments_sec"), duration)
            _check_segments(report, "agent_segments_sec", rr.get("agent_segments_sec"), duration)
            for k in ("caller_offset_sec", "agent_response_onset_sec"):
                v = rr.get(k)
                if v is not None and not _in_range(v, 0, duration):
                    report.err(f"reference_render.{k}={v} falls outside [0, {duration}]")

    # --- attestation ------------------------------------------------------
    att = label.get("attestation")
    if not isinstance(att, dict):
        report.err("missing 'attestation' block (see docs/CORPUS-GOVERNANCE.md)")
    else:
        if not att.get("contributor"):
            report.err("attestation.contributor is required")
        flags = {
            "pii_removed": "PII must be removed before submission",
            "no_phi": "no PHI may be present, regardless of consent",
            "right_to_release_mit": "you must have the right to release under MIT",
        }
        for flag, why in flags.items():
            if att.get(flag) is not True:
                report.err(f"attestation.{flag} must be true: {why}")
        # consent is required for sources with real people
        if source_type in ("real-call", "role-played") and att.get("consent_on_file") is not True:
            report.err(
                "attestation.consent_on_file must be true for real-call / role-played audio"
            )

    # --- the audio itself -------------------------------------------------
    resolved_audio = audio_path
    if resolved_audio is None and isinstance(audio_field, str):
        resolved_audio = os.path.join(os.path.dirname(os.path.abspath(label_path)), audio_field)
    report.audio_path = resolved_audio

    if resolved_audio is None:
        report.err("no audio path to check (label 'audio' missing and none passed)")
    elif not os.path.exists(resolved_audio):
        report.err(f"audio file not found: {resolved_audio}")
    else:
        try:
            n_channels, sample_rate, dur = _read_audio_meta(resolved_audio)
        except Exception as exc:
            report.err(f"could not read audio as a WAV: {exc}")
        else:
            if n_channels < 2:
                report.err(
                    f"audio has {n_channels} channel(s); the corpus requires a "
                    "dual-channel (2+) recording with the caller and agent separated"
                )
            if isinstance(label.get("sample_rate"), int) and sample_rate != label["sample_rate"]:
                report.err(
                    f"sample_rate mismatch: label says {label['sample_rate']} Hz, "
                    f"audio is {sample_rate} Hz"
                )
            if isinstance(duration, (int, float)) and abs(dur - duration) > DURATION_TOL_SEC:
                report.err(
                    f"duration mismatch: label says {duration}s, audio is {dur:.3f}s "
                    f"(tolerance {DURATION_TOL_SEC}s)"
                )

    return report


# --- CLI ------------------------------------------------------------------

def _print_report(report: Report) -> None:
    label = os.path.relpath(report.label_path)
    if report.ok:
        print(f"PASS  {label}")
        if report.audio_path:
            print(f"      audio: {os.path.relpath(report.audio_path)} (2+ channels, readable)")
        for w in report.warnings:
            print(f"      note: {w}")
        print("      Conforms. A human still reviews consent + PII before merge.")
    else:
        print(f"FAIL  {label}")
        for e in report.errors:
            print(f"  - {e}")
        for w in report.warnings:
            print(f"  note: {w}")
        print(f"  {len(report.errors)} problem(s). Fix, then re-run. Nothing gets a pass on vibes.")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    label_path = argv[0]
    audio_path = argv[1] if len(argv) > 1 else None
    report = validate(label_path, audio_path)
    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
