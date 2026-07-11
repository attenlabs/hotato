"""The optional transcript, exposed through the MCP scoring tool.

Mirrors the honesty invariants proven for the CLI/module surface
(tests/test_transcribe.py) at the MCP boundary:

  1. OFF BY DEFAULT, ZERO COST: ``transcribe`` defaults to False; a call that
     omits it (or passes it explicitly False) is byte-identical to one made
     before this parameter existed.
  2. MISSING EXTRA -> CLEAN REFUSAL, NOT A CRASH: with ``transcribe=True`` and
     the ``[transcribe]`` extra absent, the tool returns the SAME structured
     error envelope (schema/error.v1.json, error_code "backend_unavailable")
     every other optional-extra failure uses -- never a raw traceback.
  3. CONTEXT ONLY, BYTE-IDENTICAL TIMING: with the extra present (simulated
     here via a monkeypatched ``hotato.transcribe.transcribe``, so the test
     needs no real faster-whisper install), the envelope gains exactly two
     additive keys (top-level ``transcript``, per-event ``transcript_context``)
     and every existing timing/verdict field is byte-for-byte unchanged.
  4. suite is not yet supported: it is a clean usage error, not a crash.
  5. the ``voice_eval_run`` tool registered on the built server plumbs
     ``transcribe`` through to the same behaviour.
"""
from __future__ import annotations

import math
import struct
import sys
import types
import wave

import pytest

from hotato import mcp_server as m
from hotato import transcribe as _transcribe_mod
from tests import _trial_audio as ta


def _faster_whisper_installed() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def _write_mono(path, active_windows, total_sec, freq, rate=16000, amp=12000):
    n = int(total_sec * rate)
    samples = [0] * n
    for s, e in active_windows:
        a, b = int(s * rate), min(int(e * rate), n)
        for i in range(a, b):
            samples[i] = int(amp * math.sin(2 * math.pi * freq * i / rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<%dh" % n, *samples))
    return str(path)


def _fake_transcribe(text, start, end, *, model="stub", device="cpu"):
    """Build a canned two-arg-compatible stand-in for
    ``hotato.transcribe.transcribe`` returning one segment."""

    def _fn(path, model=model, device=device, **kwargs):
        return _transcribe_mod.Transcript(
            text=text,
            segments=[_transcribe_mod.TranscriptSegment(start=start, end=end, text=text)],
            language="en",
            model=model,
            device=device,
            compute_type="int8",
        )

    return _fn


# --------------------------------------------------------------------------- #
# 1. off by default, zero cost.
# --------------------------------------------------------------------------- #

def test_transcribe_absent_and_explicit_false_are_byte_identical(tmp_path):
    wav = str(tmp_path / "call.wav")
    ta.yielding_call(wav, onset=2.0, total=6.0)

    default = m._run_tool(stereo=wav)
    explicit_false = m._run_tool(stereo=wav, transcribe=False)

    assert "transcript" not in default
    assert "transcript_context" not in default["events"][0]
    assert default == explicit_false


def test_importing_mcp_server_never_imports_faster_whisper():
    """transcribe=False is the wire default; the extra must never load unless
    a caller opts in, exactly like neural/diarize."""
    import subprocess

    code = (
        "import sys\n"
        "import hotato.mcp_server\n"
        "assert 'faster_whisper' not in sys.modules, "
        "'hotato.mcp_server imported faster_whisper at import time'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# 2. missing extra -> the standard clean refusal envelope, never a crash.
# --------------------------------------------------------------------------- #

def test_transcribe_true_missing_extra_is_clean_refusal_not_crash(tmp_path):
    if _faster_whisper_installed():
        pytest.skip("faster-whisper is installed here; the missing-extra path is not exercisable")
    wav = str(tmp_path / "call.wav")
    ta.yielding_call(wav)

    out = m._run_tool(stereo=wav, transcribe=True)

    assert out.get("ok") is False
    assert out["error_code"] == "backend_unavailable"
    assert out["exit_code"] == 2
    msg = out["message"].lower()
    assert "pip install" in msg
    assert "hotato[transcribe]" in msg


# --------------------------------------------------------------------------- #
# 3. context only: additive keys, byte-identical timing.
# --------------------------------------------------------------------------- #

def test_transcribe_true_attaches_context_without_altering_timing(tmp_path, monkeypatch):
    wav = str(tmp_path / "call.wav")
    ta.yielding_call(wav, onset=2.0, total=6.0)

    baseline = m._run_tool(stereo=wav)

    monkeypatch.setattr(
        _transcribe_mod, "transcribe",
        _fake_transcribe("hello there how can I help", start=2.0, end=2.6),
    )
    out = m._run_tool(stereo=wav, transcribe=True)

    assert out.get("ok") is not False
    assert "transcript" in out
    block = out["transcript"]
    assert block["text"] == "hello there how can I help"
    assert block["model"] == "stub"
    note = block["note"].lower()
    assert "context" in note
    assert "did_yield" in note  # names exactly what it never touches

    ev = out["events"][0]
    assert ev["transcript_context"]["text"] == "hello there how can I help"

    # Byte-identical timing/verdict everywhere except the two new keys.
    assert set(out.keys()) - set(baseline.keys()) == {"transcript"}
    for key, value in baseline.items():
        if key == "events":
            continue
        assert out[key] == value, key
    for base_ev, new_ev in zip(baseline["events"], out["events"]):
        stripped = dict(new_ev)
        stripped.pop("transcript_context", None)
        assert stripped == base_ev


def test_transcribe_false_after_true_still_matches_original_baseline(tmp_path, monkeypatch):
    """Requesting transcribe never has a lingering side effect on a later
    transcribe=False call over the same file."""
    wav = str(tmp_path / "call.wav")
    ta.yielding_call(wav)
    baseline = m._run_tool(stereo=wav)

    monkeypatch.setattr(
        _transcribe_mod, "transcribe", _fake_transcribe("hi", start=2.0, end=2.2)
    )
    m._run_tool(stereo=wav, transcribe=True)
    again = m._run_tool(stereo=wav, transcribe=False)
    assert again == baseline


# --------------------------------------------------------------------------- #
# 4. suite is not yet supported: a clean usage error, never a crash.
# --------------------------------------------------------------------------- #

def test_transcribe_with_suite_is_a_clean_usage_error():
    out = m._run_tool(suite="barge-in", transcribe=True)
    assert out.get("ok") is False
    assert out["error_code"] == "usage_error"
    assert "suite" in out["message"].lower()


# --------------------------------------------------------------------------- #
# caller+agent dual-mono: each side transcribed separately and role-tagged.
# --------------------------------------------------------------------------- #

def test_transcribe_caller_agent_tags_roles_and_sorts_by_time(tmp_path, monkeypatch):
    caller_wav = _write_mono(tmp_path / "caller.wav", [(2.0, 6.0)], 6.0, 300.0)
    agent_wav = _write_mono(tmp_path / "agent.wav", [(0.2, 2.3)], 6.0, 600.0)

    import os as _os

    def fake(path, model="base.en", device="auto", **kwargs):
        if _os.path.basename(str(path)) == "caller.wav":
            return _transcribe_mod.Transcript(
                text="need help", segments=[
                    _transcribe_mod.TranscriptSegment(start=2.0, end=2.5, text="need help")],
                language="en", model="stub", device="cpu", compute_type="int8")
        return _transcribe_mod.Transcript(
            text="sure thing", segments=[
                _transcribe_mod.TranscriptSegment(start=0.5, end=1.0, text="sure thing")],
            language="en", model="stub", device="cpu", compute_type="int8")

    monkeypatch.setattr(_transcribe_mod, "transcribe", fake)

    out = m._run_tool(caller=caller_wav, agent=agent_wav, transcribe=True)
    assert out.get("ok") is not False
    segs = out["transcript"]["segments"]
    assert [s["role"] for s in segs] == ["agent", "caller"]  # sorted by start
    assert out["transcript"]["text"] == "sure thing need help"


# --------------------------------------------------------------------------- #
# 5. the registered voice_eval_run tool plumbs transcribe through.
# --------------------------------------------------------------------------- #

class _FakeFastMCP:
    def __init__(self, name):
        self.name = name
        self.tools = {}

    def tool(self, name=None, description=None):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn
        return deco

    def run(self):  # pragma: no cover
        raise AssertionError("the stdio transport must not start in a test")


@pytest.fixture()
def fake_mcp():
    saved = {k: sys.modules.get(k)
             for k in ("mcp", "mcp.server", "mcp.server.fastmcp")}
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FakeFastMCP
    server_mod.fastmcp = fastmcp_mod
    mcp_mod.server = server_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = server_mod
    sys.modules["mcp.server.fastmcp"] = fastmcp_mod
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_voice_eval_run_tool_wires_transcribe_through(fake_mcp, tmp_path, monkeypatch):
    wav = str(tmp_path / "call.wav")
    ta.yielding_call(wav, onset=2.0, total=6.0)
    monkeypatch.setattr(
        _transcribe_mod, "transcribe", _fake_transcribe("hi there", start=2.0, end=2.4)
    )

    server = m.build_server()
    out = server.tools["voice_eval_run"](stereo=wav, transcribe=True)
    assert out.get("ok") is not False
    assert out["transcript"]["text"] == "hi there"
    assert out["events"][0]["transcript_context"]["text"] == "hi there"

    # and the default (no transcribe kw) still carries no transcript key
    plain = server.tools["voice_eval_run"](stereo=wav)
    assert "transcript" not in plain
