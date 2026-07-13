"""requires-python honesty: the ">=3.9" floor is lower-bounded only (no upper
cap, following packaging guidance that an upper pin locks users out of new
interpreters). Because the floor is open-ended, the TESTED/supported matrix
must be DOCUMENTED so the version claim stays finite and honest rather than an
open-ended promise of every future Python.

Guards:
  (a) requires-python's floor equals the LOWEST `Programming Language :: Python
      :: 3.x` classifier, so the declared floor and the classifier matrix agree;
  (b) pyproject.toml carries a support-policy comment next to requires-python
      that names the tested matrix (so ">=3.9" is not read as unbounded support);
  (c) the policy is also stated in a short docs line (CONTRIBUTING.md), the
      human-facing home for "which Pythons we support".
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_text():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        return fh.read()


def _requires_python_floor(text):
    m = re.search(r'(?m)^requires-python\s*=\s*"([^"]+)"', text)
    assert m, "no requires-python in pyproject.toml"
    spec = m.group(1)
    fm = re.search(r">=\s*(\d+)\.(\d+)", spec)
    assert fm, f"requires-python {spec!r} has no >= lower bound"
    return (int(fm.group(1)), int(fm.group(2)))


def _classifier_python_minors(text):
    return sorted(
        int(m) for m in re.findall(r'Programming Language :: Python :: 3\.(\d+)"', text)
    )


def test_requires_python_floor_matches_lowest_classifier():
    text = _pyproject_text()
    floor = _requires_python_floor(text)
    minors = _classifier_python_minors(text)
    assert minors, "no `Programming Language :: Python :: 3.x` classifiers found"
    assert floor == (3, minors[0]), (
        f"requires-python floor {floor} disagrees with the lowest Python "
        f"classifier 3.{minors[0]}; the floor and the classifier matrix must agree"
    )


def test_requires_python_has_documented_support_policy():
    text = _pyproject_text()
    minors = _classifier_python_minors(text)
    assert minors, "no Python classifiers to anchor the tested matrix"
    lo, hi = minors[0], minors[-1]
    # A support-policy comment must sit in the lines just above requires-python.
    lines = text.splitlines()
    idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("requires-python")),
        None,
    )
    assert idx is not None, "no requires-python line in pyproject.toml"
    window = "\n".join(lines[max(0, idx - 8):idx])
    assert re.search(r"(?i)support policy", window), (
        "requires-python must carry a nearby `# Support policy:` comment "
        "documenting the tested matrix, so the open-ended '>=3.9' floor is honest"
    )
    assert f"3.{lo}" in window and f"3.{hi}" in window, (
        f"the support-policy comment must name the tested matrix bounds "
        f"(3.{lo}..3.{hi}) so the finite claim is explicit"
    )


def test_support_policy_documented_in_docs():
    path = os.path.join(ROOT, "CONTRIBUTING.md")
    if not os.path.exists(path):
        return  # doc not present in this tree
    with open(path, encoding="utf-8") as fh:
        doc = fh.read()
    minors = _classifier_python_minors(_pyproject_text())
    lo, hi = minors[0], minors[-1]
    assert f"3.{lo}" in doc and f"3.{hi}" in doc, (
        f"CONTRIBUTING.md must state the supported Python matrix (3.{lo} to "
        f"3.{hi}) so the finite support claim has a human-facing home"
    )
