"""Copy check for site text: no placeholder, roadmap or absence lines.
Usage: python3 autopilot/build/copycheck.py PATH [PATH ...] (directories or files). Exit 1 on any hit."""
import os
import re
import sys

BANNED = re.compile(r"coming soon|not yet|lorem ipsum|placeholder|\bTODO\b|\bTBD\b|roadmap|stay tuned|under construction", re.I)
EXTS = (".html", ".md", ".txt", ".json", ".xml")


def find(text):
    return [(n, m.group(0)) for n, line in enumerate(text.splitlines(), 1) for m in BANNED.finditer(line)]


def paths(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        if not os.path.isdir(root):
            raise FileNotFoundError(root)
        for d, _, files in os.walk(root):
            for f in files:
                if f.endswith(EXTS):
                    yield os.path.join(d, f)


def main(roots):
    hits = 0
    try:
        for p in paths(roots):
            for n, w in find(open(p, encoding="utf-8", errors="replace").read()):
                print(f"COPY {p}:{n}: {w}")
                hits += 1
    except FileNotFoundError as e:
        print(f"copycheck: no such path {e}")
        return 2
    print(f"copycheck: {hits} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["site"]))
