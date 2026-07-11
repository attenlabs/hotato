"""The single structured ERROR contract, shared by both agent surfaces.

Success is a schema'd envelope; every FAILURE is the ``ok: false`` object in
schema/error.v1.json, emitted with the SAME shape and error_code by the CLI
(--format json, exit 2) and by the one MCP tool, so an agent needs one parser
for the whole call lifecycle. Pinned here:

  * each error class -> the structured error object + exit 2 on the CLI json
    path, AND the same-error_code object from the MCP tool;
  * --format text still prints a plain "error:" line (unchanged);
  * MCP messages are rewritten from CLI flags to the tool's parameter names;
  * the SUCCESS envelope is byte-identical on both surfaces (no ok key);
  * the "exactly one input mode" constraint is a clean structured error, not a
    raw throw (only-caller, and suite-plus-a-recording).
"""

import json
import math
import os
import struct
import wave
from importlib import resources

import pytest

from hotato import cli, errors, mcp_server
from hotato.core import run_single, run_suite

jsonschema = pytest.importorskip("jsonschema")


# --- fixtures ---------------------------------------------------------------

def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _write_wav(path, *, n_channels=2, sample_rate=16000, n_frames=1600, tone=False):
    frames = bytearray()
    for i in range(n_frames):
        for _ in range(n_channels):
            v = int(0.3 * 32767 * math.sin(2 * math.pi * 220 * i / sample_rate)) if tone else 0
            frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return str(path)


def _silent_caller_stereo(path):
    """Two channels: the agent talks, the caller never does -> not scorable."""
    sr = 16000
    n = int(3.0 * sr)
    frames = bytearray()
    for i in range(n):
        t = i / sr
        a = int(0.35 * 32767 * math.sin(2 * math.pi * 330.0 * i / sr)) if 0.2 <= t < 2.8 else 0
        frames += struct.pack("<hh", 0, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _error_schema():
    return json.loads(
        resources.files("hotato")
        .joinpath("schema", "error.v1.json")
        .read_text(encoding="utf-8")
    )


def _assert_error_shape(obj, code):
    jsonschema.validate(instance=obj, schema=_error_schema())
    assert obj["tool"] == "hotato"
    assert obj["schema_version"] == "1"
    assert obj["ok"] is False
    assert obj["error_code"] == code
    assert obj["exit_code"] == 2
    # honesty + house style: no accuracy percentage, no em/en dashes
    assert "%" not in obj["message"]
    assert "—" not in obj["message"] and "–" not in obj["message"]


# --- CLI json path: each error class -> structured error + exit 2 -----------

def _cli_json_error(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr().out
    assert code == 2, out
    return json.loads(out)


def test_cli_missing_input(capsys):
    # only --caller (no --agent): incomplete single -> missing_input
    obj = _cli_json_error(["run", "--caller", "x.wav", "--format", "json"], capsys)
    _assert_error_shape(obj, "missing_input")


def test_cli_mode_conflict(capsys):
    obj = _cli_json_error(
        ["run", "--suite", "--stereo", _bundled("01-hard-interruption"),
         "--format", "json"], capsys)
    _assert_error_shape(obj, "mode_conflict")


def test_cli_mono_as_stereo(tmp_path, capsys):
    mono = _write_wav(tmp_path / "mono.wav", n_channels=1, tone=True)
    obj = _cli_json_error(["run", "--stereo", mono, "--format", "json"], capsys)
    _assert_error_shape(obj, "mono_as_stereo")


def test_cli_sample_rate_mismatch(tmp_path, capsys):
    a = _write_wav(tmp_path / "a.wav", n_channels=1, sample_rate=16000, tone=True)
    b = _write_wav(tmp_path / "b.wav", n_channels=1, sample_rate=8000, tone=True)
    obj = _cli_json_error(
        ["run", "--caller", a, "--agent", b, "--format", "json"], capsys)
    _assert_error_shape(obj, "sample_rate_mismatch")


def test_cli_file_not_found(tmp_path, capsys):
    missing = str(tmp_path / "nope.wav")
    obj = _cli_json_error(["run", "--stereo", missing, "--format", "json"], capsys)
    _assert_error_shape(obj, "file_not_found")


def test_cli_unknown_suite(capsys):
    obj = _cli_json_error(["run", "--suite", "no-such-suite", "--format", "json"], capsys)
    _assert_error_shape(obj, "unknown_suite")


def test_recursion_error_is_handled_and_classified():
    """RecursionError is not a ValueError or OSError subclass, so it must be
    listed explicitly in errors.HANDLED (the CLI/MCP catch boundary) and
    classified to a clean, stable error_code -- never left to propagate as a
    raw traceback and the uncaught-exception default exit code (1)."""
    exc = RecursionError("maximum recursion depth exceeded")
    assert isinstance(exc, errors.HANDLED)
    obj = errors.cli_error(exc)
    _assert_error_shape(obj, "input_too_deeply_nested")
    assert "too deeply nested" in obj["message"]


def test_cli_memory_error_on_oversized_decode(tmp_path, capsys, monkeypatch):
    """An oversized recording whose PCM decode raises MemoryError (both the
    numpy-accelerated ``_load_signal`` path and the vendored engine's
    ``read_wav`` fallback call ``wave.Wave_read.readframes`` to materialize
    the data chunk) must be refused with the clean exit-2 / ok:false contract,
    not an uncaught traceback and the wrong (1) exit code."""
    wav = _write_wav(tmp_path / "big.wav", n_channels=2, tone=True)

    def _boom(self, nframes=-1):
        raise MemoryError

    monkeypatch.setattr(wave.Wave_read, "readframes", _boom)
    obj = _cli_json_error(["run", "--stereo", wav, "--format", "json"], capsys)
    _assert_error_shape(obj, "usage_error")
    assert "too large" in obj["message"]


# --- CLI text path is unchanged: a plain "error:" line, no JSON -------------

def test_cli_text_still_prints_error_line(tmp_path, capsys):
    mono = _write_wav(tmp_path / "mono.wav", n_channels=1, tone=True)
    code = cli.main(["run", "--stereo", mono, "--format", "text"])
    cap = capsys.readouterr()
    assert code == 2
    assert cap.out == ""  # nothing on stdout in text mode
    assert cap.err.startswith("error:")
    assert "has one channel" in cap.err


# --- MCP tool: same error class -> same-error_code structured object --------

def test_mcp_missing_input():
    _assert_error_shape(mcp_server._run_tool(caller="x.wav"), "missing_input")


def test_mcp_mode_conflict():
    obj = mcp_server._run_tool(suite="barge-in", stereo=_bundled("01-hard-interruption"))
    _assert_error_shape(obj, "mode_conflict")


def test_mcp_mono_as_stereo(tmp_path):
    mono = _write_wav(tmp_path / "mono.wav", n_channels=1, tone=True)
    obj = mcp_server._run_tool(stereo=mono)
    _assert_error_shape(obj, "mono_as_stereo")


def test_mcp_sample_rate_mismatch(tmp_path):
    a = _write_wav(tmp_path / "a.wav", n_channels=1, sample_rate=16000, tone=True)
    b = _write_wav(tmp_path / "b.wav", n_channels=1, sample_rate=8000, tone=True)
    obj = mcp_server._run_tool(caller=a, agent=b)
    _assert_error_shape(obj, "sample_rate_mismatch")


def test_mcp_file_not_found(tmp_path):
    obj = mcp_server._run_tool(stereo=str(tmp_path / "nope.wav"))
    _assert_error_shape(obj, "file_not_found")


def test_mcp_unknown_suite():
    _assert_error_shape(mcp_server._run_tool(suite="no-such-suite"), "unknown_suite")


def test_mcp_memory_error_on_oversized_decode(tmp_path, monkeypatch):
    wav = _write_wav(tmp_path / "big.wav", n_channels=2, tone=True)

    def _boom(self, nframes=-1):
        raise MemoryError

    monkeypatch.setattr(wave.Wave_read, "readframes", _boom)
    obj = mcp_server._run_tool(stereo=wav)
    _assert_error_shape(obj, "usage_error")
    assert "too large" in obj["message"]


def test_mcp_not_scorable(tmp_path):
    """A well-formed recording with no scorable event is the structured error on
    the MCP surface (the CLI keeps its not-scorable ENVELOPE + exit 2, pinned
    elsewhere)."""
    wav = _silent_caller_stereo(tmp_path / "silent.wav")
    obj = mcp_server._run_tool(stereo=wav, expect="yield")
    _assert_error_shape(obj, "not_scorable")
    assert "caller speech" in obj["message"]


# --- MCP messages speak the tool's vocabulary, not CLI flags ----------------

def test_mcp_message_uses_parameter_names_not_flags(tmp_path):
    mono = _write_wav(tmp_path / "mono.wav", n_channels=1, tone=True)
    obj = mcp_server._run_tool(stereo=mono)
    assert "--" not in obj["message"]          # no CLI flag survives
    assert "stereo" in obj["message"]          # rewritten to the param name
    # the same underlying message on the CLI still shows the flag
    cli_obj = errors.cli_error(ValueError(
        "--stereo file has one channel; pass --caller and --agent as two mono files."))
    assert "--stereo" in cli_obj["message"]


# --- CLI and MCP agree on the error_code for the same class -----------------

@pytest.mark.parametrize("build", [
    lambda p: (["run", "--stereo", _write_wav(p / "m.wav", n_channels=1, tone=True),
                "--format", "json"],
               {"stereo": _write_wav(p / "m2.wav", n_channels=1, tone=True)}),
])
def test_cli_and_mcp_same_error_code(tmp_path, build, capsys):
    argv, mcp_kwargs = build(tmp_path)
    cli_obj = _cli_json_error(argv, capsys)
    mcp_obj = mcp_server._run_tool(**mcp_kwargs)
    assert cli_obj["error_code"] == mcp_obj["error_code"] == "mono_as_stereo"
    assert cli_obj["ok"] is mcp_obj["ok"] is False
    assert cli_obj["exit_code"] == mcp_obj["exit_code"] == 2


# --- SUCCESS path is byte-identical on both surfaces (no ok key) ------------

def test_cli_success_envelope_byte_identical(capsys):
    code = cli.main(["run", "--suite", "barge-in", "--format", "json"])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert "ok" not in env
    assert json.dumps(env, sort_keys=True) == json.dumps(
        run_suite(suite="barge-in"), sort_keys=True)


def test_cli_success_single_envelope_byte_identical(capsys):
    wav = _bundled("01-hard-interruption")
    code = cli.main(["run", "--stereo", wav, "--expect", "yield", "--format", "json"])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert "ok" not in env
    assert json.dumps(env, sort_keys=True) == json.dumps(
        run_single(stereo=wav, expect="yield"), sort_keys=True)


def test_mcp_success_envelope_byte_identical():
    env = mcp_server._run_tool(suite="barge-in", stack="generic")
    assert "ok" not in env
    assert json.dumps(env, sort_keys=True) == json.dumps(
        run_suite(suite="barge-in", stack="generic"), sort_keys=True)


# --- the schema file itself is the published, stable contract ---------------

def test_error_schema_id_and_slugs_are_stable():
    schema = _error_schema()
    assert schema["$id"] == "https://hotato.dev/schema/error.v1.json"
    enum = set(schema["properties"]["error_code"]["enum"])
    for required in ("missing_input", "mono_as_stereo", "sample_rate_mismatch",
                     "file_not_found", "unknown_suite", "not_scorable",
                     "backend_unavailable"):
        assert required in enum
    # the code table stays in sync with the shipped schema
    assert set(errors.ERROR_CODES) == enum


def test_error_schema_rejects_unknown_code():
    obj = errors.error_object("missing_input", "x")
    obj["error_code"] = "totally_made_up"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=obj, schema=_error_schema())
