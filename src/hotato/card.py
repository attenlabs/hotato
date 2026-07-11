"""Render a hotato result into a shareable card: a deterministic, stdlib-only,
redacted-by-default SVG (1200x630) with no external resources.

One command turns a machine result -- a sweep/analyze candidate (FILE#N), a fix
plan, or a verify rollup -- into a self-contained image you can drop into a PR,
an issue, or a slide. The card is honest by construction: it names the measured
timing moment and never a verdict about intent, and it carries no accuracy
number anywhere.

Four card kinds, auto-detected from the input:

  A. talk-over candidate  -- an ``overlap_while_agent_talking`` /
     ``agent_start_during_caller`` moment (FILE#N)
  B. false-stop candidate -- an ``agent_stop_no_caller`` moment (FILE#N)
  C. threshold funnel     -- a fix plan whose decision is
     ``do_not_tune_single_threshold`` (the hero card)
  D. paired comparison    -- a supported ``hotato verify`` before/after rollup
     that actually improved; never rendered as an unconditional "verified"

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
from typing import List

from . import evidence as _evidence
from . import fixture as _fixture

# --- warm charcoal / cream / ember theme (mirrors report.py so a card reads as
#     the same family as the report it came from). Inline hex only; nothing here
#     references an external asset. ------------------------------------------
_C = {
    "bg": "#1b1714",
    "panel": "#241f1a",
    "line": "#3a3128",
    "cream": "#f1e8d7",
    "muted": "#b7ab97",
    "ember": "#f0663a",   # accent + the measured timing number
    "green": "#74c98a",   # comparison: paired evidence improved
}

_W = 1200
_H = 630
_M = 76           # outer margin
_FONT = "'Helvetica Neue',Helvetica,Arial,sans-serif"
_MONO = "'SFMono-Regular',Menlo,Consolas,monospace"


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


def _frame(body: List[str], *, title: str, desc: str) -> str:
    """Assemble the shared canvas: background, a thin inner keyline, the HOTATO
    wordmark, and a divider under the header. ``body`` is the card-specific
    content already laid out below the header.

    Accessibility: every card is an image with a text equivalent. The root
    ``<svg>`` carries ``role="img"`` and ``aria-labelledby`` pointing at a
    ``<title>`` (the status line) and a ``<desc>`` (the full text equivalent),
    so a screen reader -- and any monochrome / no-color viewer -- gets the same
    pass/refuse status the colors carry. The status is expressed in WORDS in
    the title/desc, never in color alone. Ids are fixed, so the SVG stays a
    pure, deterministic function of the input."""
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-labelledby="card-title card-desc">',
        f'<title id="card-title">{_esc(title)}</title>',
        f'<desc id="card-desc">{_esc(desc)}</desc>',
        _rect(0, 0, _W, _H, fill=_C["bg"]),
        _rect(20, 20, _W - 40, _H - 40, stroke=_C["line"], sw=1.5, rx=18),
        _text(_M, 108, "HOTATO", size=32, fill=_C["ember"], weight="700",
              spacing=9),
    ]
    return "\n".join(head + body + ["</svg>"]) + "\n"


def _kind_tag(label: str) -> str:
    return _text(_W - _M, 108, label, size=22, fill=_C["muted"], weight="600",
                 anchor="end", spacing=3)


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
                         weight="800"))
    return out


# --- card A / B: one sweep/analyze candidate ------------------------------

# Every candidate kind the scanner emits, mapped to a card headline, the
# duration key that carries its measured number, and the one-line description.
_CANDIDATE_CARDS = {
    "overlap_while_agent_talking": (
        "TALK-OVER CANDIDATE", "CANDIDATE TALK-OVER", "overlap_sec",
        "of overlap while the agent was talking",
    ),
    "agent_start_during_caller": (
        "TALK-OVER CANDIDATE", "CANDIDATE TALK-OVER", "overlap_sec",
        "the agent came in while the caller had the floor",
    ),
    "agent_stop_no_caller": (
        "FALSE-STOP CANDIDATE", "CANDIDATE FALSE-STOP", "trailing_silence_sec",
        "of silence after the agent stopped, no caller nearby",
    ),
    "long_response_gap": (
        "SLOW-RESPONSE CANDIDATE", "CANDIDATE SLOW RESPONSE", "gap_sec",
        "before the agent answered the caller",
    ),
}


def _render_candidate(cand: dict, *, include_identifiers: bool) -> str:
    kind = cand.get("kind")
    spec = _CANDIDATE_CARDS.get(kind)
    if spec is None:
        raise ValueError(
            f"candidate kind {kind!r} has no card; hotato card renders "
            "talk-over, false-stop, and slow-response candidates"
        )
    tag, headword, dur_key, desc = spec
    durations = cand.get("durations") or {}
    measured = durations.get(dur_key)
    t_sec = cand.get("t_sec")

    body = [_kind_tag(tag)]
    body += _headline(_wrap(f"HOTATO FOUND A {headword}", 24), top=214)
    # The measured timing number, large, in the accent color.
    body.append(_text(_M, 388, _fmt_sec(measured), size=104, fill=_C["ember"],
                      weight="800", family=_MONO))
    for i, ln in enumerate(_wrap(desc, 40)):
        body.append(_text(_M, 440 + i * 34, ln, size=27, fill=_C["cream"],
                          weight="500"))
    if isinstance(t_sec, (int, float)):
        body.append(_text(_M, 520, f"at t={_fmt_sec(t_sec)} in the recording",
                          size=23, fill=_C["muted"], weight="500"))
    # Redaction: the source recording name is hidden unless asked for. A path
    # is collapsed to its basename; a call id embedded in a pulled recording
    # name (STACK__ID.wav) rides inside that basename and is only ever shown
    # under --include-identifiers.
    if include_identifiers:
        src = os.path.basename(str(cand.get("source", ""))) or "(unknown)"
        body.append(_text(_W - _M, 520, f"source: {src}", size=21,
                          fill=_C["muted"], weight="500", anchor="end",
                          family=_MONO))
    body.append(_footer("Hotato reports timing candidates, not intent."))
    a11y_title = f"Hotato found a {headword.lower()}"
    at = (f" at t={_fmt_sec(t_sec)} in the recording"
          if isinstance(t_sec, (int, float)) else "")
    a11y_desc = (f"{_fmt_sec(measured)} {desc}{at}. Hotato reports timing "
                 "candidates, not intent.")
    return _frame(body, title=a11y_title, desc=a11y_desc)


# --- card C: the threshold-funnel fix plan (the hero) ---------------------

def _render_funnel(plan: dict) -> str:
    fix_class = ((plan.get("recommended_fix") or {}).get("class")
                 or "engagement-control")
    body = [_kind_tag("THRESHOLD FUNNEL")]
    body += _headline(_wrap("NO SINGLE THRESHOLD CAN FIX THIS", 24), top=196)
    # The two axes that pull against each other.
    axes = ["missed a real interruption", "false-stopped on a backchannel"]
    for i, ax in enumerate(axes):
        y = 336 + i * 46
        body.append(_dot(_M + 8, y - 9, 7, _C["ember"]))
        body.append(_text(_M + 32, y, ax, size=31, fill=_C["cream"],
                          weight="600"))
    body.append(_text(
        _M, 448, "One sensitivity dial cannot satisfy both axes at once.",
        size=27, fill=_C["muted"], weight="500"))
    body.append(_text(_M, 506, "Hotato refused threshold tuning.", size=27,
                      fill=_C["cream"], weight="700"))
    body.append(_text(_W - _M, 506, f"fix class: {fix_class}", size=25,
                      fill=_C["ember"], weight="700", anchor="end",
                      family=_MONO))
    body.append(_footer(
        "Reproducible timing verdicts from the open scorer. No accuracy score."))
    a11y_desc = (
        "No single threshold can fix this: one sensitivity dial cannot both "
        "avoid missing a real interruption and avoid false-stopping on a "
        f"backchannel. Hotato refused threshold tuning. Fix class: {fix_class}."
    )
    return _frame(body, title="No single threshold can fix this",
                  desc=a11y_desc)


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

    text_equiv = (
        f"{now} of {used} failing fixtures now pass; {still} of {guards} hold "
        f"fixtures still pass; evidence tier: {ev_headline}"
    )

    if tier >= _evidence.TIER_PAIRED:
        _attested = tier >= _evidence.TIER_ATTESTED
        _card_title = ("PAIRED FRESH-RECAPTURE" if _attested
                       else "PAIRED (OPERATOR-ASSERTED)")
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
        num_text = _fmt_sec(measurement.get("seconds_to_yield"))
        sub = "measured time to yield"
    else:
        num_text = _fmt_sec(measurement.get("talk_over_sec"))
        sub = "measured talk-over while the agent held the floor"

    status_word = ("NOT SCORABLE" if not scorable
                   else "PASSED" if passed else "FAILED")
    status_color = (_C["muted"] if not scorable
                    else _C["green"] if passed else _C["ember"])

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


# --- dispatch -------------------------------------------------------------

_ANALYZE_HINT = (
    "a sweep/analyze result names many candidate moments, so a card needs a "
    "candidate ref: FILE#N (e.g. {p}#1), the same #N rank the report shows"
)


def render_plan_card(plan: dict) -> str:
    """Public entry for an in-process fix-plan dict (used by ``hotato start``).
    Only the threshold-funnel plan renders; any other plan is a clean error."""
    if plan.get("decision") != "do_not_tune_single_threshold":
        raise ValueError(
            "hotato card renders the threshold-funnel plan (decision "
            f"do_not_tune_single_threshold); this plan's decision is "
            f"{plan.get('decision')!r}. It is not a card."
        )
    return _render_funnel(plan)


def make_card(input_arg: str, *, include_identifiers: bool = False) -> str:
    """Detect the input's kind and render the matching SVG card. Raises
    ValueError on anything that is not a hotato candidate ref, fix plan, or
    verify rollup (the CLI turns that into exit 2)."""
    if input_arg and "#" in input_arg:
        # A candidate ref: FILE#N or FILE#CALL:N. Reuse the exact resolver the
        # promote path uses, so a card and a fixture speak of the same moment.
        path, call, number = _fixture.parse_candidate_ref(input_arg)
        doc = _fixture._load_result(path)
        cand = _fixture._resolve_candidate(doc, path=path, call=call,
                                           number=number)
        return _render_candidate(cand, include_identifiers=include_identifiers)

    with open(input_arg, encoding="utf-8") as fh:
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
        return render_plan_card(doc)
    if kind == "verify":
        return _render_verify(doc)
    if kind == "voice-turn-taking-contract":
        return _render_contract(doc, include_identifiers=include_identifiers)
    if kind == "analyze":
        raise ValueError(_ANALYZE_HINT.format(p=input_arg))
    raise ValueError(
        f"{input_arg!r} is not a card input (kind={kind!r}); pass a hotato fix "
        "plan (kind 'fix-plan'), a verify result (kind 'verify'), a failure "
        "contract (kind 'voice-turn-taking-contract'), or a sweep/analyze "
        "candidate ref (FILE#N)"
    )
