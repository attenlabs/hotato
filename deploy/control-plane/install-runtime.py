#!/usr/bin/env python3
"""Copy host-private bootstrap files into least-privilege named volumes."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

SOURCE = Path("/source")
COPIES = (
    ("valkey.conf", Path("/out/valkey/valkey.conf"), 0o444, None, 1 << 20),
    ("valkey-password", Path("/out/valkey/valkey-password"), 0o444, None, 4096),
    ("livekit.yaml", Path("/out/livekit/livekit.yaml"), 0o444, None, 1 << 20),
    ("sip.yaml", Path("/out/sip/sip.yaml"), 0o444, None, 1 << 20),
    (
        "otelcol.yaml",
        Path("/out/otel/otelcol.yaml"),
        0o400,
        (10001, 10001),
        1 << 20,
    ),
    (
        "hotato-production-token",
        Path("/out/hotato/hotato-production-token"),
        0o400,
        (10001, 10001),
        4096,
    ),
    (
        "hotato-production-maintenance.json",
        Path("/out/hotato/hotato-production-maintenance.json"),
        0o400,
        (10001, 10001),
        1 << 20,
    ),
)
DIRECTORIES = (
    (Path("/out/otel-wal"), 0o700, (10001, 10001)),
)


def _read_regular(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path} must be a regular, non-symlink file")
    if info.st_size > limit:
        raise ValueError(f"{path} exceeds its {limit}-byte limit")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError(f"{path} changed before it was opened")
        data = b""
        while len(data) <= limit:
            chunk = os.read(descriptor, min(65_536, limit + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise ValueError(f"{path} exceeds its {limit}-byte limit")
        return data
    finally:
        os.close(descriptor)


def _atomic_install(path: Path, data: bytes, mode: int, owner: tuple[int, int] | None) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError(f"{path.parent} must be a mounted directory")
    descriptor, temporary = tempfile.mkstemp(prefix=".hotato-install-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        if owner is not None:
            os.fchown(descriptor, *owner)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _prepare_directory(
    path: Path, mode: int, owner: tuple[int, int] | None
) -> None:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{path} must be a mounted, non-symlink directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError(f"{path} changed before it was opened")
        os.fchmod(descriptor, mode)
        if owner is not None:
            os.fchown(descriptor, *owner)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install(source: Path = SOURCE) -> None:
    for path, mode, owner in DIRECTORIES:
        _prepare_directory(path, mode, owner)
    for name, destination, mode, owner, limit in COPIES:
        _atomic_install(destination, _read_regular(source / name, limit), mode, owner)


if __name__ == "__main__":
    install()
