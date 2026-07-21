"""Render a hotato result into a shareable card: a deterministic, stdlib-only,
redacted-by-default SVG (1200x630) with no external resources.

One command turns a machine result -- a sweep/analyze candidate (FILE#N), a fix
plan, a verify rollup, a failure contract, or a test-run result -- into a
self-contained image you can drop into a PR, an issue, or a slide. The card is
honest by construction: it names the measured timing moment (or the evidence a
say-do check read) and never a verdict about intent, and it carries no accuracy
number anywhere.

Six card kinds, auto-detected from the input:

  A. talk-over candidate  -- an ``overlap_while_agent_talking`` /
     ``agent_start_during_caller`` moment (FILE#N)
  B. false-stop candidate -- an ``agent_stop_no_caller`` moment (FILE#N)
  C. threshold funnel     -- a fix plan whose decision is
     ``do_not_tune_single_threshold`` (the hero card)
  D. paired comparison    -- a supported ``hotato verify`` before/after rollup
     that actually improved; never rendered as an unconditional "verified"
  E. failure contract     -- a ``hotato contract create`` contract (kind
     ``voice-turn-taking-contract``)
  F. say-do failure       -- a ``hotato test run`` result (kind
     ``hotato.test-run``) whose tool/state evidence failed a declared
     outcome: the claim vs the evidence (assertion id, span refs, the
     share-safe public reason)

Every byte is a pure function of the input JSON: no timestamps, no version, no
randomness, so the same input renders the same SVG forever. The SVG references
no font file, no image, no stylesheet, and no link; all color is inline hex.

Redaction: a call id, a filesystem path (only the basename is ever a candidate
for display), and a vendor recording name are hidden by default. Pass
``include_identifiers=True`` (CLI: ``--include-identifiers``) to show the source
recording's basename on a candidate card.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from typing import List

from . import evidence as _evidence
from . import fixture as _fixture
from .errors import open_regular as _open_regular

# --- claim-language contract (src/hotato/data/evidence_language.json) ------
#
# The single source of truth for which public claim PHRASE may assert which
# evidence tier. card.py renders its headline via ``evidence.headline_for`` and
# then VALIDATES that the rendered phrase is one the table knows AND that the
# tier the table permits it does not exceed the classification tier the evidence
# vector actually earned -- so no renderer can ever emit a phrase stronger than
# its evidence. scripts/copy_lint.py reads the SAME table for shipped static
# copy, so the phrasing and its evidence bar have one source of truth.

class ClaimContractError(ValueError):
    """A renderer produced a claim phrase stronger than its evidence tier, or a
    phrase the claim-language contract does not know. Fail-closed: refuse the card
    rather than ship an over-claim."""


@lru_cache(maxsize=1)
def _claim_table() -> dict:
    raw = (resources.files("hotato")
           .joinpath("data", "evidence_language.json")
           .read_text(encoding="utf-8"))
    return json.loads(raw).get("claims", {})


def _claim_max_tier(headline: str):
    """The max_tier the contract allows the rendered ``headline`` to assert, or
    ``None`` when the phrase is not in the contract. Matches the longest claim
    phrase the headline starts with, so a headline carrying a trailing qualifier
    (e.g. ``... -- NO HOLD GUARD SUBMITTED``) still resolves to its base claim."""
    table = _claim_table()
    if headline in table:
        return table[headline]["max_tier"]
    best = None
    best_len = -1
    for phrase, meta in table.items():
        if headline.startswith(phrase) and len(phrase) > best_len:
            best, best_len = meta["max_tier"], len(phrase)
    return best


def _assert_claim_within_evidence(headline: str, tier: int) -> None:
    """Fail-closed check: the rendered ``headline`` must be a phrase the claim
    contract knows, and the tier that phrase is allowed to assert must not exceed
    the classification ``tier`` the evidence vector earned. Since
    ``evidence.headline_for`` already returns the phrase FOR ``tier``, this always
    holds for correct code -- it is the tripwire that catches a future renderer or
    a hand-edited phrase that would over-claim."""
    max_tier = _claim_max_tier(headline)
    if max_tier is None:
        raise ClaimContractError(
            f"rendered claim {headline!r} is not in the evidence-language "
            "contract (src/hotato/data/evidence_language.json); refusing to ship "
            "a claim the honesty table does not govern"
        )
    if max_tier > tier:
        raise ClaimContractError(
            f"rendered claim {headline!r} asserts evidence tier {max_tier}, above "
            f"the classification tier {tier} the evidence vector earned; refusing "
            "to ship an over-claim"
        )

# --- canonical ember-dark theme (the exact brand tokens the home page and the
#     failure record use, so a card reads as the same surface). One ember accent
#     carries measured values; status meaning is carried by teal / crimson /
#     amber, never by the ember hue. Color is inline; when the illustrative
#     assets are built the brand fonts ride in as embedded woff2 data so the
#     card is still a single self-contained file. -----------------------------
_C = {
    "bg": "#16110d",
    "surface": "#1f1712",
    "panel": "#241a13",    # card
    "line": "rgba(246,239,228,0.10)",
    "cream": "#f6efe4",    # ink
    "muted": "#b9a892",
    "ember": "#ff5a1f",    # single accent + the measured timing number
    "ember_glow": "#ff7a3c",
    "green": "#3ecf8e",    # PASS / good
    "crimson": "#ff5c5c",  # FAIL / talk-over
    "amber": "#f5b942",    # warn / inconclusive
}

_W = 1200
_H = 630
_M = 76           # outer margin
# Brand type: Bricolage Grotesque (display / headline / wordmark), Hanken
# Grotesk (body), Spline Sans Mono (eyebrows, labels, status pills, numbers,
# commands, record ids). The family lists fall back cleanly where the brand
# faces are not present; the built illustrative cards embed them so they render
# on the site and in the README.
_DISPLAY = "'Bricolage','Bricolage Grotesque','Helvetica Neue',system-ui,sans-serif"
_SANS = "'Hanken','Hanken Grotesk','Helvetica Neue',system-ui,sans-serif"
_MONO = "'SplineMono','Spline Sans Mono',ui-monospace,'SF Mono',Menlo,monospace"
_FONT = _SANS     # body default


# --- SVG primitives (deterministic, escaped) ------------------------------

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _text(x, y, s, *, size, fill, weight="400", family=_FONT,
          anchor="start", spacing=0.0) -> str:
    ls = f' letter-spacing="{spacing:g}"' if spacing else ""
    return (
        f'<text x="{x:g}" y="{y:g}" font-family="{family}" '
        f'font-size="{size:g}" font-weight="{weight}" fill="{fill}"'
        f'{ls} text-anchor="{anchor}">{_esc(s)}</text>'
    )


def _rect(x, y, w, h, *, fill=None, stroke=None, sw=1.0, rx=0) -> str:
    parts = [f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}"']
    if rx:
        parts.append(f' rx="{rx:g}"')
    parts.append(f' fill="{fill}"' if fill else ' fill="none"')
    if stroke:
        parts.append(f' stroke="{stroke}" stroke-width="{sw:g}"')
    parts.append("/>")
    return "".join(parts)


def _dot(cx, cy, r, fill) -> str:
    return f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{fill}"/>'


def _wrap(text: str, max_chars: int) -> List[str]:
    """Greedy word wrap into lines of at most ``max_chars``. Deterministic:
    the same text and width always split the same way."""
    lines: List[str] = []
    cur = ""
    for word in text.split():
        cand = word if not cur else f"{cur} {word}"
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fmt_sec(v) -> str:
    """A duration in seconds, 2 decimals with trailing zeros trimmed, so 0.32
    -> '0.32s', 2.0 -> '2s', 0.5 -> '0.5s'. Deterministic and mojibake-free."""
    if not isinstance(v, (int, float)):
        return "?s"
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return f"{s}s"


def _pill(x, y, text, *, size=15, text_fill, stroke=None, fill=None,
          dashed=False, anchor="start", weight="600", spacing=1.4) -> str:
    """A Spline Mono pill: a rounded chip carrying a label, id, or status. A
    solid-filled pill reads as attested; an unfilled dashed pill reads as
    provisional, so a 'candidate, not verdict' chip is visibly weaker than an
    attested one. Width is derived from the monospace advance, so the box always
    fits its text deterministically."""
    adv = size * 0.62
    tw = len(text) * adv + spacing * max(len(text) - 1, 0)
    w = tw + 30
    h = size + 18
    x0 = x - w if anchor == "end" else x
    r = h / 2
    box = [f'<rect x="{x0:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{r:g}"']
    box.append(f' fill="{fill}"' if fill else ' fill="none"')
    if stroke:
        box.append(f' stroke="{stroke}" stroke-width="1.4"')
        if dashed:
            box.append(' stroke-dasharray="5 5"')
    box.append("/>")
    label = _text(x0 + w / 2, y + r + size * 0.34, text, size=size,
                  fill=text_fill, weight=weight, family=_MONO, anchor="middle",
                  spacing=spacing)
    return "".join(box) + "\n" + label


def _frame(body: List[str], *, title: str, desc: str, font_css: str = "") -> str:
    """Assemble the shared canvas: background, a soft ember accent glow, a thin
    inner keyline, the hotato wordmark, and the card-specific ``body``.

    ``font_css`` optionally carries embedded ``@font-face`` rules (used when the
    illustrative assets are built) so the brand faces render wherever the SVG is
    dropped in as an image; the runtime card leaves it empty and falls back to
    the system stack. Either way the card is one self-contained file.

    Accessibility: every card is an image with a text equivalent. The root
    ``<svg>`` carries ``role="img"`` and ``aria-labelledby`` pointing at a
    ``<title>`` (the status line) and a ``<desc>`` (the full text equivalent),
    so a screen reader, and any monochrome viewer, gets the same status the
    colors carry. The status is expressed in WORDS, never in color alone. Ids
    are fixed, so the SVG stays a pure, deterministic function of the input."""
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-labelledby="card-title card-desc">',
        f'<title id="card-title">{_esc(title)}</title>',
        f'<desc id="card-desc">{_esc(desc)}</desc>',
    ]
    if font_css:
        head.append('<defs><style type="text/css"><![CDATA['
                    + font_css + ']]></style></defs>')
    head += [
        _rect(0, 0, _W, _H, fill=_C["bg"]),
        # soft ember accent in the top-right corner: two translucent discs, no
        # gradient and no url() so the card holds the no-external-resource line.
        f'<circle cx="1090" cy="40" r="240" fill="{_C["ember"]}" '
        f'fill-opacity="0.055"/>',
        f'<circle cx="1150" cy="10" r="150" fill="{_C["ember_glow"]}" '
        f'fill-opacity="0.05"/>',
        _rect(20, 20, _W - 40, _H - 40, stroke=_C["line"], sw=1.5, rx=18),
        # brand mark: a small ember tile beside the wordmark.
        _rect(_M, 74, 26, 26, fill=_C["ember"], rx=7),
        _text(_M + 40, 96, "hotato", size=34, fill=_C["cream"], weight="800",
              family=_DISPLAY),
    ]
    return "\n".join(head + body + ["</svg>"]) + "\n"


def _kind_tag(label: str) -> str:
    """The top-right kind tag as a Spline Mono evidence pill."""
    return _pill(_W - _M, 70, label, size=15, text_fill=_C["muted"],
                 stroke=_C["line"], anchor="end", spacing=1.6)


def _footer(text: str) -> str:
    return "\n".join([
        _rect(_M, _H - 96, _W - 2 * _M, 1.2, fill=_C["line"]),
        _text(_M, _H - 54, text, size=23, fill=_C["muted"], weight="500"),
    ])


def _headline(lines: List[str], *, top: float, size: float = 56,
              lh: float = 66) -> List[str]:
    out = []
    for i, ln in enumerate(lines):
        out.append(_text(_M, top + i * lh, ln, size=size, fill=_C["cream"],
                         weight="800", family=_DISPLAY))
    return out


# --- card A / B: one sweep/analyze candidate ------------------------------

# Every candidate kind the scanner emits, mapped to a card headline, the
# duration key that carries its measured number, and the one-line description.
_CANDIDATE_CARDS = {
    "overlap_while_agent_talking": (
        "TALK-OVER CANDIDATE", "Candidate talk-over", "overlap_sec",
        "of overlap while the agent was talking", "overlap", "overlap",
    ),
    "agent_start_during_caller": (
        "TALK-OVER CANDIDATE", "Candidate talk-over", "overlap_sec",
        "the agent came in while the caller had the floor", "overlap",
        "overlap",
    ),
    "agent_stop_no_caller": (
        "FALSE-STOP CANDIDATE", "Candidate false-stop", "trailing_silence_sec",
        "of silence after the agent stopped, no caller nearby", "gap",
        "trailing silence",
    ),
    "long_response_gap": (
        "SLOW-RESPONSE CANDIDATE", "Candidate slow response", "gap_sec",
        "before the agent answered the caller", "gap", "response gap",
    ),
}

# The dual-channel strip amplitude is a fixed, deterministic pseudo-waveform so
# the same card renders the same bytes forever; it illustrates timing, not the
# real samples.
_STRIP_X0, _STRIP_X1 = 336, _W - _M - 18
_STRIP_N = 40


def _bar(cx, yc, half, fill) -> str:
    bw = 8.4
    return (f'<rect x="{cx - bw / 2:g}" y="{yc - half:g}" width="{bw:g}" '
            f'height="{2 * half:g}" rx="3" fill="{fill}"/>')


def _waveform(mode: str) -> List[str]:
    """The focal object of a candidate card: a two-lane audio strip (agent over
    caller) with the measured moment marked by a crimson band. In ``overlap``
    mode the band is where both lanes are speaking at once; in ``gap`` mode it is
    the silence after the agent lane stops with the caller lane quiet."""
    import math
    x0, x1 = _STRIP_X0, _STRIP_X1
    w = x1 - x0
    step = w / _STRIP_N
    y_agent, y_caller = 258, 384
    amp = 30
    if mode == "overlap":
        agent_win = (0.0, 1.0)
        caller_win = (0.44, 0.68)
        hi = (0.44, 0.68)
    else:  # gap
        agent_win = (0.0, 0.52)
        caller_win = (2.0, 2.0)  # caller quiet throughout
        hi = (0.56, 0.82)
    hx0, hx1 = x0 + hi[0] * w, x0 + hi[1] * w

    def lane(yc, win, active_fill):
        out = []
        for i in range(_STRIP_N):
            f = (i + 0.5) / _STRIP_N
            cx = x0 + f * w
            if win[0] <= f <= win[1]:
                a = 0.42 + 0.58 * abs(math.sin(f * 22.0 + yc))
                half = amp * a
                fill = _C["crimson"] if hi[0] <= f <= hi[1] else active_fill
                out.append(_bar(cx, yc, half, fill))
            else:
                out.append(_bar(cx, yc, 1.6, _C["line"]))
        return out

    parts: List[str] = []
    # the highlight band spans both lanes.
    parts.append(
        f'<rect x="{hx0:g}" y="{y_agent - amp - 16:g}" width="{hx1 - hx0:g}" '
        f'height="{(y_caller + amp + 16) - (y_agent - amp - 16):g}" rx="10" '
        f'fill="rgba(255,92,92,0.10)" stroke="{_C["crimson"]}" '
        f'stroke-width="1.4" stroke-dasharray="6 5"/>')
    parts.append(_text(x0, y_agent - amp - 22, "agent", size=15,
                       fill=_C["muted"], weight="600", family=_MONO,
                       spacing=1.2))
    parts.append(_text(x0, y_caller + amp + 30, "caller", size=15,
                       fill=_C["muted"], weight="600", family=_MONO,
                       spacing=1.2))
    parts += lane(y_agent, agent_win, _C["cream"])
    parts += lane(y_caller, caller_win, _C["cream"])
    if mode == "gap":
        # name what the empty band means: the agent stopped and no caller came.
        parts.append(_text((hx0 + hx1) / 2, (y_agent + y_caller) / 2 + 5,
                           "no caller", size=17, fill=_C["crimson"],
                           weight="700", family=_MONO, anchor="middle",
                           spacing=1.0))
    return parts


def _render_candidate(cand: dict, *, include_identifiers: bool,
                      font_css: str = "") -> str:
    kind = cand.get("kind")
    spec = _CANDIDATE_CARDS.get(kind)
    if spec is None:
        raise ValueError(
            f"candidate kind {kind!r} has no card; hotato card renders "
            "talk-over, false-stop, and slow-response candidates"
        )
    tag, headline, dur_key, desc, mode, eyebrow = spec
    durations = cand.get("durations") or {}
    measured = durations.get(dur_key)
    t_sec = cand.get("t_sec")

    body = [_kind_tag(tag)]
    body += _headline([headline], top=176, size=52)
    # Focal object: the dual-channel strip with the measured moment marked.
    body += _waveform(mode)
    # The measured duration, large, in the status hue (crimson), tied to the
    # crimson band; the ember accent stays reserved for the brand mark.
    body.append(_text(_M, 244, eyebrow, size=15, fill=_C["muted"],
                      weight="600", family=_MONO, spacing=2))
    body.append(_text(_M, 344, _fmt_sec(measured), size=96, fill=_C["crimson"],
                      weight="800", family=_MONO))
    desc_lines = _wrap(desc, 60)
    for i, ln in enumerate(desc_lines):
        body.append(_text(_M, 470 + i * 32, ln, size=27, fill=_C["cream"],
                          weight="500"))
    if isinstance(t_sec, (int, float)):
        body.append(_text(_M, 470 + len(desc_lines) * 32 + 6,
                          f"at t={_fmt_sec(t_sec)} in the recording",
                          size=21, fill=_C["muted"], weight="500",
                          family=_MONO))
    # The honest-strength chip: a dashed, unfilled pill reads visibly weaker
    # than a solid attested one.
    body.append(_pill(_W - _M, 470, "candidate, not verdict", size=16,
                      text_fill=_C["muted"], stroke=_C["muted"], dashed=True,
                      anchor="end"))
    # Redaction: the source recording name is hidden unless asked for. A path
    # is collapsed to its basename; a call id embedded in a pulled recording
    # name (STACK__ID.wav) rides inside that basename and is only ever shown
    # under --include-identifiers.
    if include_identifiers:
        src = os.path.basename(str(cand.get("source", ""))) or "(unknown)"
        body.append(_text(_W - _M, 520, f"source: {src}", size=18,
                          fill=_C["muted"], weight="500", anchor="end",
                          family=_MONO))
    body.append(_footer("Hotato reports timing candidates, not intent."))
    a11y_title = f"Hotato found a candidate {tag.split()[0].lower()}"
    at = (f" at t={_fmt_sec(t_sec)} in the recording"
          if isinstance(t_sec, (int, float)) else "")
    a11y_desc = (f"{_fmt_sec(measured)} {desc}{at}. Hotato reports timing "
                 "candidates, not intent.")
    return _frame(body, title=a11y_title, desc=a11y_desc, font_css=font_css)


# --- card C: the threshold-funnel fix plan (the hero) ---------------------

def _bowtie() -> List[str]:
    """The focal object of the funnel card: the 'no single dial' bowtie. One
    sensitivity axis runs left (loose) to right (tight); two error curves cross,
    so wherever the single dial sits, one error is already climbing. There is no
    setting that puts both low at once."""
    x0, x1 = 336, _W - _M - 30
    y0, y1 = 250, 430          # plot box
    w, h = x1 - x0, y1 - y0
    # axis
    parts = [
        f'<line x1="{x0:g}" y1="{y1:g}" x2="{x1:g}" y2="{y1:g}" '
        f'stroke="{_C["line"]}" stroke-width="1.4"/>',
        f'<line x1="{x0:g}" y1="{y0:g}" x2="{x0:g}" y2="{y1:g}" '
        f'stroke="{_C["line"]}" stroke-width="1.4"/>',
    ]
    # missed-interruption curve: high on the LOOSE (left) end, falls to the right.
    miss = (f'M {x0:g} {y0 + 0.10 * h:g} '
            f'C {x0 + 0.45 * w:g} {y0 + 0.30 * h:g} '
            f'{x0 + 0.62 * w:g} {y1 - 0.06 * h:g} {x1:g} {y1 - 0.02 * h:g}')
    # false-stop curve: low on the loose end, climbs to the TIGHT (right) end.
    fstop = (f'M {x0:g} {y1 - 0.02 * h:g} '
             f'C {x0 + 0.40 * w:g} {y1 - 0.08 * h:g} '
             f'{x0 + 0.58 * w:g} {y0 + 0.28 * h:g} {x1:g} {y0 + 0.10 * h:g}')
    parts.append(f'<path d="{miss}" fill="none" stroke="{_C["crimson"]}" '
                 f'stroke-width="4" stroke-linecap="round"/>')
    parts.append(f'<path d="{fstop}" fill="none" stroke="{_C["amber"]}" '
                 f'stroke-width="4" stroke-linecap="round"/>')
    # the single dial sits at the crossing; wherever it moves, one curve rises.
    xc = x0 + 0.52 * w
    parts.append(f'<line x1="{xc:g}" y1="{y0 - 6:g}" x2="{xc:g}" y2="{y1 + 6:g}" '
                 f'stroke="{_C["cream"]}" stroke-width="1.4" '
                 f'stroke-dasharray="4 5"/>')
    parts.append(_dot(xc, (y0 + y1) / 2, 8, _C["cream"]))
    parts.append(f'<circle cx="{xc:g}" cy="{(y0 + y1) / 2:g}" r="14" '
                 f'fill="none" stroke="{_C["cream"]}" stroke-width="1.4"/>')
    # axis end labels + one dial label.
    parts.append(_text(x0, y1 + 30, "loose", size=14, fill=_C["muted"],
                       weight="600", family=_MONO, spacing=1.2))
    parts.append(_text(x1, y1 + 30, "tight", size=14, fill=_C["muted"],
                       weight="600", family=_MONO, anchor="end", spacing=1.2))
    parts.append(_text(xc, y0 - 16, "one dial", size=14, fill=_C["muted"],
                       weight="600", family=_MONO, anchor="middle", spacing=1.2))
    # curve legends near their high ends.
    parts.append(_dot(x0 + 14, y0 + 0.10 * h - 5, 6, _C["crimson"]))
    parts.append(_text(x0 + 30, y0 + 0.10 * h, "missed a real interruption",
                       size=19, fill=_C["cream"], weight="600"))
    parts.append(_dot(x1 - 8, y0 + 0.10 * h - 5, 6, _C["amber"]))
    parts.append(_text(x1 - 22, y0 + 0.10 * h, "false-stopped on a backchannel",
                       size=19, fill=_C["cream"], weight="600", anchor="end"))
    return parts


def _render_funnel(plan: dict, *, font_css: str = "") -> str:
    fix_class = ((plan.get("recommended_fix") or {}).get("class")
                 or "engagement-control")
    body = [_kind_tag("THRESHOLD FUNNEL")]
    body += _headline(["NO SINGLE THRESHOLD CAN FIX THIS"], top=176, size=46)
    body += _bowtie()
    body.append(_text(
        _M, 486, "One sensitivity dial cannot satisfy both axes at once.",
        size=25, fill=_C["muted"], weight="500"))
    body.append(_text(_M, 524, "Hotato refused threshold tuning.", size=27,
                      fill=_C["cream"], weight="700", family=_DISPLAY))
    body.append(_pill(_W - _M, 506, f"fix class: {fix_class}", size=16,
                      text_fill=_C["ember"], stroke=_C["ember"], anchor="end"))
    body.append(_footer(
        "Reproducible timing verdicts from the open scorer."))
    a11y_desc = (
        "No single threshold can fix this: one sensitivity dial cannot both "
        "avoid missing a real interruption and avoid false-stopping on a "
        f"backchannel. Hotato refused threshold tuning. Fix class: {fix_class}."
    )
    return _frame(body, title="No single threshold can fix this",
                  desc=a11y_desc, font_css=font_css)


# --- card D: a supported before/after comparison rollup -------------------
#
# A ``hotato verify`` result is paired before/after evidence, never a claim
# about the CURRENT agent in isolation. This card must never render the word
# "VERIFIED": per the words-to-reserve table, the honest status for a genuine
# paired improvement names its origin -- "PAIRED FRESH-RECAPTURE IMPROVED" only
# when the recapture is runner-attested, "PAIRED (OPERATOR-ASSERTED)" otherwise
# -- never "verified fix" or "fix verified". Hotato reports coincidence, never
# causation, and never claims a hold guard was "protected" -- only that it did
# not regress.
#
# The green fresh-recapture card is the strongest visual claim a card can make,
# so it is gated on the EVIDENCE tier (green + "fresh-recapture" reserved for the
# ATTESTED tier), not just on the
# (hand-writable) claim/counts fields: it renders only when the result carries
# an evidence classification that reaches the paired tier (a fix-trial recompute
# from audio). That tier is RE-DERIVED here from the evidence vector -- the
# input ``tier`` field is itself hand-writable, so a forged {"tier": 3} with a
# weak/absent vector is capped back down to what the vector supports and can
# never mint the green pass. A standalone verify (an envelope comparison, tier
# ASSERTED) or a legacy input with no evidence block renders a MUTED,
# explicitly-unverified card whose headline names the real tier
# ("ASSERTED (UNVERIFIED)" / "MEASURED FROM AUDIO"), never the green pass.

def _render_verify(v: dict) -> str:
    claim = v.get("claim") or {}
    hold = v.get("hold_axis") or {}
    reg = v.get("regression_axis") or {}
    now = reg.get("now_pass") or 0
    used = reg.get("used_to_fail")
    still = hold.get("still_pass")
    guards = hold.get("hold_guards")
    if not claim.get("supported"):
        raise ValueError(
            "this verify result does not support a claim (too few "
            "previously-failing fixtures, or nothing now passes); no card. "
            "Run hotato verify with enough paired fixtures first."
        )
    if (hold.get("regressed") or 0) > 0:
        raise ValueError(
            "this verify result regressed a hold/backchannel fixture; the "
            "'paired evidence improved' card would be false. No card."
        )
    if now == 0:
        raise ValueError(
            "this verify result improved nothing (no previously-failing "
            "fixture now passes); the 'paired evidence improved' card would "
            "be false. No card."
        )

    # Evidence gate. A missing/weak block never renders the green pass: a
    # legacy input with no evidence is treated as the envelope-only ASSERTED
    # ceiling, exactly as a standalone verify is classified.
    ev = v.get("evidence")
    if not (isinstance(ev, dict) and isinstance(ev.get("tier"), int)):
        ev = _evidence.classify({
            "score_integrity": "envelope_only", "audio_identity": "missing",
            "pairing_integrity": "id_only", "label_authority": "none",
            "policy_integrity": "unsigned", "fixture_set_integrity": "unknown",
            "capture_origin": "unknown", "input_health": None,
            "channel_mapping": None,
        })
    # RE-DERIVE the tier from the evidence VECTOR: the input ``tier`` field is
    # hand-writable, so a forged {"evidence": {"tier": 3}} must never mint the
    # green paired card on its own. With an inspectable vector we cap the tier
    # at what that vector actually supports (never trust an input tier the
    # vector cannot back); with no vector at all the tier is ASSERTED, because
    # a bare tier number is not evidence of anything.
    vector = ev.get("vector")
    if isinstance(vector, dict):
        real_tier = _evidence.evidence_tier(
            vector, _evidence.REQUIRED_FOR_PAIRED_PROOF)
        tier = min(int(ev.get("tier", 0)), real_tier)
    else:
        tier = min(int(ev.get("tier", _evidence.TIER_ASSERTED)),
                   _evidence.TIER_ASSERTED)
    ev_headline = _evidence.headline_for(
        tier, vector if isinstance(vector, dict) else {})
    # Claim contract: the rendered headline may never assert a tier above the
    # evidence the vector earned. Validated against the same table copy_lint reads.
    _assert_claim_within_evidence(ev_headline, tier)

    text_equiv = (
        f"{now} of {used} failing fixtures now pass; {still} of {guards} hold "
        f"fixtures still pass; evidence tier: {ev_headline}"
    )

    if tier >= _evidence.TIER_PAIRED:
        _attested = tier >= _evidence.TIER_ATTESTED
        _card_title = ("PAIRED FRESH-RECAPTURE" if _attested
                       else "PAIRED (OPERATOR-ASSERTED)")
        # The big visible claim on the card face is also bound by the contract.
        _assert_claim_within_evidence(_card_title, tier)
        _kind = "ATTESTED PAIRED" if _attested else "PAIRED (OPERATOR-ASSERTED)"
        _guard_line = ("no submitted hold guard regressed" if guards
                       else "no hold guard was submitted")
        body = [_kind_tag(_kind)]
        body += _headline(_wrap(_card_title, 26), top=200)
        body.append(_text(_M, 250, _guard_line,
                          size=23, fill=_C["muted"], weight="500"))
        # Green accent is reserved for ATTESTED (runner-verified) fresh recapture.
        # An operator-asserted pair reports its real counts in cream, never the
        # fresh-fix green it did not earn.
        _accent = _C["green"] if _attested else _C["cream"]
        rows = [
            (f"{now} of {used}", "failing fixtures now pass", _accent),
            (f"{still} of {guards}", "hold fixtures still pass", _C["cream"]),
        ]
        for i, (num, label, col) in enumerate(rows):
            y = 400 + i * 70
            body.append(_text(_M, y, num, size=52, fill=col, weight="800",
                              family=_MONO))
            body.append(_text(_M + 210, y, label, size=28, fill=_C["muted"],
                              weight="500"))
        body.append(_footer("Hotato reports coincidence, not causation."))
        return _frame(body, title=ev_headline, desc=text_equiv)

    # tier < PAIRED: a muted, explicitly-unverified card. The WORDS carry the
    # status (no green), so it reads the same in monochrome. The caveat is
    # keyed off the RE-DERIVED tier, never the (hand-writable) input tier.
    caveat = _evidence.one_sentence({"tier": tier})
    body = [_kind_tag("ENVELOPE COMPARISON")]
    body += _headline(_wrap(ev_headline, 26), top=196)
    for i, ln in enumerate(_wrap(caveat, 62)):
        body.append(_text(_M, 264 + i * 30, ln, size=21, fill=_C["muted"],
                          weight="500"))
    rows = [
        (f"{now} of {used}", "failing fixtures now pass", _C["cream"]),
        (f"{still} of {guards}", "hold fixtures still pass", _C["muted"]),
    ]
    for i, (num, label, col) in enumerate(rows):
        y = 432 + i * 66
        body.append(_text(_M, y, num, size=46, fill=col, weight="800",
                          family=_MONO))
        body.append(_text(_M + 210, y, label, size=26, fill=_C["muted"],
                          weight="500"))
    body.append(_footer(
        "Envelope comparison only; not a fresh-recapture paired proof."))
    return _frame(body, title=ev_headline, desc=text_equiv)


# --- card E: a failure contract (hotato contract create) ------------------

def _render_contract(contract: dict, *, include_identifiers: bool = False) -> str:
    label = contract.get("label") or {}
    measurement = contract.get("measurement") or {}
    expect = label.get("expected_behavior", "yield")
    scorable = bool(measurement.get("scorable"))
    passed = measurement.get("passed")

    if not scorable:
        num_text, sub = "N/A", "NOT SCORABLE"
    elif expect == "yield":
        # Hero the DEFINED number. When the agent never yielded (the sharpest
        # failure), ``seconds_to_yield`` is null by definition, so heroing it
        # renders a meaningless "?s" on the most-shared asset. Lead instead with
        # the measured talk-over and say plainly that the agent never yielded.
        stt = measurement.get("seconds_to_yield")
        tov = measurement.get("talk_over_sec")
        if stt is None and tov is not None:
            num_text = _fmt_sec(tov)
            sub = "talk-over; the agent never yielded"
        else:
            num_text = _fmt_sec(stt)
            sub = "measured time to yield"
    else:
        num_text = _fmt_sec(measurement.get("talk_over_sec"))
        sub = "measured talk-over while the agent held the floor"

    status_word = ("NOT SCORABLE" if not scorable
                   else "PASSED" if passed else "FAILED")
    status_color = (_C["muted"] if not scorable
                    else _C["green"] if passed else _C["crimson"])

    body = [_kind_tag("FAILURE CONTRACT")]
    body += _headline(_wrap(f"CONTRACT: EXPECT {expect.upper()}", 24), top=200)
    body.append(_text(_M, 360, num_text, size=96, fill=_C["ember"], weight="800",
                      family=_MONO))
    body.append(_text(_M, 404, sub, size=23, fill=_C["muted"], weight="500"))
    body.append(_text(_M, 470, f"id: {contract.get('id', '')}", size=25,
                      fill=_C["cream"], weight="600", family=_MONO))
    body.append(_text(_W - _M, 470, status_word, size=28, fill=status_color,
                      weight="800", anchor="end", spacing=2))
    if include_identifiers:
        sha = (contract.get("source") or {}).get("source_audio_sha256", "")
        if sha:
            body.append(_text(_W - _M, 506, f"sha256 {sha[:16]}...", size=18,
                              fill=_C["muted"], weight="500", anchor="end",
                              family=_MONO))
    body.append(_footer("A human labeled this contract; Hotato measured the timing."))
    a11y_title = f"Failure contract expect {expect}: {status_word}"
    a11y_desc = (f"Contract {contract.get('id', '')}, expected behavior "
                 f"{expect}: {status_word}. {num_text} {sub}. A human labeled "
                 "this contract; Hotato measured the timing.")
    return _frame(body, title=a11y_title, desc=a11y_desc)


# --- card F: a say-do outcome failure (hotato test run) --------------------
#
# A ``hotato test run`` result whose deterministic lane failed a tool/state
# evidence assertion: the conversation claims an outcome, the trace (Authority
# 1) or the post-call state (Authority 2) does not back it. The card renders
# the CLAIM VS EVIDENCE shape -- the failing assertion's id and kind, its span
# refs when the evaluator recorded any, and its share-safe ``public_reason``
# (built by hotato.assert_ from allowlisted structured fields only, never
# transcript text, a tool payload, or a state value) -- so the card stays
# shareable with no scrub. The same deterministic-SVG invariants hold: no
# timestamp, no accuracy number, inline color only, redacted by default.

# The Authority-1/2 evidence kinds a say-do failure can rest on. Words-only
# kinds (phrase/pii/...) never qualify: a say-do card is about evidence
# contradicting the conversation, and evidence means tool spans or state.
_SAYDO_EVIDENCE_KINDS = ("tool_result", "tool_call", "tool_error",
                         "http_result", "state", "state_change")
_SAYDO_TRACE_KINDS = ("tool_result", "tool_call", "tool_error", "http_result")


def _render_saydo(result: dict, *, font_css: str = "") -> str:
    from .errors import is_safe_bare_token as _is_safe

    env = result.get("assertions")
    results = env.get("results") if isinstance(env, dict) else None
    if not isinstance(results, list):
        raise ValueError(
            "this test-run result carries no evaluated assertions envelope; "
            "the say-do card renders a hotato test run result saved with "
            "--format json (hotato start --demo writes one at "
            "saydo/test-run.json)"
        )
    fails = [r for r in results
             if isinstance(r, dict) and r.get("status") == "FAIL"
             and r.get("kind") in _SAYDO_EVIDENCE_KINDS]
    if not fails:
        raise ValueError(
            "this test-run result has no failing tool/state evidence "
            "assertion (tool_result, tool_call, tool_error, http_result, "
            "state, state_change); the say-do failure card renders a declared "
            "outcome the trace or post-call state did not back. No card."
        )
    # Deterministic selection: the first failing outcome-tagged evidence
    # assertion in result order, else the first failing evidence assertion.
    # Reordering equal inputs is the only way to change which one leads.
    prime = next((r for r in fails if r.get("dimension") == "outcome"),
                 fails[0])

    kind = prime["kind"]
    aid_raw = prime.get("id")
    aid = (aid_raw if isinstance(aid_raw, str) and _is_safe(aid_raw)
           else "(redacted)")
    public = prime.get("public_reason")
    if not isinstance(public, str) or not public.strip():
        # A hand-built result may omit it; the generic fallback is built from
        # the closed kind vocabulary only, so it is share-safe by construction.
        public = f"A declared {kind} evidence condition was not satisfied."
    span_ids = [s for s in (prime.get("span_ids") or [])
                if isinstance(s, str) and _is_safe(s)]
    if span_ids:
        evidence_line = f"trace spans: {', '.join(span_ids)}"
    elif kind in _SAYDO_TRACE_KINDS:
        evidence_line = "no qualifying tool span in the trace"
    else:
        evidence_line = "post-call state did not hold the declared outcome"

    test_id_raw = result.get("test_id")
    test_id = (test_id_raw if isinstance(test_id_raw, str)
               and _is_safe(test_id_raw) else None)
    outcome = (result.get("dimensions") or {}).get("outcome")
    counts_text = None
    if isinstance(outcome, dict):
        p, f = outcome.get("pass"), outcome.get("fail")
        if (isinstance(p, int) and not isinstance(p, bool)
                and isinstance(f, int) and not isinstance(f, bool)):
            counts_text = f"outcome: {p} pass / {f} fail"

    body = [_kind_tag("SAY-DO FAILURE")]
    body += _headline(["CLAIMED, NOT EVIDENCED"], top=176, size=50)
    body.append(_text(_M, 244, "outcome evidence", size=15, fill=_C["muted"],
                      weight="600", family=_MONO, spacing=2))
    body.append(_text(_M, 344, "FAIL", size=96, fill=_C["crimson"],
                      weight="800", family=_MONO))
    body += _saydo_panel(evidence_line, f"assertion: {aid} ({kind})")
    # The share-safe public reason, capped to two deterministic lines (the
    # full sentence lives in the JSON result the card was rendered from).
    public_lines = _wrap(public, 56)
    if len(public_lines) > 2:
        public_lines = [public_lines[0], public_lines[1] + " ..."]
    for i, ln in enumerate(public_lines):
        body.append(_text(_M, 474 + i * 32, ln, size=25, fill=_C["cream"],
                          weight="500"))
    if counts_text:
        body.append(_pill(_W - _M, 458, counts_text, size=16,
                          text_fill=_C["muted"], stroke=_C["muted"],
                          anchor="end"))
    if test_id:
        body.append(_text(_W - _M, 520, f"test: {test_id}", size=18,
                          fill=_C["muted"], weight="500", anchor="end",
                          family=_MONO))
    footer = ("Tool and state evidence decide the outcome, never the agent's "
              "words.")
    body.append(_footer(footer))
    a11y_title = "Say-do failure: a claimed outcome the evidence did not back"
    a11y_desc = (f"{public} Assertion {aid} ({kind}); {evidence_line}. "
                 f"{footer}")
    return _frame(body, title=a11y_title, desc=a11y_desc, font_css=font_css)


def _saydo_panel(evidence_line: str, assertion_line: str) -> List[str]:
    """The focal object of the say-do card: the claim box over the evidence
    box. The claim box is dashed and unfilled (the same visibly-weaker
    treatment the 'candidate, not verdict' chip uses -- words are never
    evidence); the evidence box carries the crimson verdict line plus the
    assertion ref that produced it."""
    x0, x1 = _STRIP_X0, _STRIP_X1
    w = x1 - x0
    parts: List[str] = []
    y_said, h_said = 232, 80
    parts.append(
        f'<rect x="{x0:g}" y="{y_said:g}" width="{w:g}" height="{h_said:g}" '
        f'rx="12" fill="none" stroke="{_C["muted"]}" stroke-width="1.4" '
        f'stroke-dasharray="6 5"/>')
    parts.append(_text(x0 + 22, y_said + 28, "said", size=15,
                       fill=_C["muted"], weight="600", family=_MONO,
                       spacing=1.6))
    parts.append(_text(x0 + 22, y_said + 60, "what the call says happened",
                       size=21, fill=_C["cream"], weight="500"))
    parts.append(_pill(x1 - 16, y_said + 24, "words, not evidence", size=14,
                       text_fill=_C["muted"], stroke=_C["muted"], dashed=True,
                       anchor="end"))
    parts.append(_text(x0 + 22, 336, "checked against", size=14,
                       fill=_C["muted"], weight="600", family=_MONO,
                       spacing=1.6))
    y_did, h_did = 350, 96
    parts.append(
        f'<rect x="{x0:g}" y="{y_did:g}" width="{w:g}" height="{h_did:g}" '
        f'rx="12" fill="rgba(255,92,92,0.06)" stroke="{_C["crimson"]}" '
        f'stroke-width="1.4"/>')
    parts.append(_text(x0 + 22, y_did + 28, "did", size=15,
                       fill=_C["muted"], weight="600", family=_MONO,
                       spacing=1.6))
    parts.append(_text(x0 + 22, y_did + 56, evidence_line, size=21,
                       fill=_C["crimson"], weight="600"))
    parts.append(_text(x0 + 22, y_did + 82, assertion_line, size=16,
                       fill=_C["muted"], weight="500", family=_MONO))
    return parts


# --- dispatch -------------------------------------------------------------

_ANALYZE_HINT = (
    "a sweep/analyze result names many candidate moments, so a card needs a "
    "candidate ref: FILE#N (e.g. {p}#1), the same #N rank the report shows"
)


def render_plan_card(plan: dict, *, font_css: str = "") -> str:
    """Public entry for an in-process fix-plan dict (used by ``hotato start``).
    Only the threshold-funnel plan renders; any other plan is a clean error.

    ``font_css`` optionally embeds the brand faces (used when the illustrative
    gallery assets are built); the runtime card leaves it empty."""
    if plan.get("decision") != "do_not_tune_single_threshold":
        raise ValueError(
            "hotato card renders the threshold-funnel plan (decision "
            f"do_not_tune_single_threshold); this plan's decision is "
            f"{plan.get('decision')!r}. It is not a card."
        )
    return _render_funnel(plan, font_css=font_css)


def make_card(input_arg: str, *, include_identifiers: bool = False,
              font_css: str = "") -> str:
    """Detect the input's kind and render the matching SVG card. Raises
    ValueError on anything that is not a hotato candidate ref, fix plan,
    verify rollup, failure contract, or test-run result (the CLI turns that
    into exit 2).

    ``font_css`` optionally embeds the brand faces for the built gallery assets;
    the runtime card leaves it empty and falls back to the system stack."""
    if input_arg and "#" in input_arg:
        # A candidate ref: FILE#N or FILE#CALL:N. Reuse the exact resolver the
        # promote path uses, so a card and a fixture speak of the same moment.
        path, call, number = _fixture.parse_candidate_ref(input_arg)
        doc = _fixture._load_result(path)
        cand = _fixture._resolve_candidate(doc, path=path, call=call,
                                           number=number)
        return _render_candidate(cand, include_identifiers=include_identifiers,
                                 font_css=font_css)

    with _open_regular(input_arg, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{input_arg!r} is not JSON ({exc}); pass a hotato fix plan, a "
                "verify result, or a sweep/analyze candidate ref (FILE#N)"
            ) from exc
    if not isinstance(doc, dict):
        raise ValueError(
            f"{input_arg!r} is not a hotato result object; pass a fix plan, a "
            "verify result, or a sweep/analyze candidate ref (FILE#N)"
        )
    kind = doc.get("kind")
    if kind == "fix-plan":
        return render_plan_card(doc, font_css=font_css)
    if kind == "verify":
        return _render_verify(doc)
    if kind == "voice-turn-taking-contract":
        return _render_contract(doc, include_identifiers=include_identifiers)
    if kind == "hotato.test-run":
        return _render_saydo(doc, font_css=font_css)
    if kind == "analyze":
        raise ValueError(_ANALYZE_HINT.format(p=input_arg))
    raise ValueError(
        f"{input_arg!r} is not a card input (kind={kind!r}); pass a hotato fix "
        "plan (kind 'fix-plan'), a verify result (kind 'verify'), a failure "
        "contract (kind 'voice-turn-taking-contract'), a test-run result "
        "(kind 'hotato.test-run'), or a sweep/analyze candidate ref (FILE#N)"
    )
