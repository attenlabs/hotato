"""Docs-to-runtime exit-code binding.

Regression guard for the 1.9.0-era README defect: the Quickstart said
``start --demo`` "exits `1`" while the command exits 0 by design --
docs/START.md documents the 0, and the gate that exits 1 is
``hotato contract verify contracts/``. These tests hold every surface that
narrates the demo's exit code to the exit code the runtime produces, so
this class of drift reddens CI instead of shipping.
"""

import os
import re

from hotato import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _quickstart(text):
    m = re.search(r"(?ms)^## Quickstart$(.*?)^## ", text)
    assert m, "no ## Quickstart section found"
    return m.group(1)


def test_quickstart_never_attributes_exit_one_to_the_demo():
    """The shipped falsehood: 'scores the two bundled demo calls and exits
    `1`'. Any exit-1 claim inside Quickstart must sit on a line about the
    gate, never on a line about the demo."""
    for rel in ("README.md", "README.pypi.md"):
        for line in _quickstart(_read(rel)).splitlines():
            if re.search(r"exits? `?1`?", line):
                assert "demo" not in line.lower(), (
                    f"{rel} Quickstart attributes exit 1 to the demo; "
                    "`start --demo` exits 0 by design (the exit-1 gate is "
                    "`hotato contract verify contracts/`)"
                )


def test_start_md_still_documents_demo_exit_zero():
    assert re.search(r"start --demo itself exits 0", _read("docs/START.md")), (
        "docs/START.md no longer states that `start --demo` exits 0 -- if "
        "the demo's exit contract changed, update README.md, README.pypi.md, "
        "and this suite in the same commit"
    )


def test_start_demo_exit_code_matches_docs(tmp_path, monkeypatch):
    """The runtime side of the binding: the demo exits 0, as documented."""
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    rc = cli.main(["start", "--demo", "--dir", str(tmp_path)])
    assert rc == 0, (
        "start --demo no longer exits 0; docs/START.md and both README "
        "Quickstarts narrate exit 0 -- update them in the same commit"
    )
