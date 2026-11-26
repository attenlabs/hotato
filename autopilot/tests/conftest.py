import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "jobs"), os.path.join(ROOT, "scan"), os.path.join(ROOT, "gates"), os.path.join(ROOT, "build")):
    sys.path.insert(0, p)
