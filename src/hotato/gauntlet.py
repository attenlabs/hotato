"""``hotato gauntlet``: the bundled, seeded turn-taking stress suite and its
"Gauntlet N/10" badge.

A curated battery of standardized turn-taking stimuli that ship INSIDE the
package (the manifest under ``hotato/data/gauntlet/``; the audio is the existing
``hotato/data/audio/*.wav`` reference set), so a bare ``pip install hotato`` can
run the whole suite with no file authoring. Each case is a two-channel stimulus
(a packaged reference recording, some with a seeded synthetic perturbation from
:mod:`hotato.synth` applied) scored by the deterministic timing scorer
(:func:`hotato.core.run_single`) at a labelled caller onset. A case PASSES when
the scorer's yield/hold verdict agrees with the case's ground-truth label.

Determinism: the reference recordings are fixed, every perturbation pins a seed
(so the derived clip is byte-identical), and the scorer is deterministic, so the
whole suite renders, scores, and counts byte-identical on every machine and CI
run. This module ADDS the curated suite, its runner, and the badge renderer; the
scoring is the existing :func:`hotato.core.run_single`, never a new engine.

Scope: the gauntlet scores the bundled deterministic stimulus. Live-endpoint
targeting is a separate concern and is not part of this suite. The badge is a
self-contained SVG whose ``N`` is read from an executed gauntlet run, never
invented.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import Any, Dict, List, Optional

__all__ = [
    "SUITE_NAME",
    "SUITE_DIR",
    "AUDIO_DIR",
    "MANIFEST_FILENAME",
    "load_manifest",
    "list_cases",
    "ids",
    "families",
    "run_case",
    "run_gauntlet",
    "render_badge",
]

# The suite's stable id and the package-relative directory its manifest ships in
# (installed with the wheel; see pyproject ``package-data``). The stimuli reuse
# the bundled reference audio under ``hotato/data/audio``.
SUITE_NAME = "hotato-turn-taking-gauntlet"
SUITE_DIR = ("data", "gauntlet")
AUDIO_DIR = ("data", "audio")
MANIFEST_FILENAME = "manifest.json"

# yield/hold are the two ground-truth turn-taking labels the scorer decides.
_EXPECTS = ("yield", "hold")


def _suite_resource(filename: str):
    """The importlib resource for a file inside the packaged gauntlet directory.
    Mirrors :mod:`hotato.simulate_pack` -- the same posture the package uses for
    its other bundled data (installed package data, never a user-supplied path)."""
    from importlib import resources  # deferred: import cost at interpreter start

    return resources.files("hotato").joinpath(*SUITE_DIR, filename)


def _audio_resource(wav: str):
    """The importlib resource for one bundled reference recording."""
    from importlib import resources

    return resources.files("hotato").joinpath(*AUDIO_DIR, wav)


def load_manifest() -> Dict[str, Any]:
    """Load and lightly validate the gauntlet manifest (``gauntlet/manifest.json``).

    Returns the parsed manifest ``{suite, version, description, cases: [...]}``.
    Each ``cases`` entry carries ``id``, ``title``, ``family``, ``wav``,
    ``onset_sec``, ``expect`` (``yield``/``hold``) and, for a robustness variant,
    a ``perturbation`` recipe plus its ``seed``. Raises ``ValueError`` on a
    malformed manifest -- a broken bundled index is a packaging defect, surfaced
    up front."""
    # open-ok: bundled importlib resource (installed package data, not a user path)
    text = _suite_resource(MANIFEST_FILENAME).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except ValueError as exc:  # pragma: no cover - a shipped-file defect
        raise ValueError(f"gauntlet manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("gauntlet manifest must be a mapping")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("gauntlet manifest must carry a non-empty 'cases' list")
    seen: set = set()
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"gauntlet manifest cases[{i}] must be a mapping")
        cid = case.get("id")
        if not cid or not isinstance(cid, str):
            raise ValueError(f"gauntlet manifest cases[{i}] is missing a string 'id'")
        if cid in seen:
            raise ValueError(f"gauntlet manifest has a duplicate case id {cid!r}")
        seen.add(cid)
        wav = case.get("wav")
        if not wav or not isinstance(wav, str):
            raise ValueError(f"gauntlet case {cid!r} is missing a string 'wav'")
        expect = case.get("expect")
        if expect not in _EXPECTS:
            raise ValueError(
                f"gauntlet case {cid!r} 'expect' must be one of {_EXPECTS}"
            )
        if not isinstance(case.get("onset_sec"), (int, float)):
            raise ValueError(f"gauntlet case {cid!r} needs a numeric 'onset_sec'")
        pert = case.get("perturbation")
        if pert is not None:
            if not isinstance(pert, dict) or not pert.get("transform"):
                raise ValueError(
                    f"gauntlet case {cid!r} 'perturbation' must name a transform"
                )
            if not isinstance(case.get("seed"), int):
                raise ValueError(
                    f"gauntlet case {cid!r} has a perturbation but no integer 'seed'"
                )
    return manifest


def list_cases() -> List[Dict[str, Any]]:
    """The gauntlet cases sorted by ``id`` for a stable, byte-reproducible
    listing regardless of manifest order."""
    cases = list(load_manifest().get("cases") or [])
    return sorted(cases, key=lambda c: c["id"])


def ids() -> List[str]:
    """The gauntlet case ids, sorted."""
    return [c["id"] for c in list_cases()]


def families() -> List[str]:
    """The distinct turn-taking families the suite covers, sorted."""
    return sorted({c.get("family", "") for c in list_cases()})


def run_case(case: Dict[str, Any], work_dir: str) -> Dict[str, Any]:
    """Score ONE gauntlet case and return a byte-stable per-case result.

    For a robustness variant the bundled recording is first perturbed with the
    pinned seed into ``work_dir`` (via :func:`hotato.synth.perturb`), so the
    derived clip is byte-identical every run; then the clip is scored. The case
    passes when it is scorable and the scorer's yield/hold verdict agrees with
    the case's ground-truth label. Only basenames are recorded (never an absolute
    path), so the result is identical across machines."""
    from . import core as _core  # deferred: keep module import light

    onset = float(case["onset_sec"])
    expect = case["expect"]
    pert = case.get("perturbation")
    clip_name = None
    if pert is None:
        path = str(_audio_resource(case["wav"]))
    else:
        from . import synth as _synth  # deferred

        clip_name = f"gauntlet-{case['id']}.wav"
        out_path = os.path.join(work_dir, clip_name)
        _synth.perturb(str(_audio_resource(case["wav"])), pert,
                       out_path=out_path, seed=int(case["seed"]))
        path = out_path

    env = _core.run_single(stereo=path, onset_sec=onset, expect=expect)
    event = env["events"][0]
    # A not-scorable event carries scorable=False and no trustworthy verdict; a
    # scorable event's verdict.passed already encodes did_yield == expected label.
    scorable = event.get("scorable", True) is not False
    verdict = event.get("verdict") or {}
    passed = bool(scorable and verdict.get("passed"))
    result = {
        "id": case["id"],
        "title": case.get("title", case["id"]),
        "family": case.get("family", ""),
        "expect": expect,
        "onset_sec": onset,
        "perturbation": pert,
        "seed": case.get("seed") if pert is not None else None,
        "clip": clip_name,
        "scorable": scorable,
        "did_yield": verdict.get("did_yield"),
        "seconds_to_yield": verdict.get("seconds_to_yield"),
        "talk_over_sec": verdict.get("talk_over_sec"),
        "passed": passed,
    }
    if not scorable:
        result["not_scorable_reason"] = event.get("not_scorable_reason")
    return result


def run_gauntlet(out_dir: Optional[str] = None) -> Dict[str, Any]:
    """Run the whole bundled suite deterministically and return the result
    envelope ``{tool, schema_version, kind, suite, total, passed, all_passed,
    cases}``.

    When ``out_dir`` is given the derived robustness clips and a ``gauntlet.json``
    copy of the result are written there; otherwise the clips render into a
    throwaway temp directory and nothing is persisted. Either way the returned
    envelope is byte-identical: the reference recordings are fixed, every
    perturbation pins a seed, and the scorer is deterministic."""
    manifest = load_manifest()
    persist = out_dir is not None
    if persist:
        os.makedirs(out_dir, exist_ok=True)
    with contextlib.ExitStack() as stack:
        work = out_dir if persist else stack.enter_context(
            tempfile.TemporaryDirectory())
        case_results = [run_case(c, work) for c in list_cases()]
    total = len(case_results)
    passed = sum(1 for r in case_results if r["passed"])
    envelope = {
        "tool": "hotato",
        "schema_version": "1",
        "kind": "gauntlet",
        "suite": manifest.get("suite", SUITE_NAME),
        "total": total,
        "passed": passed,
        "all_passed": passed == total,
        "cases": case_results,
    }
    if persist:
        path = os.path.join(out_dir, "gauntlet.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, indent=2, sort_keys=False)
            fh.write("\n")
    return envelope


# --- the "Gauntlet N/10" badge (deterministic, self-contained SVG) ---------
#
# A compact two-segment badge (label + score) whose N is READ from an executed
# gauntlet run, never invented. No external resource: no font file, image,
# stylesheet, script, or link; all color is inline hex; the only URL is the SVG
# namespace declaration. The status is expressed in WORDS in the title/desc, so
# a screen reader and any monochrome viewer get the same status the color
# carries. Every byte is a pure function of (passed, total): no timestamps, no
# version, no randomness, so the same result renders the same SVG forever.

_BADGE_H = 36
_BADGE_SIZE = 14
_BADGE_PAD = 13
_BADGE_ADV = _BADGE_SIZE * 0.62   # monospace advance -> deterministic box width
_BADGE_R = 6
_BADGE_FONT = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"
_BADGE_INK = "#f6efe4"            # cream, on the dark label segment
_BADGE_BG = "#16110d"            # hotato ink background
_BADGE_OK = "#3ecf8e"           # every case cleared
_BADGE_PARTIAL = "#ff5a1f"      # some case did not clear
_BADGE_OK_INK = "#0e1a1b"       # dark ink reads on the bright cleared segment


def _badge_esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _seg_width(text: str) -> int:
    return round(len(text) * _BADGE_ADV + 2 * _BADGE_PAD)


def render_badge(result: Dict[str, Any]) -> str:
    """Render the deterministic self-contained ``Gauntlet N/10`` SVG badge from a
    gauntlet result envelope (as :func:`run_gauntlet` returns). The ``N`` and the
    total are read straight from the result's ``passed`` / ``total`` counts, so
    the badge can never assert a score the suite did not measure. Raises
    ``ValueError`` for anything that is not a gauntlet result."""
    if not isinstance(result, dict) or result.get("kind") != "gauntlet":
        raise ValueError(
            "render_badge needs a gauntlet result (kind='gauntlet'); the badge "
            "score is derived from an executed run, never invented"
        )
    total = result.get("total")
    passed = result.get("passed")
    if not isinstance(total, int) or not isinstance(passed, int) \
            or total <= 0 or not 0 <= passed <= total:
        raise ValueError("gauntlet result carries an unusable passed/total count")

    all_passed = passed == total
    status = _BADGE_OK if all_passed else _BADGE_PARTIAL
    score_ink = _BADGE_OK_INK if all_passed else _BADGE_INK

    label = "hotato gauntlet"
    score = f"{passed}/{total}"
    left_w = _seg_width(label)
    right_w = _seg_width(score)
    w, h, r = left_w + right_w, _BADGE_H, _BADGE_R
    baseline = h / 2 + _BADGE_SIZE * 0.34

    title = f"hotato gauntlet: {passed} of {total} turn-taking stress cases cleared"
    desc = (
        f"hotato gauntlet badge. The deterministic timing scorer cleared "
        f"{passed} of {total} bundled turn-taking stress cases, each a yield or "
        f"hold moment scored against its label. Scores the bundled deterministic "
        f"stimulus."
    )

    # right segment as a path with rounded RIGHT corners and a square left edge,
    # butting cleanly against the dark rounded label segment (no seam gap, no
    # clipPath / url()).
    right_path = (
        f"M{left_w:g},0 H{w - r:g} A{r:g} {r:g} 0 0 1 {w:g} {r:g} "
        f"V{h - r:g} A{r:g} {r:g} 0 0 1 {w - r:g} {h:g} H{left_w:g} Z"
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:g}" height="{h:g}" '
        f'viewBox="0 0 {w:g} {h:g}" role="img" aria-labelledby="g-title g-desc">',
        f'<title id="g-title">{_badge_esc(title)}</title>',
        f'<desc id="g-desc">{_badge_esc(desc)}</desc>',
        f'<rect x="0" y="0" width="{w:g}" height="{h:g}" rx="{r:g}" '
        f'fill="{_BADGE_BG}"/>',
        f'<path d="{right_path}" fill="{status}"/>',
        f'<text x="{left_w / 2:g}" y="{baseline:g}" font-family="{_BADGE_FONT}" '
        f'font-size="{_BADGE_SIZE:g}" font-weight="700" fill="{_BADGE_INK}" '
        f'text-anchor="middle">{_badge_esc(label)}</text>',
        f'<text x="{left_w + right_w / 2:g}" y="{baseline:g}" '
        f'font-family="{_BADGE_FONT}" font-size="{_BADGE_SIZE:g}" '
        f'font-weight="700" fill="{score_ink}" text-anchor="middle">'
        f'{_badge_esc(score)}</text>',
        "</svg>",
    ]
    return "\n".join(parts) + "\n"
