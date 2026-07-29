"""The one place hotato's colours are defined.

These values mirror the site's design tokens (hotato-site
``DESIGN-STANDARD.md`` section 3: neutral surfaces, one brand accent, semantic
colour that carries meaning). A report is the artifact a user sends to a
colleague, so the HTML the product writes has to read as the same surface as
hotato.dev. Every renderer -- ``report``, ``serve.render``, ``card``,
``contract``, ``aggregate``, ``failure_render``, ``counterexample.render``,
``gauntlet`` (the badge) and ``loadtest`` (the load result page) -- reads its
colours from here; nothing else defines a palette.

The semantic roles carry the standard's names:

===============  =========  ==================================================
Role             Value      Used for
===============  =========  ==================================================
``GROUND``       #0d1117    Page background
``SURFACE``      #161b22    Cards, table headers, code blocks
``SURFACE_2``    #1c2128    Nested surfaces, chips, hover
``BORDER``       #30363d    Every 1px rule, card edge, and chart gridline
``INK``          #e6edf3    Body text
``MUTED``        #9aa4af    Captions, metadata, table labels
``EMBER``        #ff5a1f    Brand: wordmark, accent, primary fill
``GREEN``        #3fb950    Success: passing, yielded, fixed
``RED``          #f85149    Danger: failing, the caught incident
``ATTENTION``    #d29922    Open, awaiting a response, inconclusive
``INFO``         #4493f8    Acknowledged, informational, the agent channel
``CALLER``       #d29922    Call track: the human side
``AGENT``        #4493f8    Call track: the machine side
===============  =========  ==================================================

Text on a filled accent (an ember button, a PASS chip) uses ``ON_ACCENT``, the
ground colour, which is the only dark value those fills can carry legibly.

``PAPER_*`` are the standard's light-theme neutrals, used by the ``@media
print`` blocks so a printed or PDF-exported report is ink on white rather than
a dark page.

Every text-on-surface pair below clears WCAG AA (4.5:1); the tightest are red
on surface-2 and muted on a border-coloured chip, both 4.83:1.
"""

from __future__ import annotations

# --- neutrals -------------------------------------------------------------
GROUND = "#0d1117"
SURFACE = "#161b22"
SURFACE_2 = "#1c2128"
BORDER = "#30363d"
INK = "#e6edf3"
MUTED = "#9aa4af"

# --- brand + semantics ----------------------------------------------------
EMBER = "#ff5a1f"
EMBER_GLOW = "#ff7a3c"   # the lighter brand tint: glow discs, brand gradient far stop
GREEN = "#3fb950"
RED = "#f85149"
ATTENTION = "#d29922"
INFO = "#4493f8"

# --- the two call tracks --------------------------------------------------
CALLER = ATTENTION       # human side
AGENT = INFO             # machine side

# --- derived roles --------------------------------------------------------
GRID = BORDER            # chart gridlines are the border rule
ON_ACCENT = GROUND       # text sitting on an ember / green / red fill

# --- light neutrals, for the print stylesheet -----------------------------
PAPER = "#ffffff"
PAPER_SURFACE = "#f6f8fa"
PAPER_BORDER = "#d0d7de"
PAPER_INK = "#1f2328"
PAPER_INK_2 = "#39424c"


# The report/serve/aggregate palette. The keys are the historical ones so a
# consumer or test that reads a colour by name keeps working; the values now
# come from the roles above.
REPORT = {
    "bg": GROUND,
    "card": SURFACE,
    "card2": SURFACE_2,
    "line": BORDER,
    "cream": INK,          # primary text
    "muted": MUTED,
    "mono": INK,
    "caller": CALLER,      # human track
    "agent": AGENT,        # machine track
    "ember": EMBER,        # accent + onset marker + talk-over
    "green": GREEN,        # PASS + yield marker
    "red": RED,            # FAIL
    "grid": GRID,
    # Translucent washes derived from the palette, so a fill and its own
    # border can never drift apart the way the warm ones did.
    "ember_halo": "rgba(255, 90, 31, 0.14)",
    "red_wash": "rgba(248, 81, 73, 0.10)",
    "red_wash_soft": "rgba(248, 81, 73, 0.06)",
    # added roles, so a renderer never needs an inline hex
    "attention": ATTENTION,
    "info": INFO,
    "on_accent": ON_ACCENT,
    "paper": PAPER,
    "paper_surface": PAPER_SURFACE,
    "paper_line": PAPER_BORDER,
    "paper_ink": PAPER_INK,
    "paper_ink2": PAPER_INK_2,
}

# The social/illustrative card palette (SVG). Same roles, the card's own key
# names: it keeps three surface levels and calls its danger colour "crimson"
# and its attention colour "amber".
CARD = {
    "bg": GROUND,
    "surface": SURFACE,
    "panel": SURFACE_2,    # card
    "line": BORDER,
    "cream": INK,          # ink
    "muted": MUTED,
    "ember": EMBER,        # single accent + the measured timing number
    "ember_glow": EMBER_GLOW,
    "green": GREEN,        # PASS / good
    "crimson": RED,        # FAIL / talk-over
    "amber": ATTENTION,    # warn / inconclusive
    "on_accent": ON_ACCENT,
}
