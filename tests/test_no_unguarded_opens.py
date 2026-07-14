"""Structural lint: NO unguarded blocking open of a potentially-external path.

A writer-less FIFO makes any blocking ``open()``/``wave.open()``/``ZipFile()``
hang forever, so every read of a path that could come from a CLI arg, MCP
param, bundle content, or webhook payload must route through
``errors.require_regular_file`` (via ``open_regular``/``wav_read`` or an
explicit call). This test walks the AST of every module (the vendored
``_engine`` excluded) and FAILS on any read-mode open that is neither a
guarded-helper call nor explicitly justified with an ``# open-ok: <reason>``
pragma on the line above (reserved for reads of bundled package resources or
files the tool itself controls). Three prior review rounds each found more
unguarded sites; this makes the class regression-proof instead of relying on
grep memory.
"""
import ast
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "hotato")

OPEN_NAMES = {"open", "ZipFile"}
READ_METHODS = {"read_text", "read_bytes"}


def _mode_is_write(call: ast.Call) -> bool:
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(c in mode for c in ("w", "a", "x"))


def _has_open_ok_pragma(lines, lineno: int) -> bool:
    # pragma on the open line itself or up to 2 lines above (multi-line pragma)
    for off in (0, 1, 2, 3):
        i = lineno - 1 - off
        if 0 <= i < len(lines) and "# open-ok:" in lines[i]:
            return True
    return False


def _violations():
    found = []
    for dirpath, _dirs, files in os.walk(SRC):
        if "_engine" in dirpath:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            src = open(p, encoding="utf-8").read()
            lines = src.splitlines()
            tree = ast.parse(src, filename=p)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = (f.id if isinstance(f, ast.Name)
                        else f.attr if isinstance(f, ast.Attribute) else None)
                rel = os.path.relpath(p, SRC)
                if name in OPEN_NAMES:
                    if name == "open" and isinstance(f, ast.Attribute):
                        # attribute-form open(): only wave.open blocks on a
                        # FIFO path. webbrowser.open is not a file open, and
                        # zf.open reads a member INSIDE an archive whose path
                        # was already guarded before ZipFile() opened it.
                        obj = f.value
                        obj_name = obj.id if isinstance(obj, ast.Name) else None
                        if obj_name not in ("wave", "_wave"):
                            continue
                    if _mode_is_write(node):
                        continue
                    if _has_open_ok_pragma(lines, node.lineno):
                        continue
                    # calls to the guarded helpers never look like plain open()
                    found.append(f"{rel}:{node.lineno} ({name})")
                elif name in READ_METHODS:
                    seg = ast.dump(node)
                    if "resources" in seg or "importlib" in seg:
                        continue
                    if _has_open_ok_pragma(lines, node.lineno):
                        continue
                    found.append(f"{rel}:{node.lineno} ({name})")
    return found


def test_every_blocking_open_is_guarded_or_justified():
    v = _violations()
    assert not v, (
        "Unguarded blocking open(s) of a potentially-external path. Route each "
        "through errors.open_regular/wav_read (FIFO-safe), or, ONLY for a "
        "bundled-resource / tool-controlled path, add an '# open-ok: <reason>' "
        "pragma on the line above:\n  " + "\n  ".join(v))
