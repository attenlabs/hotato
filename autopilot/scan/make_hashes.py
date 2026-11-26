"""LOCAL helper: plaintext list (one token per line) -> hash lines.
Run on the owner's machine only; commit the output, never the input.
  python3 scan/make_hashes.py names.txt >> scan/banned.sha256
Tokens longer than 3 words are rejected (the scanner only matches 1-3 words).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leakscan import digest, normalize  # noqa: E402


def hash_lines(lines):
    out = []
    for raw in lines:
        tok = normalize(raw)
        if not tok:
            continue
        if len(tok.split()) > 3:
            raise ValueError("token longer than 3 words")
        out.append(digest(tok))
    return sorted(set(out))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_hashes.py PLAINTEXT_FILE")
    with open(sys.argv[1], encoding="utf-8") as fh:
        print("\n".join(hash_lines(fh)))
