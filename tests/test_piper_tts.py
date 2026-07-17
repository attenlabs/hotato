from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from hotato.piper_tts import PiperCallerTTS, PiperTTSError


def _executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _inputs(tmp_path: Path):
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"bounded-fixture-model")
    config = tmp_path / "voice.onnx.json"
    config.write_text(json.dumps({"audio": {"sample_rate": 22_050}}), encoding="utf-8")
    return model, config


def test_piper_returns_raw_pcm_and_digest_only_provenance(tmp_path):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper fixture",
        """
import sys
text = sys.stdin.buffer.read()
sys.stderr.write("local diagnostic only")
sys.stdout.buffer.write(b"\\x01\\x00" * max(1, len(text)))
""",
    )
    with PiperCallerTTS(
        str(model), str(config), command=str(executable), voice="fixture-a"
    ) as tts:
        private_root = tts._temporary.name
        result = tts.synthesize("hello")
        assert result["sample_rate_hz"] == 22_050
        assert result["pcm_s16le"] == b"\x01\x00" * 6
        assert result["provider"] == "piper-local-cli"
        assert result["model"].startswith("sha256:")
        assert result["voice"] == "fixture-a"
        encoded = json.dumps({k: v for k, v in result.items() if k != "pcm_s16le"})
        assert str(model) not in encoded
        assert str(config) not in encoded
        assert private_root not in encoded
        assert "local diagnostic only" not in encoded
        assert result["settings"]["stderr_bytes"] == len("local diagnostic only")
    assert not os.path.exists(private_root)


def test_piper_rejects_symlink_inputs_and_sample_rate_outside_pcm_contract(tmp_path):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper", "import sys\nsys.stdout.buffer.write(b'\\x00\\x00')\n"
    )
    link = tmp_path / "model-link.onnx"
    link.symlink_to(model)
    with pytest.raises(ValueError, match="symbolic link"):
        PiperCallerTTS(str(link), str(config), command=str(executable))

    config.write_text(json.dumps({"audio": {"sample_rate": 500}}), encoding="utf-8")
    with pytest.raises(ValueError, match="sample_rate"):
        PiperCallerTTS(str(model), str(config), command=str(executable))


def test_piper_fails_closed_on_output_overflow(tmp_path):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper", "import sys\nsys.stdout.buffer.write(b'x' * 10000)\n"
    )
    with PiperCallerTTS(
        str(model), str(config), command=str(executable), max_output_bytes=100
    ) as tts:
        with pytest.raises(PiperTTSError, match="output-stream"):
            tts.synthesize("hello")


def test_piper_timeout_is_bounded_and_secret_environment_is_not_inherited(
    tmp_path, monkeypatch
):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper",
        """
import os, sys, time
if os.environ.get("SHOULD_NOT_REACH_PIPER"):
    sys.stderr.write(os.environ["SHOULD_NOT_REACH_PIPER"])
time.sleep(2)
sys.stdout.buffer.write(b"\\x00\\x00")
""",
    )
    monkeypatch.setenv("SHOULD_NOT_REACH_PIPER", "private-token")
    with PiperCallerTTS(
        str(model), str(config), command=str(executable), timeout_seconds=0.05
    ) as tts:
        with pytest.raises(PiperTTSError, match="timeout") as caught:
            tts.synthesize("hello")
    assert "private-token" not in str(caught.value)


def test_piper_refuses_odd_length_and_empty_raw_output(tmp_path):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper", "import sys\nsys.stdout.buffer.write(b'x')\n"
    )
    with PiperCallerTTS(str(model), str(config), command=str(executable)) as tts:
        with pytest.raises(PiperTTSError, match="even-length"):
            tts.synthesize("hello")


def test_piper_refuses_executable_that_changes_during_synthesis(tmp_path):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper",
        """
import sys
with open(sys.argv[0], "a", encoding="utf-8") as stream:
    stream.write("# changed\\n")
sys.stdout.buffer.write(b"\\x00\\x00")
""",
    )
    with PiperCallerTTS(str(model), str(config), command=str(executable)) as tts:
        with pytest.raises(PiperTTSError, match="changed during"):
            tts.synthesize("hello")


def test_piper_executes_private_content_bound_copy_after_source_swap(tmp_path):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper",
        "import sys\nsys.stdout.buffer.write(b'\\x01\\x00')\n",
    )
    with PiperCallerTTS(str(model), str(config), command=str(executable)) as tts:
        _executable(
            executable,
            "import sys\nsys.stdout.buffer.write(b'evil')\n",
        )
        result = tts.synthesize("hello")
    assert result["pcm_s16le"] == b"\x01\x00"


def test_piper_detects_staged_model_mutation_by_subprocess(tmp_path):
    model, config = _inputs(tmp_path)
    executable = _executable(
        tmp_path / "piper",
        """
import pathlib, sys
model_path = pathlib.Path(sys.argv[sys.argv.index("--model") + 1])
model_path.write_bytes(b"changed")
sys.stdout.buffer.write(b"\\x00\\x00")
""",
    )
    with PiperCallerTTS(str(model), str(config), command=str(executable)) as tts:
        with pytest.raises(PiperTTSError, match="model changed during"):
            tts.synthesize("hello")
