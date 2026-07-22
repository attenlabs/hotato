#!/usr/bin/env python3
"""Render the deterministic DTMF conformance fixtures.

Two SYNTHETIC two-channel WAVs, byte-identical on every machine (pure stdlib
``math``, no seed needed because there is no noise -- the tones ARE the ground
truth):

  conformant.wav   the caller channel carries the four digit tone-pairs for
                   "1234" back-to-back inside the stated window; every claimed
                   digit is audibly present.
  defect.wav       identical render EXCEPT the third slot (digit "3") is
                   silenced while the claim stays "1234" -- a delivered-audio
                   disagreement the check must catch, on that digit and only
                   that digit.

The label (``label.json``) states digits, per-digit offsets and durations, the
window, the amplitudes, and the defect, so a reader gets the ground truth in
text next to the audio.

Usage:
  python3 tests/fixtures/dtmf/build_dtmf.py           # write WAVs + label.json
  python3 tests/fixtures/dtmf/build_dtmf.py --check    # re-render to a temp dir
                                                       # and byte-compare to disk
"""

from __future__ import annotations

import argparse
import filecmp
import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # repo root
sys.path.insert(0, os.path.join(REPO, "src"))

from hotato._engine.audio import write_wav  # noqa: E402
from hotato.dtmf_conformance import digit_frequencies  # noqa: E402

SAMPLE_RATE = 8000
CALLER_CHANNEL = 0
AGENT_CHANNEL = 1
DIGITS = "1234"
AMPLITUDE = 0.25          # per sine; the pair peaks at 0.5, well clear of clip
LEAD_SEC = 0.20           # silence before the first digit
DIGIT_SEC = 0.15          # each digit tone, rendered back-to-back
TAIL_SEC = 0.20           # silence after the last digit
DEFECT_INDEX = 2          # the "3" slot is silenced in defect.wav

WINDOW_START_SEC = LEAD_SEC
WINDOW_END_SEC = LEAD_SEC + len(DIGITS) * DIGIT_SEC

CONFORMANT_WAV = "conformant.wav"
DEFECT_WAV = "defect.wav"
LABEL_JSON = "label.json"


def _silence(n: int) -> list:
    return [0.0] * n


def _tone(digit: str, n: int) -> list:
    """A DTMF tone pair for ``digit``, ``n`` samples, phase reset at sample 0."""
    row_f, col_f = digit_frequencies(digit)
    out = []
    for t in range(n):
        theta = 2.0 * math.pi * t / SAMPLE_RATE
        out.append(AMPLITUDE * math.sin(row_f * theta) + AMPLITUDE * math.sin(col_f * theta))
    return out


def _caller_channel(silence_indices) -> list:
    lead = int(round(LEAD_SEC * SAMPLE_RATE))
    digit_n = int(round(DIGIT_SEC * SAMPLE_RATE))
    tail = int(round(TAIL_SEC * SAMPLE_RATE))
    samples = _silence(lead)
    for i, d in enumerate(DIGITS):
        if i in silence_indices:
            samples.extend(_silence(digit_n))
        else:
            samples.extend(_tone(d, digit_n))
    samples.extend(_silence(tail))
    return samples


def _render(out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)
    written = []

    conformant_caller = _caller_channel(silence_indices=set())
    defect_caller = _caller_channel(silence_indices={DEFECT_INDEX})
    agent = _silence(len(conformant_caller))

    conf_path = os.path.join(out_dir, CONFORMANT_WAV)
    defect_path = os.path.join(out_dir, DEFECT_WAV)
    write_wav(conf_path, SAMPLE_RATE, [conformant_caller, agent])
    write_wav(defect_path, SAMPLE_RATE, [defect_caller, agent])
    written.extend([CONFORMANT_WAV, DEFECT_WAV])

    per_digit = []
    for i, d in enumerate(DIGITS):
        row_f, col_f = digit_frequencies(d)
        per_digit.append({
            "index": i,
            "digit": d,
            "row_freq_hz": row_f,
            "col_freq_hz": col_f,
            "offset_sec": round(LEAD_SEC + i * DIGIT_SEC, 6),
            "duration_sec": DIGIT_SEC,
        })
    label = {
        "id": "dtmf-conformance-fixture",
        "source_type": "synthetic",
        "note": "Synthetic rendered DTMF tones. Not a recording of a call.",
        "sample_rate": SAMPLE_RATE,
        "caller_channel": CALLER_CHANNEL,
        "agent_channel": AGENT_CHANNEL,
        "digits": DIGITS,
        "amplitude_per_sine": AMPLITUDE,
        "window_start_sec": round(WINDOW_START_SEC, 6),
        "window_end_sec": round(WINDOW_END_SEC, 6),
        "per_digit": per_digit,
        "files": {
            "conformant": {
                "file": CONFORMANT_WAV,
                "expect": "every claimed digit present",
            },
            "defect": {
                "file": DEFECT_WAV,
                "claimed_digits": DIGITS,
                "silenced_index": DEFECT_INDEX,
                "silenced_digit": DIGITS[DEFECT_INDEX],
                "expect": "digit at silenced_index fails; the others pass",
            },
        },
    }
    label_path = os.path.join(out_dir, LABEL_JSON)
    with open(label_path, "w", encoding="utf-8") as fh:
        json.dump(label, fh, indent=2, sort_keys=True)
        fh.write("\n")
    written.append(LABEL_JSON)
    return written


def _check() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        written = _render(tmp)
        mismatched = []
        for name in written:
            disk = os.path.join(HERE, name)
            fresh = os.path.join(tmp, name)
            if not os.path.exists(disk) or not filecmp.cmp(disk, fresh, shallow=False):
                mismatched.append(name)
    if mismatched:
        print("DTMF fixture is stale or missing: " + ", ".join(mismatched), file=sys.stderr)
        print("Regenerate with: python3 tests/fixtures/dtmf/build_dtmf.py", file=sys.stderr)
        return 1
    print("DTMF fixture regenerates byte-identically.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="re-render to a temp dir and byte-compare to disk")
    args = parser.parse_args(argv)
    if args.check:
        return _check()
    written = _render(HERE)
    print("wrote: " + ", ".join(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
