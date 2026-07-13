"""LF line endings must survive an autocrlf=true (Windows) checkout for the
files whose BYTES are an oracle.

Two byte oracles break under a CRLF rewrite:

  * the digest-pinned Failure Record reference kit -- its evidence sha256s are
    computed over the raw file bytes (``validate_record(root=...)``), so an
    added ``\\r`` changes every digest and the oracle rejects an untampered
    kit;
  * the shell scripts -- ``bash -n`` (and a real shell) choke on ``\\r``.

A committed ``.gitattributes`` pins these to ``eol=lf`` so a Windows checkout
cannot silently rewrite them. These tests fail loudly if that pin is dropped.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gitattributes_rules():
    path = os.path.join(REPO_ROOT, ".gitattributes")
    assert os.path.isfile(path), ".gitattributes is missing at the repo root"
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.lstrip().startswith("#")]


def test_gitattributes_pins_shell_scripts_to_lf():
    rules = _gitattributes_rules()
    assert any(r.startswith("*.sh") and "eol=lf" in r for r in rules), (
        "*.sh must be pinned to `eol=lf` so `bash -n` survives a CRLF checkout"
    )


def test_gitattributes_pins_the_failure_record_reference_kit_to_lf():
    rules = _gitattributes_rules()
    assert any("failure-record-reference" in r and "eol=lf" in r
               for r in rules), (
        "the digest-pinned Failure Record reference kit must be `eol=lf` so a "
        "CRLF checkout cannot change its evidence bytes and break the oracle"
    )


# Byte-oracle guard on disk today: these files must carry no CR, so a commit
# that introduces CRLF (even without a Windows checkout) trips here.
_LF_ONLY_TREES = (
    "tests/data/failure-record-reference",
    "deploy",
    "scripts",
)
_LF_ONLY_SUFFIXES = (".json", ".sh")


def test_byte_oracle_files_are_lf_only_on_disk():
    offenders = []
    for rel in _LF_ONLY_TREES:
        root = os.path.join(REPO_ROOT, rel)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(_LF_ONLY_SUFFIXES):
                    continue
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    if b"\r" in fh.read():
                        offenders.append(os.path.relpath(full, REPO_ROOT))
    assert not offenders, (
        "CR bytes present (would break the digest/bash byte oracle): "
        f"{sorted(offenders)}"
    )
