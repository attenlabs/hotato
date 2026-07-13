"""The `hotato` first-run screen.

Printed when `hotato` runs with no subcommand. Zero third-party dependencies:
the logo is a literal, color is optional ANSI gated on an interactive terminal,
and it degrades to plain text when the output is piped, when NO_COLOR is set, or
on a terminal too narrow for the logo. Every command shown is copy-paste-safe
(no inline `#` comments to break a paste).
"""
from __future__ import annotations

import os
import shutil
import sys

# ansi_shadow "hotato", generated once at design time and embedded so the
# package stays dependency-free at runtime.
_LOGO = [
    '██╗  ██╗ ██████╗ ████████╗ █████╗ ████████╗ ██████╗ ',
    '██║  ██║██╔═══██╗╚══██╔══╝██╔══██╗╚══██╔══╝██╔═══██╗',
    '███████║██║   ██║   ██║   ███████║   ██║   ██║   ██║',
    '██╔══██║██║   ██║   ██║   ██╔══██║   ██║   ██║   ██║',
    '██║  ██║╚██████╔╝   ██║   ██║  ██║   ██║   ╚██████╔╝',
    '╚═╝  ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝ ',
]
_LOGO_WIDTH = 52

# A wisp of steam over the wordmark: hotato is a hot potato.
_STEAM = [
    "        ) )   ( (",
    "       ( (     ) )",
]

_EMBER = "\033[38;2;255;90;31m"
_POTATO = "\033[38;2;224;184;119m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _use_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _term_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def render(*, color: bool = False, width: int | None = None) -> str:
    """Return the first-run screen as a string. ``color`` adds ANSI; ``width``
    (default: the real terminal width) selects the full logo or a compact
    header for narrow terminals."""
    if width is None:
        width = _term_width()
    e = _EMBER if color else ""
    p = _POTATO if color else ""
    d = _DIM if color else ""
    b = _BOLD if color else ""
    r = _RESET if color else ""

    out = []
    if width >= _LOGO_WIDTH:
        for s in _STEAM:
            out.append(d + s + r)
        for g in _LOGO:
            out.append(e + g + r)
    else:
        # narrow terminal: a compact, always-legible header
        out.append("")
        out.append(b + e + "hotato" + r + d + "  (a hot potato)" + r)
    out.append("")
    out.append("  " + b + "Conversation QA for voice agents." + r)
    out.append("  See exactly why a call passed, or failed.")
    out.append("")
    out.append("  " + b + "Try it now." + r + " One command, on your machine:")
    out.append("")
    out.append("      " + e + "hotato start --demo" + r)
    out.append("")
    out.append("  It scores two bundled calls and opens a report you can read.")
    out.append("")
    out.append("  More:")
    out.append("      " + e + "hotato serve" + r + d + "            the team dashboard, in your browser" + r)
    out.append("      " + e + "hotato test run FILE" + r + d + "    score one call across five dimensions" + r)
    out.append("      " + e + "hotato demo" + r + d + "             watch a failing agent get caught" + r)
    out.append("      " + e + "hotato --help" + r + d + "           every command" + r)
    out.append("")
    out.append(d + "  hotato.dev   github.com/attenlabs/hotato   MIT, offline, zero dependencies." + r)
    out.append("")
    return "\n".join(out) + "\n"


def print_intro(stream=None) -> None:
    stream = stream or sys.stdout
    stream.write(render(color=_use_color(stream)))
