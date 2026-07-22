"""DTMF conformance check + fixture.

A "DTMF sent" claim is conformant only when the tones are audibly present in the
delivered audio at the claimed time. The conformant render carries every claimed
digit's tone pair and passes on every digit; the defect render silences one
digit's slot while the claim is unchanged, and the check catches that digit --
and only that digit -- as a delivered-audio disagreement.

The fixtures are synthetic, deterministic (pure stdlib math), and regenerate
byte-identically via ``tests/fixtures/dtmf/build_dtmf.py``.
"""

from __future__ import annotations

import filecmp
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO, "tests", "fixtures", "dtmf")
BUILDER_PATH = os.path.join(FIXTURE_DIR, "build_dtmf.py")

from hotato.dtmf_conformance import (  # noqa: E402
    DTMF_DIGITS,
    check_dtmf_conformance,
    check_dtmf_conformance_wav,
    detect_digit_presence,
    digit_frequencies,
    goertzel_power,
)


def _load_builder():
    spec = importlib.util.spec_from_file_location("hotato_build_dtmf", BUILDER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


BUILDER = _load_builder()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Render the fixtures fresh into a temp dir (independent of any on-disk
    copy) so every assertion runs against a byte-determined render."""
    out = tmp_path_factory.mktemp("dtmf_fixture")
    BUILDER._render(str(out))
    return str(out)


# --------------------------------------------------------------------------
# the check against the rendered fixtures
# --------------------------------------------------------------------------

def test_conformant_render_passes_every_digit(rendered):
    path = os.path.join(rendered, BUILDER.CONFORMANT_WAV)
    result = check_dtmf_conformance_wav(
        path, BUILDER.CALLER_CHANNEL, BUILDER.DIGITS,
        BUILDER.WINDOW_START_SEC, BUILDER.WINDOW_END_SEC,
    )
    assert result["status"] == "PASS"
    assert [d["status"] for d in result["per_digit"]] == ["PASS"] * len(BUILDER.DIGITS)
    for d in result["per_digit"]:
        assert d["row_energy"] >= result["threshold"]
        assert d["col_energy"] >= result["threshold"]


def test_defect_render_fails_only_the_silenced_digit(rendered):
    path = os.path.join(rendered, BUILDER.DEFECT_WAV)
    result = check_dtmf_conformance_wav(
        path, BUILDER.CALLER_CHANNEL, BUILDER.DIGITS,
        BUILDER.WINDOW_START_SEC, BUILDER.WINDOW_END_SEC,
    )
    assert result["status"] == "FAIL"
    statuses = [d["status"] for d in result["per_digit"]]
    for i, status in enumerate(statuses):
        if i == BUILDER.DEFECT_INDEX:
            assert status == "FAIL", f"expected the silenced digit {i} to fail"
        else:
            assert status == "PASS", f"digit {i} should still pass"
    # exactly one digit failed: the named one
    assert statuses.count("FAIL") == 1


def test_out_of_range_window_is_inconclusive(rendered):
    """A window past the end of the recording cannot be measured -- the check
    reports INCONCLUSIVE, distinct from a caught FAIL."""
    path = os.path.join(rendered, BUILDER.CONFORMANT_WAV)
    result = check_dtmf_conformance_wav(
        path, BUILDER.CALLER_CHANNEL, "1", 100.0, 101.0,
    )
    assert result["status"] == "INCONCLUSIVE"
    assert result["per_digit"][0]["row_energy"] is None


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------

def test_fixture_is_byte_deterministic(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    written = BUILDER._render(str(a))
    BUILDER._render(str(b))
    for name in written:
        assert filecmp.cmp(a / name, b / name, shallow=False), f"{name} differs between renders"


def test_builder_check_passes():
    """The on-disk fixture matches a fresh render (the ``--check`` contract)."""
    assert BUILDER.main(["--check"]) == 0


def test_detector_is_deterministic(rendered):
    path = os.path.join(rendered, BUILDER.CONFORMANT_WAV)
    r1 = check_dtmf_conformance_wav(
        path, BUILDER.CALLER_CHANNEL, BUILDER.DIGITS,
        BUILDER.WINDOW_START_SEC, BUILDER.WINDOW_END_SEC,
    )
    r2 = check_dtmf_conformance_wav(
        path, BUILDER.CALLER_CHANNEL, BUILDER.DIGITS,
        BUILDER.WINDOW_START_SEC, BUILDER.WINDOW_END_SEC,
    )
    assert r1 == r2


# --------------------------------------------------------------------------
# detector unit checks
# --------------------------------------------------------------------------

def test_digit_frequencies_table_is_complete():
    assert len(DTMF_DIGITS) == 16
    assert digit_frequencies("1") == (697, 1209)
    assert digit_frequencies("D") == (941, 1633)
    assert digit_frequencies("d") == (941, 1633)  # case-insensitive letters
    assert digit_frequencies("#") == (941, 1477)
    with pytest.raises(ValueError):
        digit_frequencies("Z")


def test_goertzel_finds_a_single_tone():
    import math
    sr = 8000
    n = 800
    tone = [math.sin(2 * math.pi * 770 * t / sr) for t in range(n)]
    p_on = goertzel_power(tone, sr, 770)
    p_off = goertzel_power(tone, sr, 1633)
    assert p_on > 100 * p_off  # energy concentrates at the tone's bin


def test_silent_slot_fails_not_inconclusive():
    """A readable slot with no energy is a FAIL (the claimed tones are absent),
    not INCONCLUSIVE (which is reserved for slots with no samples)."""
    res = detect_digit_presence([0.0] * 1200, 8000, "5")
    assert res["status"] == "FAIL"
    empty = detect_digit_presence([], 8000, "5")
    assert empty["status"] == "INCONCLUSIVE"
