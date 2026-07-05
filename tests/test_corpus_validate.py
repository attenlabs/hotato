"""Tests for the corpus contribution validator (``corpus/validate.py``).

The validator is a standalone repo script, not part of the shipped package, so we
load it by path. It checks that a contributed (recording, label) pair conforms:
required label fields, category/expected consistency, source-type honesty, timings
in range, the attestation booleans, and -- the load-bearing one -- that the audio
is a readable WAV with at least two channels.

These tests exercise the bundled example (must PASS) and a spread of malformed
inputs (must FAIL), including a mono recording where the corpus requires two
channels.
"""

import importlib.util
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VALIDATE_PY = os.path.join(_REPO, "corpus", "validate.py")
_EXAMPLE = os.path.join(_REPO, "corpus", "examples", "sample-contribution.json")
_STEREO_WAV = os.path.join(_REPO, "src", "hotato", "data", "audio", "01-hard-interruption.example.wav")
_MONO_WAV = os.path.join(_REPO, "examples", "audio", "bc-01-repeated-backchannels.caller.wav")


def _load_validator():
    # Register in sys.modules so the module's @dataclass string annotations resolve.
    spec = importlib.util.spec_from_file_location("hotato_corpus_validate", _VALIDATE_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


VAL = _load_validator()


def _write(tmp_path, label: dict, name="label.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(label), encoding="utf-8")
    return str(p)


def _valid_label() -> dict:
    with open(_EXAMPLE, encoding="utf-8") as fh:
        return json.load(fh)


# --- the bundled example passes -------------------------------------------

def test_bundled_example_conforms():
    report = VAL.validate(_EXAMPLE)
    assert report.ok, report.errors
    assert report.errors == []


def test_example_schema_is_valid_json_schema_if_jsonschema_present():
    schema_path = os.path.join(_REPO, "corpus", "label.schema.json")
    with open(schema_path, encoding="utf-8") as fh:
        schema = json.load(fh)
    with open(_EXAMPLE, encoding="utf-8") as fh:
        label = json.load(fh)
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=label, schema=schema)


# --- malformed inputs fail -------------------------------------------------

def test_mono_recording_is_rejected(tmp_path):
    label = _valid_label()
    # point a valid label at a MONO recording -> the 2-channel rule must fire
    report = VAL.validate(_EXAMPLE, _MONO_WAV)
    assert not report.ok
    assert any("channel" in e for e in report.errors)


def test_missing_required_field_fails(tmp_path):
    label = _valid_label()
    del label["attestation"]
    path = _write(tmp_path, label)
    report = VAL.validate(path, _STEREO_WAV)
    assert not report.ok
    assert any("attestation" in e for e in report.errors)


def test_out_of_range_onset_fails(tmp_path):
    label = _valid_label()
    label["caller_onset_sec"] = 99.0  # past the end of a 6.0s clip
    path = _write(tmp_path, label)
    report = VAL.validate(path, _STEREO_WAV)
    assert not report.ok
    assert any("caller_onset_sec" in e for e in report.errors)


def test_category_expected_inconsistency_fails(tmp_path):
    label = _valid_label()
    # should_yield but declared not to yield -> inconsistent
    label["expected"]["yield"] = False
    path = _write(tmp_path, label)
    report = VAL.validate(path, _STEREO_WAV)
    assert not report.ok
    assert any("should_yield" in e for e in report.errors)


def test_broken_attestation_flags_fail(tmp_path):
    label = _valid_label()
    label["attestation"]["no_phi"] = False
    label["attestation"]["pii_removed"] = False
    path = _write(tmp_path, label)
    report = VAL.validate(path, _STEREO_WAV)
    assert not report.ok
    assert any("no_phi" in e for e in report.errors)
    assert any("pii_removed" in e for e in report.errors)


def test_non_mit_license_fails(tmp_path):
    label = _valid_label()
    label["license"] = "GPL-3.0"
    path = _write(tmp_path, label)
    report = VAL.validate(path, _STEREO_WAV)
    assert not report.ok
    assert any("MIT" in e for e in report.errors)


def test_out_of_range_segment_fails(tmp_path):
    label = _valid_label()
    label["reference_render"]["caller_segments_sec"] = [[2.40, 99.0]]  # end past duration
    path = _write(tmp_path, label)
    report = VAL.validate(path, _STEREO_WAV)
    assert not report.ok
    assert any("caller_segments_sec" in e for e in report.errors)


# --- the CLI entry point returns the right exit codes ----------------------

def test_cli_main_exit_codes(capsys):
    assert VAL.main([_EXAMPLE]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    # a non-existent label is a failure, not a crash
    assert VAL.main([os.path.join(_REPO, "corpus", "does-not-exist.json")]) == 1
    # no args is a usage error
    assert VAL.main([]) == 2
