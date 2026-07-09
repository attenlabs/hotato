"""``hotato issue create SWEEP_JSON --repo OWNER/REPO``: turn a sweep result
into a GitHub issue that asks a human to confirm or ignore each candidate.

The output is a plain markdown issue body: a title from the run, a worst-
candidate block (call id, time, kind, the measured number, the report it came
from), and one confirm-or-ignore section per top candidate. Each section
carries the exact ``hotato fixture promote FILE#N`` command for BOTH a yield
and a hold label (you pick which; the page never does), and a line for the
close-it path when the moment is not a turn-taking failure at all. These are
MEASURED CANDIDATE moments, never verdicts and never intent.

Two honesty boundaries are structural, not prose:

  1. :func:`build_issue` is a PURE, OFFLINE renderer. It reads the parsed
     sweep/analyze document and emits the title, the body, and the exact
     ``gh`` argv it *would* run. It touches no network and shells out to
     nothing.
  2. The only side effect, :func:`create_via_gh`, runs solely from the CLI's
     ``issue create`` path AND only when the caller passes ``--yes`` with an
     explicit ``--repo``. The default is a dry run that prints the body and
     the exact command, creating nothing. This mirrors the project default
     ``github_issue_on_candidate = false``: Hotato never opens an issue on
     your behalf unless you ask for it in that exact call.

The candidate parsing is the SAME parser ``hotato fixture promote`` uses
(:func:`hotato.fixture._load_result`), and the per-candidate promote commands
and measured-number headline are the SAME ones the sweep/analyze dashboard
renders (:mod:`hotato.analyze`), so a ref in the issue resolves byte-for-byte
to the ref on the page.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import List, Optional, Sequence

from .analyze import (
    _detail_text,
    _headline,
    _promote_command,
    suggest_fixture_id,
)
from .fixture import _load_result

__all__ = [
    "load_sweep_result",
    "build_issue",
    "render_gh_command",
    "create_via_gh",
    "DEFAULT_TOP",
    "FIXTURE_OUT_DIR",
]

DEFAULT_TOP = 3
# The fixture root the copied promote commands write into; the same default the
# analyze/sweep dashboard's copy buttons use, so the issue and the page agree.
FIXTURE_OUT_DIR = "tests/hotato"


def load_sweep_result(path: str) -> dict:
    """Read a ``hotato sweep --format json`` (or ``hotato analyze --format
    json``) result file, using the SAME parser ``hotato fixture promote``
    uses. A missing file raises FileNotFoundError (exit 2, file_not_found); a
    file that is not an analyze-kind envelope with a candidates list raises
    ValueError with the honest reason. Reused, not reimplemented, so a ref in
    the issue resolves exactly like a ref on the command line."""
    return _load_result(path)


def _call_id(source: str) -> str:
    """The bare call id a ``FILE#CALL:N`` ref answers to: the recording's stem
    with extensions dropped, and -- for a pulled recording named
    ``STACK__ID.wav`` -- the bare call id. Display only; the same stem rule
    :func:`hotato.analyze.suggest_fixture_id` uses."""
    stem = os.path.splitext(os.path.basename(source))[0]
    if "__" in stem:
        return stem.split("__", 1)[1]
    return stem.split(".", 1)[0]


def render_gh_command(repo: str, title: str,
                      labels: Sequence[str] = ()) -> List[str]:
    """The exact ``gh issue create`` argv this would run. The body is piped on
    stdin (``--body-file -``) so the printed command and the created issue
    carry byte-identical text and the command line never has to inline the
    whole body."""
    argv = ["gh", "issue", "create", "--repo", repo, "--title", title]
    for label in labels:
        argv += ["--label", label]
    argv += ["--body-file", "-"]
    return argv


def _worst_block(report_ref: str, worst: dict) -> str:
    return "\n".join([
        "## Worst candidate",
        "",
        f"- call: `{_call_id(worst['source'])}`",
        f"- time: {worst['t_sec']:.2f}s",
        f"- kind: {worst['kind']}",
        f"- measured: {_headline(worst)}",
        f"- report: `{report_ref}`",
    ])


def _candidate_section(report_ref: str, rank: int, cand: dict) -> str:
    call = _call_id(cand["source"])
    yield_cmd = _promote_command(report_ref, rank, cand, "yield")
    hold_cmd = _promote_command(report_ref, rank, cand, "hold")
    return "\n".join([
        f"### #{rank}  {cand['kind']}  in `{call}`  at "
        f"{cand['t_sec']:.2f}s  ({_headline(cand)})",
        "",
        _detail_text(cand),
        "",
        "Confirm it as a regression fixture (you pick the label):",
        "",
        "```",
        yield_cmd,
        "```",
        "",
        "or, if the agent should have kept the floor:",
        "",
        "```",
        hold_cmd,
        "```",
        "",
        "Not a turn-taking moment? Ignore it, and close this issue if none of "
        "the moments here are real.",
    ])


def _issue_title(doc: dict, shown_n: int) -> str:
    folder = doc.get("folder") or "recordings"
    moment = "moment" if shown_n == 1 else "moments"
    return (f"Turn-taking sweep: {shown_n} candidate {moment} to review in "
            f"{folder}")


def build_issue(
    doc: dict,
    *,
    report_ref: str,
    repo: str,
    top: int = DEFAULT_TOP,
    labels: Sequence[str] = (),
) -> dict:
    """Render the issue from a parsed sweep/analyze result. PURE and OFFLINE:
    no network, no subprocess, no filesystem. Returns the title, the markdown
    body, the exact ``gh`` argv (and its shell-quoted display form), the worst
    candidate, and the machine list of the shown candidates.

    ``report_ref`` is the result-file name the promote commands read (the
    basename of the passed sweep json), so a maintainer who writes that file
    with ``hotato sweep ... --format json`` can run the commands verbatim.
    Raises ValueError when the result has no candidates to file (never opens
    an empty issue)."""
    ranked = doc.get("candidates") or []
    if not ranked:
        raise ValueError(
            f"{report_ref} has no candidate moments to file (total_candidates "
            "is 0); there is nothing to open an issue about."
        )
    shown = ranked if top <= 0 else ranked[:top]
    worst = ranked[0]
    title = _issue_title(doc, len(shown))

    calls_scanned = doc.get("calls_scanned")
    total = doc.get("total_candidates", len(ranked))

    intro = [
        f"A hotato sweep surfaced {total} candidate turn-taking "
        f"{'moment' if total == 1 else 'moments'}"
        + (f" across {calls_scanned} call{'' if calls_scanned == 1 else 's'}."
           if calls_scanned is not None else "."),
        "",
        "These are measured timing candidates, not verdicts. Hotato measures "
        "the timing; you label each moment. Confirm the real ones as "
        "permanent regression fixtures, and ignore the rest.",
        "",
        _worst_block(report_ref, worst),
        "",
        "## Candidates to confirm",
        "",
        "Each moment below is a measured candidate. `yield` means the agent "
        "should have stopped for the caller; `hold` means it should have kept "
        "the floor through a backchannel or noise.",
        "",
    ]
    sections = [_candidate_section(report_ref, i, c)
                for i, c in enumerate(shown, 1)]
    footer = [
        "",
        "---",
        "",
        "Measured candidate moments from a hotato sweep, ranked by measured "
        "overlap or gap. Energy is not intent and Hotato infers none. Offline; "
        "no audio left the machine it was scanned on.",
    ]
    body = "\n".join(intro + ["\n\n".join(sections)] + footer) + "\n"

    argv = render_gh_command(repo, title, labels)
    return {
        "tool": "hotato",
        "kind": "issue",
        "schema_version": "1",
        "repo": repo,
        "title": title,
        "labels": list(labels),
        "top": len(shown),
        "total_candidates": total,
        "worst": {
            "call": _call_id(worst["source"]),
            "source": worst["source"],
            "t_sec": worst["t_sec"],
            "kind": worst["kind"],
            "measured": _headline(worst),
            "report": report_ref,
        },
        "candidates": [
            {
                "rank": i,
                "call": _call_id(c["source"]),
                "source": c["source"],
                "t_sec": c["t_sec"],
                "kind": c["kind"],
                "measured": _headline(c),
                "id": suggest_fixture_id(c["source"], c["kind"], i),
                "promote_yield": _promote_command(report_ref, i, c, "yield"),
                "promote_hold": _promote_command(report_ref, i, c, "hold"),
            }
            for i, c in enumerate(shown, 1)
        ],
        "body": body,
        "gh_command": argv,
        "gh_command_display": " ".join(shlex.quote(a) for a in argv),
    }


def create_via_gh(argv: Sequence[str], body: str):
    """Run ``gh issue create``, piping ``body`` on stdin. The ONLY side effect
    in this module; the CLI calls it solely under ``--yes`` with an explicit
    ``--repo``. Returns ``(returncode, stdout, stderr)``. A missing ``gh``
    binary raises FileNotFoundError, which the CLI surfaces as the standard
    exit-2 structured error."""
    proc = subprocess.run(
        list(argv), input=body, text=True, capture_output=True,
    )
    return proc.returncode, proc.stdout, proc.stderr
