"""``errors.rename_no_replace``: atomic no-replace publish for files and dirs.

Pinned here:

  * a file or directory source is published atomically at the destination and
    the source name is gone afterwards;
  * an existing destination is REFUSED with ``FileExistsError`` -- never
    silently replaced -- and the source is left in place for cleanup (the
    check-then-``os.replace`` TOCTOU this helper closes);
  * both capability legs behave identically: the libc no-replace rename
    syscall when the runtime exposes one, and the guarded fallback
    (``os.link``+``os.unlink`` for files, mkdir-claim-then-rename for
    directories) when it does not -- the fallback is forced via monkeypatch so
    it is exercised on every platform;
  * a libc symbol whose kernel/filesystem lacks the no-replace flag (the
    callable reports False) falls back cleanly instead of failing.
"""

import os

import pytest

from hotato import errors


def _make_file(tmp_path, name, content=b"payload"):
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def _make_dir(tmp_path, name, filename="child.txt", content="payload"):
    path = tmp_path / name
    path.mkdir()
    (path / filename).write_text(content, encoding="utf-8")
    return str(path)


def _no_syscall(monkeypatch):
    monkeypatch.setattr(errors, "_libc_rename_no_replace", lambda: None)


_LEGS = ["default", "fallback"]


@pytest.fixture(params=_LEGS)
def leg(request, monkeypatch):
    """Run each behavior test on the default leg AND with the libc syscall
    forced away, so the guarded fallback is covered even where the syscall
    exists (and vice versa the default leg covers the syscall where it does)."""
    if request.param == "fallback":
        _no_syscall(monkeypatch)
    return request.param


def test_publishes_a_file_and_consumes_the_source(tmp_path, leg):
    src = _make_file(tmp_path, "src.json", b"{}")
    dest = str(tmp_path / "out.json")
    errors.rename_no_replace(src, dest)
    assert not os.path.exists(src)
    with open(dest, "rb") as fh:
        assert fh.read() == b"{}"


def test_publishes_a_directory_and_consumes_the_source(tmp_path, leg):
    src = _make_dir(tmp_path, "bundle-tmp")
    dest = str(tmp_path / "bundle")
    errors.rename_no_replace(src, dest)
    assert not os.path.exists(src)
    with open(os.path.join(dest, "child.txt"), encoding="utf-8") as fh:
        assert fh.read() == "payload"


def test_refuses_an_existing_file_destination(tmp_path, leg):
    src = _make_file(tmp_path, "src.json", b"new")
    dest = _make_file(tmp_path, "out.json", b"already-published")
    with pytest.raises(FileExistsError):
        errors.rename_no_replace(src, dest)
    # Refusal never clobbers the destination and leaves the source for the
    # caller's cleanup path.
    with open(dest, "rb") as fh:
        assert fh.read() == b"already-published"
    assert os.path.exists(src)


def test_refuses_an_existing_directory_destination(tmp_path, leg):
    src = _make_dir(tmp_path, "bundle-tmp", content="new")
    dest = _make_dir(tmp_path, "bundle", content="already-published")
    with pytest.raises(FileExistsError):
        errors.rename_no_replace(src, dest)
    with open(os.path.join(dest, "child.txt"), encoding="utf-8") as fh:
        assert fh.read() == "already-published"
    assert os.path.exists(src)


def test_refuses_an_existing_empty_directory_destination(tmp_path, leg):
    # The exact silent-clobber case of a raw os.replace/os.rename on POSIX:
    # a racing publisher just created the (still empty) destination.
    src = _make_dir(tmp_path, "bundle-tmp")
    dest = tmp_path / "bundle"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        errors.rename_no_replace(src, str(dest))
    assert os.path.exists(src)
    assert os.listdir(dest) == []


def test_flag_unsupported_syscall_falls_back_cleanly(tmp_path, monkeypatch):
    # A libc symbol backed by a kernel/filesystem without the no-replace flag
    # reports False; the publish must complete through the guarded fallback.
    monkeypatch.setattr(
        errors, "_libc_rename_no_replace",
        lambda: (lambda source, destination: False),
    )
    src = _make_file(tmp_path, "src.json")
    dest = str(tmp_path / "out.json")
    errors.rename_no_replace(src, dest)
    assert os.path.exists(dest) and not os.path.exists(src)


def test_missing_source_raises_the_normal_oserror(tmp_path, leg):
    with pytest.raises(OSError):
        errors.rename_no_replace(str(tmp_path / "absent"),
                                 str(tmp_path / "out"))
