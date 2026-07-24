"""``hotato vapi health`` / ``hotato retell health`` / ``hotato bland
health`` / ``hotato synthflow health`` / ``hotato millis health``:
one command from API key to the fleet health report.

ONE implementation behind thin CLI entries: list your recent calls on
the platform, download each recording with the EXISTING pull machinery
(``hotato.capture`` -- the same adapters, the same credential resolution,
the same skip-honestly loop `hotato pull` runs), check every recording with
the autopsy engine through the folder aggregate (``hotato.scanfolder``),
and print the health output with the Voice Stability Score headline. You
never touch a WAV: the dashboard-download-rename-move step is gone.

Design rules honoured here:

  * ZERO new platform-API logic. Listing, fetching, credentials, and the
    mono policy are ``hotato.capture``'s, reused wholesale. Retell has no
    verified list-recent-calls endpoint, so ``hotato retell health`` takes
    explicit ``--call-id`` values (repeatable) instead of guessing one --
    the same rule ``hotato pull`` pins.
  * Credentials resolve exactly as pull does today: an explicit flag, then
    the ``hotato connect`` store, then the stack's environment variable
    (``VAPI_API_KEY`` / ``RETELL_API_KEY`` / ``BLAND_API_KEY``). A missing
    key is ONE actionable line: export the variable or run
    ``hotato connect <stack>``.
  * Bland / Synthflow / Millis export one mixed channel, so each of their
    calls runs autopsy's mono best-effort path: silence timing (dead air,
    latency gaps) is measured from the mixed channel with a measured
    confidence per finding, and talk-over attribution comes from a
    two-channel recording -- the functional scope stated once per run
    (``autopsy.MONO_SCOPE_NOTE``).
  * The Voice Stability Score is the measured share of DUAL-CHANNEL calls
    with zero critical incidents, times 100 (machine field
    ``critical_free_call_rate``) -- the formula prints directly beneath
    the number with the eligible sample size and the policy sha beside
    it. Mono calls never enter that rate: they report into the
    "Best-effort mono observations" block with their own counts, so the
    mono stacks' health reports carry observations without a stability
    score. Zero analyzable calls refuse with the reason instead of
    rendering a 0/0 score.
  * The analysis runs on this machine: recordings download straight from
    the platform's own API and go nowhere else.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from . import capture as _capture

__all__ = [
    "HEALTH_STACKS",
    "DEFAULT_LAST",
    "DEFAULT_LIMIT",
    "default_download_dir",
    "missing_key_message",
    "retell_ids_message",
    "run_health",
]

# The platform health entries. Every one already ships a pull adapter
# (capture.PULL_STACKS); vapi/retell fetch the separated two-channel
# recording (capture.DUAL_PULL_STACKS); bland/synthflow/millis are
# mono/mixed by spec (capture.MONO_STACKS) and route through autopsy's
# best-effort mono path.
HEALTH_STACKS = ("vapi", "retell", "bland", "synthflow", "millis")

DEFAULT_LAST = "7d"
DEFAULT_LIMIT = 100


def default_download_dir(stack: str) -> str:
    """Where the recordings land unless ``--dir`` says otherwise."""
    return os.path.join("hotato-output", f"{stack}-calls")


def missing_key_message(stack: str) -> str:
    """The one actionable line for a missing credential."""
    env = _capture.CONNECT_SPECS[stack]["env"]["api_key"]
    return (f"{stack} health needs an API key: export {env}=YOUR_KEY or run "
            f"hotato connect {stack}.")


def retell_ids_message() -> str:
    """Retell's list-recent-calls endpoint is unconfirmed in the integration
    spec, so hotato never guesses one (the same rule ``hotato pull`` pins):
    the health check takes explicit ids."""
    return ("Retell has no verified list-recent-calls endpoint, so hotato "
            "never guesses one: pass the call ids to check, e.g. "
            "hotato retell health --call-id CALL_ID (repeat --call-id for "
            "more).")


def _window_cutoff(last: str) -> Optional[float]:
    try:
        return _capture.since_epoch(last)
    except ValueError as exc:
        raise ValueError(
            f"--last {last!r} is not a duration; use e.g. 7d, 12h, 30m, 2w "
            "(s=seconds, m=minutes, h=hours, d=days, w=weeks)."
        ) from exc


def run_health(
    stack: str,
    *,
    last: str = DEFAULT_LAST,
    limit: int = DEFAULT_LIMIT,
    output: Optional[str] = None,
    dir: Optional[str] = None,
    ids: Optional[List[str]] = None,
    api_key: Optional[str] = None,
    fmt: str = "text",
) -> int:
    """Pull recent recordings for ``stack``, run the folder health aggregate
    over the download directory, and print the health output (Voice
    Stability Score headline over the dual-channel denominator with sample
    size and policy sha beside it, the measured share formula beneath, the
    best-effort mono observations block for mono-analyzed calls, the
    evidence coverage block, categories, worst calls, and recurrence-state
    lines when prior runs of the same directory are stored). A mono stack's
    report carries the observations block without a stability score.

    Every refusal is a clean ValueError (CLI exit 2) with the reason: a
    missing key, a malformed ``--last`` window, retell without ``--call-id``,
    a window with no calls, a pull in which every listed recording failed to
    fetch, or a pulled set with zero analyzable calls (no score is reported
    over zero calls)."""
    from . import autopsy as _autopsy
    from . import scanfolder as _scanfolder

    stack = (stack or "").strip().lower()
    if stack not in HEALTH_STACKS:
        raise ValueError(
            f"{stack!r} has no health command; the platform health entries "
            f"are: {', '.join(HEALTH_STACKS)}."
        )
    cutoff = _window_cutoff(last)
    try:
        creds = _capture.resolve_creds(
            stack, {"api_key": api_key} if api_key else None)
    except ValueError as exc:
        raise ValueError(missing_key_message(stack)) from exc
    if stack == "retell" and not ids:
        raise ValueError(retell_ids_message())

    out_dir = dir or default_download_dir(stack)
    # Bland's export is mono/mixed by spec: the recordings are pulled for
    # autopsy's mono best-effort path (measured-confidence silence timing;
    # the scope line states what mono establishes). The stricter scoring
    # commands keep their own --allow-mono gate untouched.
    allow_mono = stack in _capture.MONO_STACKS
    res = _capture.pull(
        stack, creds, out_dir=out_dir, ids=ids, since=cutoff,
        limit=max(1, int(limit)), allow_mono=allow_mono,
        log=lambda m: sys.stderr.write(m + "\n"))

    if not res["listed"]:
        raise ValueError(
            f"no {stack} calls found in the last {last}. Widen the window "
            "(--last 30d) or check the account; the health report needs at "
            "least one call to measure."
        )
    if not res["pulled"]:
        raise ValueError(
            f"every listed {stack} recording failed to fetch "
            f"({res['listed']} listed, 0 pulled). Check the key and the "
            "vendor's status, then re-run; the per-call reasons are in the "
            "skip lines above."
        )

    result, calls_raw = _scanfolder.run_scan_folder(out_dir, min_gap_sec=2.0)
    if result["counts"]["analyzed"] == 0:
        reasons = "; ".join(
            f"{r['file']}: {r['reason']}" for r in result["refused"][:3])
        raise ValueError(
            f"0 of the {stack} recordings in {out_dir} were analyzable "
            f"({result['counts']['refused']} refused: {reasons}). No score "
            "is reported over zero analyzed calls."
        )
    prior_runs = _scanfolder.persist_run(result, calls_raw)
    result["prior_runs"] = prior_runs
    if output:
        from .cli import _atomic_write_text

        _atomic_write_text(
            output, _scanfolder.build_scan_report_html(result, prior_runs))

    pull_block = {
        "stack": stack,
        "window": last,
        "listed": res["listed"],
        "pulled": len(res["pulled"]),
        "skipped": len(res["skipped"]),
        "dir": out_dir,
    }
    if fmt == "json":
        from . import errors as _errors

        payload = dict(result)
        payload["pull"] = pull_block
        if output:
            payload["output_path"] = output
        print(_errors.safe_json_dumps(payload, indent=2))
        return 0

    print(f"hotato {stack} health: pulled {len(res['pulled'])} of "
          f"{res['listed']} listed call{'s' if res['listed'] != 1 else ''} "
          f"({len(res['skipped'])} skipped, last {last}) -> {out_dir}")
    if allow_mono:
        print(f"  {_autopsy.MONO_SCOPE_NOTE}")
    print(_scanfolder.render_text(result, prior_runs))
    if output:
        print(f"  report also written to {output}")
    return 0
