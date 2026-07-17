"""Bounded local Piper TTS adapter for caller programs.

The adapter deliberately treats Piper as an untrusted local subprocess.  It
does not use a shell, does not inherit the operator's environment, stages
regular model/config files into a private directory, bounds both output
streams, and records digest-only provenance.  A successful invocation means
only that Piper produced structurally valid mono PCM16LE; transport delivery
is established separately by the caller session.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, Mapping, Optional, Tuple


class PiperTTSError(RuntimeError):
    """Piper could not produce bounded, structurally valid PCM."""


def _positive_int(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be in [{low}, {high}]")
    return value


def _positive_number(value: Any, label: str, high: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= high
    ):
        raise ValueError(f"{label} must be in (0, {high}]")
    return float(value)


def _bounded_label(value: Any, label: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")
    return value


def _open_regular_descriptor(path: Path, maximum: int) -> Tuple[int, os.stat_result]:
    raw = os.fspath(path)
    before = os.lstat(raw)
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{raw!r} must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{raw!r} must be a regular file")
    if before.st_size > maximum:
        raise ValueError(f"{raw!r} exceeds the {maximum}-byte limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(raw, flags)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or opened.st_size > maximum
    ):
        os.close(descriptor)
        raise ValueError(f"{raw!r} changed or is not a bounded regular file")
    return descriptor, opened


def _copy_regular_bounded(
    source: Path, destination: Path, maximum: int, *, mode: int = 0o600
) -> str:
    descriptor, opened = _open_regular_descriptor(source, maximum)
    digest = hashlib.sha256()
    copied = 0
    try:
        output_descriptor = os.open(
            os.fspath(destination),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            mode,
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(output_descriptor, mode)
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > maximum:
                    raise ValueError(f"{os.fspath(source)!r} exceeds the {maximum}-byte limit")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(output_descriptor, view)
                    view = view[written:]
            os.fsync(output_descriptor)
        finally:
            os.close(output_descriptor)
        after = os.fstat(descriptor)
        if (
            copied != opened.st_size
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError(f"{os.fspath(source)!r} changed while it was staged")
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def _hash_regular_bounded(path: Path, maximum: int) -> str:
    descriptor, opened = _open_regular_descriptor(path, maximum)
    digest = hashlib.sha256()
    consumed = 0
    try:
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum - consumed + 1))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > maximum:
                raise ValueError(f"{os.fspath(path)!r} exceeds the {maximum}-byte limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if consumed != opened.st_size or after.st_mtime_ns != opened.st_mtime_ns:
            raise ValueError(f"{os.fspath(path)!r} changed while it was hashed")
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


class _BoundedReader(threading.Thread):
    def __init__(self, stream: BinaryIO, maximum: int, overflow: threading.Event):
        super().__init__(daemon=True)
        self.stream = stream
        self.maximum = maximum
        self.overflow = overflow
        self.data = bytearray()
        self.total = 0
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(64 * 1024)
                if not chunk:
                    break
                self.total += len(chunk)
                room = max(0, self.maximum + 1 - len(self.data))
                if room:
                    self.data.extend(chunk[:room])
                if self.total > self.maximum:
                    self.overflow.set()
        except BaseException as exc:  # subprocess pipe boundary
            self.error = exc


class _BoundedInputWriter(threading.Thread):
    """Write bounded caller text without letting a non-reader defeat timeout."""

    def __init__(self, stream: BinaryIO, value: bytes):
        super().__init__(daemon=True)
        self.stream = stream
        self.value = value
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            self.stream.write(self.value)
            self.stream.flush()
        except BrokenPipeError:
            pass
        except BaseException as exc:  # subprocess pipe boundary
            self.error = exc
        finally:
            try:
                self.stream.close()
            except OSError:
                pass


def _kill_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


class PiperCallerTTS:
    """Local Piper CLI adapter returning mono PCM16LE plus digest provenance."""

    def __init__(
        self,
        model_path: str,
        config_path: str,
        *,
        command: str = "piper",
        voice: str = "default",
        timeout_seconds: float = 60.0,
        max_text_bytes: int = 64 * 1024,
        max_output_bytes: int = 32 * 1024 * 1024,
        max_diagnostic_bytes: int = 64 * 1024,
        max_model_bytes: int = 2 * 1024 * 1024 * 1024,
        max_config_bytes: int = 1024 * 1024,
        max_executable_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.timeout_seconds = _positive_number(timeout_seconds, "timeout_seconds", 600)
        self.max_text_bytes = _positive_int(
            max_text_bytes, "max_text_bytes", 1, 1024 * 1024
        )
        self.max_output_bytes = _positive_int(
            max_output_bytes, "max_output_bytes", 2, 1024 * 1024 * 1024
        )
        self.max_diagnostic_bytes = _positive_int(
            max_diagnostic_bytes, "max_diagnostic_bytes", 1, 1024 * 1024
        )
        max_model_bytes = _positive_int(
            max_model_bytes, "max_model_bytes", 1, 4 * 1024 * 1024 * 1024
        )
        max_config_bytes = _positive_int(
            max_config_bytes, "max_config_bytes", 1, 64 * 1024 * 1024
        )
        max_executable_bytes = _positive_int(
            max_executable_bytes, "max_executable_bytes", 1, 2 * 1024 * 1024 * 1024
        )
        self._max_model_bytes = max_model_bytes
        self._max_config_bytes = max_config_bytes
        self.voice = _bounded_label(voice, "voice")

        resolved = shutil.which(command)
        if resolved is None:
            raise ValueError(f"Piper command {command!r} was not found")
        executable_source = Path(os.path.realpath(resolved))
        self._max_executable_bytes = max_executable_bytes
        self._temporary = tempfile.TemporaryDirectory(prefix="hotato-piper-")
        root = Path(self._temporary.name)
        # Execute a privately staged, content-addressed copy.  Hashing an
        # external pathname and later passing that pathname to Popen leaves a
        # swap window in which different code can be executed and then
        # restored before the post-run hash.  The private copy closes that
        # window while retaining support for native binaries and shebang
        # entrypoints.
        self._staged_executable = root / ("piper" + executable_source.suffix)
        self._staged_model = root / "voice.onnx"
        self._staged_config = root / "voice.onnx.json"
        try:
            self._executable_sha256 = _copy_regular_bounded(
                executable_source,
                self._staged_executable,
                max_executable_bytes,
                mode=0o700,
            )
            self._model_sha256 = _copy_regular_bounded(
                Path(model_path), self._staged_model, max_model_bytes
            )
            self._config_sha256 = _copy_regular_bounded(
                Path(config_path), self._staged_config, max_config_bytes
            )
            with self._staged_config.open("r", encoding="utf-8") as stream:  # internal staged file
                config = json.load(stream)
            try:
                sample_rate = config["audio"]["sample_rate"]
            except (KeyError, TypeError) as exc:
                raise ValueError("Piper config must contain audio.sample_rate") from exc
            self.sample_rate_hz = _positive_int(
                sample_rate, "Piper audio.sample_rate", 8_000, 192_000
            )
        except BaseException:
            self._temporary.cleanup()
            raise
        self._closed = False
        self._process_lock = threading.Lock()
        self._active_process: Optional[subprocess.Popen] = None

    def __enter__(self) -> "PiperCallerTTS":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.abort()
            self._temporary.cleanup()

    def abort(self) -> None:
        """Stop an in-flight Piper process without waiting for normal output."""

        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            _kill_process(process)

    def synthesize(self, text: str) -> Mapping[str, Any]:
        if self._closed:
            raise PiperTTSError("Piper adapter is closed")
        if not isinstance(text, str) or not text:
            raise ValueError("Piper text must be a non-empty string")
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_text_bytes:
            raise ValueError(f"Piper text exceeds {self.max_text_bytes} UTF-8 bytes")
        if b"\x00" in encoded:
            raise ValueError("Piper text contains a NUL byte")

        command = [
            os.fspath(self._staged_executable),
            "--model",
            os.fspath(self._staged_model),
            "--config",
            os.fspath(self._staged_config),
            "--output_raw",
        ]
        if _hash_regular_bounded(
            self._staged_executable, self._max_executable_bytes
        ) != self._executable_sha256:
            raise PiperTTSError("staged Piper executable changed after adapter initialization")
        try:
            model_identity = _hash_regular_bounded(
                self._staged_model, self._max_model_bytes
            )
            config_identity = _hash_regular_bounded(
                self._staged_config, self._max_config_bytes
            )
        except (OSError, ValueError) as exc:
            raise PiperTTSError("staged Piper inputs became unavailable") from exc
        if model_identity != self._model_sha256:
            raise PiperTTSError("staged Piper model changed after adapter initialization")
        if config_identity != self._config_sha256:
            raise PiperTTSError("staged Piper config changed after adapter initialization")
        environment: Dict[str, str] = {
            "HOME": self._temporary.name,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        for name in ("SYSTEMROOT", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._temporary.name,
            env=environment,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
        with self._process_lock:
            self._active_process = process
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        overflow = threading.Event()
        stdout = _BoundedReader(process.stdout, self.max_output_bytes, overflow)
        stderr = _BoundedReader(process.stderr, self.max_diagnostic_bytes, overflow)
        stdin = _BoundedInputWriter(process.stdin, encoded + b"\n")
        stdout.start()
        stderr.start()
        stdin.start()
        try:
            deadline = time.monotonic() + self.timeout_seconds
            timed_out = False
            while process.poll() is None:
                if overflow.is_set():
                    _kill_process(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kill_process(process)
                    break
                time.sleep(0.01)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                _kill_process(process)
                process.wait(timeout=5)
            stdout.join(timeout=5)
            stderr.join(timeout=5)
            stdin.join(timeout=5)
            process.stdout.close()
            process.stderr.close()
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None

        if (
            stdout.is_alive()
            or stderr.is_alive()
            or stdin.is_alive()
            or stdout.error
            or stderr.error
            or stdin.error
        ):
            raise PiperTTSError("Piper output capture did not terminate cleanly")
        diagnostic = bytes(stderr.data)
        diagnostic_sha256 = "sha256:" + hashlib.sha256(diagnostic).hexdigest()
        if timed_out:
            raise PiperTTSError(f"Piper exceeded the {self.timeout_seconds:g}-second timeout")
        if overflow.is_set():
            raise PiperTTSError("Piper exceeded a configured output-stream byte limit")
        if process.returncode != 0:
            raise PiperTTSError(
                f"Piper exited {process.returncode}; stderr_sha256={diagnostic_sha256}"
            )
        if _hash_regular_bounded(
            self._staged_executable, self._max_executable_bytes
        ) != self._executable_sha256:
            raise PiperTTSError("staged Piper executable changed during synthesis")
        try:
            model_identity = _hash_regular_bounded(
                self._staged_model, self._max_model_bytes
            )
            config_identity = _hash_regular_bounded(
                self._staged_config, self._max_config_bytes
            )
        except (OSError, ValueError) as exc:
            raise PiperTTSError("staged Piper inputs became unavailable during synthesis") from exc
        if model_identity != self._model_sha256:
            raise PiperTTSError("staged Piper model changed during synthesis")
        if config_identity != self._config_sha256:
            raise PiperTTSError("staged Piper config changed during synthesis")
        pcm = bytes(stdout.data)
        if not pcm or len(pcm) % 2:
            raise PiperTTSError("Piper output_raw was empty or not even-length PCM16LE")
        return {
            "pcm_s16le": pcm,
            "sample_rate_hz": self.sample_rate_hz,
            "provider": "piper-local-cli",
            "model": self._model_sha256,
            "voice": self.voice,
            "settings": {
                "config_sha256": self._config_sha256,
                "executable_observed_sha256": self._executable_sha256,
                "encoding": "pcm_s16le",
                "channels": 1,
                "stderr_bytes": stderr.total,
                "stderr_sha256": diagnostic_sha256,
            },
        }


__all__ = ["PiperCallerTTS", "PiperTTSError"]
