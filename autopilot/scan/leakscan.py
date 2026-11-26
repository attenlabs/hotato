"""Leak scanner. Matches only SHA-256 hashes of banned tokens.

A token is a 1-3 word n-gram, case-folded, words = runs of letters/digits.
The hash file holds hashes only; no plaintext name is ever stored here.
Usage: python3 autopilot/scan/leakscan.py [--hashes FILE] [paths...]
Default file list: git ls-files; outside a git checkout (a Cloud Build upload has no .git), every
file under the current directory, skipping .git, __pycache__ and the Terraform cache.
Exit 0 clean, 1 hits, 2 usage error. Prints file:line and hash prefix only.
"""
import hashlib
import os
import re
import subprocess
import sys

WORD = re.compile(r"[^\W_]+", re.UNICODE)
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HASHES = os.path.join(HERE, "banned.sha256")


def words(text):
    return WORD.findall(text.casefold())


def ngrams(ws, n_max=3):
    for n in range(1, n_max + 1):
        for i in range(len(ws) - n + 1):
            yield " ".join(ws[i:i + n])


def digest(gram):
    return hashlib.sha256(gram.encode("utf-8")).hexdigest()


def normalize(token):
    """Canonical form of a banned token (used by the hash helper)."""
    return " ".join(words(token))


def load_hashes(path):
    out = set()
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", line):
            out.add(line)
    return out


def scan_text(text, hashes):
    """Return a list of (line_number, hash) for every banned n-gram."""
    hits = []
    if not hashes:
        return hits
    for no, line in enumerate(text.splitlines(), 1):
        for g in ngrams(words(line)):
            h = digest(g)
            if h in hashes:
                hits.append((no, h))
    return hits


SKIP_DIRS = {".git", "__pycache__", ".terraform", ".pytest_cache"}


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    except FileNotFoundError:
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return [p for p in out.stdout.splitlines() if p]
    files = []
    for d, dirs, names in os.walk("."):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        files += [os.path.relpath(os.path.join(d, n)) for n in sorted(names)]
    return files


def main(argv):
    hpath = DEFAULT_HASHES
    args = list(argv)
    if args[:1] == ["--hashes"]:
        if len(args) < 2:
            return 2
        hpath, args = args[1], args[2:]
    hashes = load_hashes(hpath)
    files = args or tracked_files()
    found = 0
    for f in files:
        try:
            text = open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for no, h in scan_text(text, hashes):
            print(f"LEAK {f}:{no} hash={h[:12]}")
            found += 1
    print(f"leakscan: {len(hashes)} hashes, {len(files)} files, {found} hits")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
