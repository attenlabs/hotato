"""``hotato scan <directory>``: the folder health report.

Point ``hotato scan`` at a folder of call recordings and it runs the autopsy
engine (:func:`hotato.autopsy.run_autopsy`) over EVERY audio file -- stereo
through the existing deterministic whole-call scanner, mono best-effort with
measured confidences, exactly autopsy's own rules -- then aggregates the
incidents into one folder report:

  * the HEALTH headline is a measured share: "N of M dual-channel calls had
    no critical incidents (X%)". The denominator is DUAL-CHANNEL
    deterministic-timing calls ONLY -- a mono call never enters this rate;
    mono-analyzed calls are reported in their own "Best-effort mono
    observations" block with their own counts and an authority label. It is
    never a blended 0-100 quality score -- METHODOLOGY.md's rule (one
    blended number hides exactly the distinction the tool exists to draw)
    applies to the folder view too. Every category and every call keeps its
    own measured numbers beside the share.
  * the VOICE STABILITY SCORE is that same share, times 100 -- nothing
    blended, no weights, no other arithmetic: ``round(share x 100)``. The
    machine field is ``critical_free_call_rate``. It is printed with the
    measured share line directly beneath it as the formula, with the
    eligible sample size and the analysis-policy sha beside it, and a
    small-sample label when the eligible sample is under 20 dual-channel
    calls. With zero dual-channel calls no score renders and the report
    states why (no 0/0 theater).
  * an EVIDENCE COVERAGE block lists per-lane measured counts from what the
    run actually had -- dual-channel timing, mono best-effort, refused with
    reasons. A lane never renders as assessed when its evidence was absent
    from the run.
  * RECURRENCE lines carry a measured state: an incident kind present in
    THIS run that also appears in stored prior runs of the same directory
    prints as ``observed`` (1-2 calls in the stored window), ``RECURRING``
    (3+), ``RECURRING, LOW SAMPLE`` (3+ but the eligible dual-channel
    sample is under 20), or ``ELEVATED`` (20+ eligible dual-channel calls
    in both compared runs, same policy and coverage, and Wilson 95%
    intervals on the kind's per-call rate that do not overlap). Lines
    derive only from the stored summary envelopes (measured facts, with
    each prior run's ``recorded_at`` provenance), never extrapolation, so
    the same directory + the same prior-run store always prints the same
    lines.
  * a per-category incident breakdown: counts plus the worst measured
    magnitude in each category (overlap seconds, gap seconds, silence
    seconds -- the scanner's own numbers, restated, never combined).
  * the worst-calls ranking (critical count first, then worst measured
    magnitude), each row linking to that call's own per-call autopsy report,
    which is generated alongside.
  * unreadable files are listed as REFUSED with the reason -- never skipped
    silently and never scored.
  * est. cost totals render ONLY under ``--cost-config`` (the operator's own
    per-incident figures; hotato ships no default dollar amount).

Everything is offline and deterministic: the walk order is the sorted
relative path list, and the same directory with the same flags produces
byte-identical CLI text and a byte-identical HTML report. Output naming is
content-addressed like autopsy's: the scan id is ``scn-`` + the first 12 hex
chars of the sha256 over the sorted (relative path, file sha256) manifest
plus the analysis flags, so the report lands at
``hotato-output/scan-<id>.html`` with a machine-readable summary envelope at
``hotato-output/scan-<id>.json``.

TREND: each summary envelope carries a ``dir_key`` (derived from the scanned
directory's resolved path) and a ``recorded_at`` provenance timestamp,
stamped once when the envelope is first written (a re-run of unchanged
content resolves to the same id and leaves the stored envelope untouched).
When prior envelopes for the same directory exist in the output dir, the
report renders a run-over-run trend strip from them; the current run's own
envelope is excluded, so the page stays byte-identical for the same
directory + the same prior-run store.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import List, Optional, Tuple

from . import autopsy as _autopsy
from .errors import open_regular as _open_regular

__all__ = [
    "AUDIO_EXTS",
    "SCAN_FOLDER_NOTE",
    "SCORE_HOW_NOTE",
    "MONO_OBSERVATIONS_LABEL",
    "MONO_OBSERVATIONS_NOTE",
    "SMALL_SAMPLE_MIN_CALLS",
    "policy_sha",
    "run_scan_folder",
    "build_envelope",
    "load_prior_runs",
    "persist_run",
    "fleet_alerts",
    "fleet_alert_text",
    "build_scan_report_html",
    "render_text",
]

# The audio inputs the autopsy engine accepts: WAV natively, mp3/m4a through
# ffmpeg when it is on PATH (a missing ffmpeg surfaces per file as a refusal
# with the actionable reason, exactly like `hotato autopsy` on that file).
AUDIO_EXTS = (".wav", ".mp3", ".m4a")

SCAN_FOLDER_NOTE = (
    "The health figure is a measured share -- dual-channel calls with zero "
    "critical incidents over dual-channel calls analyzed -- never a blended "
    "quality score; every category and every call keeps its own measured "
    "numbers."
)

# The one-line derivation printed beside the Voice Stability Score in the
# HTML report, pointing at the measured share line it restates.
SCORE_HOW_NOTE = (
    "How this is calculated: the score is the measured share line above, "
    "times 100 -- dual-channel calls with zero critical incidents over "
    "dual-channel calls analyzed (deterministic timing). No weights, no "
    "other arithmetic; mono calls are separate best-effort observations."
)

# The dual-channel eligible-sample bar: under this many dual-channel calls
# the score renders with a small-sample label, and a 3+ recurrence reads
# RECURRING, LOW SAMPLE instead of RECURRING.
SMALL_SAMPLE_MIN_CALLS = 20

# The separate block a mono-analyzed call reports into: its own counts under
# an authority label. A mono call never enters the Voice Stability
# denominator.
MONO_OBSERVATIONS_LABEL = "Best-effort mono observations"
MONO_OBSERVATIONS_NOTE = (
    "Measured silence timing from one mixed channel, each finding with its "
    "measured confidence; talk-over and barge-in attribution comes from a "
    "two-channel recording. These calls carry their own counts and never "
    "enter the Voice Stability denominator."
)

# incident kind key -> the measurement field that carries its magnitude, in
# lookup order (an incident reports the first of these it measured).
_MAGNITUDE_FIELDS = (
    ("overlap_sec", "overlap"),
    ("gap_sec", "gap"),
    ("silence_sec", "silence"),
    ("trailing_silence_sec", "trailing silence"),
    ("activity_sec", "activity"),
)

_CHUNK_BYTES = 1 << 20


# --- deterministic discovery + the content-derived ids ----------------------

def _iter_audio(folder: str) -> List[Tuple[str, str]]:
    """Every recording under ``folder`` (by :data:`AUDIO_EXTS`) as
    ``(relpath, abspath)``, sorted by relpath with forward slashes, so the
    walk order -- and therefore every byte of output -- is deterministic
    across runs and machines (the same normalization ``analyze`` uses)."""
    found = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in files:
            if name.lower().endswith(AUDIO_EXTS):
                ap = os.path.join(root, name)
                rel = os.path.relpath(ap, folder).replace(os.sep, "/")
                found.append((rel, ap))
    found.sort(key=lambda pair: pair[0])
    return found


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with _open_regular(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_id(entries: List[Tuple[str, str]], min_gap_sec: float) -> str:
    """``scn-`` + 12 hex of the sha256 over the sorted (relpath, sha256)
    manifest plus the analysis flags: the same directory content analyzed
    with the same flags gets the same id -- and the same output paths -- on
    every machine."""
    lines = [f"{rel}\t{digest}" for rel, digest in entries]
    lines.append(f"min_gap_sec={min_gap_sec}")
    h = hashlib.sha256("\n".join(lines).encode("utf-8"))
    return "scn-" + h.hexdigest()[:12]


def policy_sha(min_gap_sec: float) -> str:
    """12 hex chars identifying the analysis policy behind a run: the flags
    plus the severity bars that decide what counts as critical. Printed
    beside the score, stored in the summary envelope, and required to match
    before two runs are compared for the ELEVATED recurrence state."""
    doc = {
        "min_gap_sec": float(min_gap_sec),
        "talk_over_critical_sec": _autopsy.TALK_OVER_CRITICAL_SEC,
        "dead_air_critical_sec": _autopsy.DEAD_AIR_CRITICAL_SEC,
    }
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _dir_key(folder: str) -> str:
    """12 hex chars keying THIS directory across runs (from its resolved
    path), so prior summary envelopes for the same directory are findable in
    the output dir whatever the directory's contents were at the time."""
    resolved = os.path.realpath(os.path.abspath(folder))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


# --- magnitudes (the scanner's own numbers, restated, never combined) --------

def _incident_magnitude(inc: dict) -> Optional[dict]:
    """The one measured magnitude of an incident, as ``{"value", "measure"}``
    (e.g. 1.96 / "overlap"), read straight from the measurements the scanner
    reported. ``None`` when the incident carries none of the known fields."""
    m = inc.get("measurements") or {}
    for field, label in _MAGNITUDE_FIELDS:
        v = m.get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return {"value": round(float(v), 3), "measure": label}
    return None


def _worse(a: Optional[dict], b: Optional[dict]) -> Optional[dict]:
    if a is None:
        return b
    if b is None or a["value"] >= b["value"]:
        return a
    return b


def _magnitude_text(mag: Optional[dict]) -> str:
    if mag is None:
        return ""
    return f"{mag['value']:.2f}s {mag['measure']}"


# --- the run -----------------------------------------------------------------

def run_scan_folder(
    folder: str,
    *,
    cost_config: Optional[dict] = None,
    min_gap_sec: float = 2.0,
) -> Tuple[dict, List[Tuple[dict, str]]]:
    """Run the autopsy engine over every recording under ``folder`` and
    aggregate the incidents into the folder health result.

    Returns ``(result, calls_raw)``: ``result`` is the JSON-serializable
    aggregate, and ``calls_raw`` is the per-call ``(autopsy_result,
    report_html)`` list for the caller to write alongside (the worst-calls
    ranking links to those reports). An unreadable file becomes a ``refused``
    row with its reason -- never a silent skip, never a crash. Raises
    ``ValueError`` (CLI exit 2) when ``folder`` is not a directory or holds
    no recordings at all."""
    # A bad --min-gap is ONE usage mistake, not a property of any recording:
    # validate it up front (exit-2 usage error) so it can never degrade into
    # a refusal row per file (the same rule analyze.validate_scan_args pins).
    if min_gap_sec <= 0:
        raise ValueError(f"--min-gap must be > 0 seconds; got {min_gap_sec}.")
    if not os.path.isdir(folder):
        raise ValueError(
            f"{folder!r} is not a directory. Point hotato scan at a folder "
            "of call recordings (hotato scan ./calls), or scan one recording "
            "with hotato scan --stereo call.wav."
        )
    files = _iter_audio(folder)
    if not files:
        raise ValueError(
            f"{folder!r} has no call recordings "
            f"({', '.join('*' + e for e in AUDIO_EXTS)}). Point hotato scan "
            "at a folder of recordings, or analyze one file with "
            "hotato autopsy RECORDING."
        )

    manifest: List[Tuple[str, str]] = []
    calls_raw: List[Tuple[dict, str]] = []
    calls: List[dict] = []
    refused: List[dict] = []
    total_crit = 0
    total_warn = 0
    by_kind: dict = {}

    for rel, ap in files:
        try:
            manifest.append((rel, _file_sha256(ap)))
        except (ValueError, OSError) as exc:
            manifest.append((rel, "unreadable"))
            refused.append({"file": rel, "reason": str(exc)})
            continue
        try:
            call_result, call_html = _autopsy.run_autopsy(
                ap, cost_config=cost_config, min_gap_sec=min_gap_sec)
        except (ValueError, OSError) as exc:
            refused.append({"file": rel, "reason": str(exc)})
            continue
        calls_raw.append((call_result, call_html))

        worst = None
        for inc in call_result["incidents"]:
            mag = _incident_magnitude(inc)
            worst = _worse(worst, mag)
            k = inc["kind_key"]
            slot = by_kind.setdefault(
                k, {"kind_key": k, "kind": inc["kind"], "count": 0,
                    "critical": 0, "worst": None})
            slot["count"] += 1
            if inc["severity"] == "CRITICAL":
                slot["critical"] += 1
            slot["worst"] = _worse(slot["worst"], mag)
        n_crit = call_result["summary"]["critical"]
        n_warn = call_result["summary"]["warning"]
        total_crit += n_crit
        total_warn += n_warn
        calls.append({
            "source": rel,
            "autopsy_id": call_result["id"],
            "mode": call_result["mode"],
            "duration_sec": call_result["duration_sec"],
            "critical": n_crit,
            "warning": n_warn,
            "incidents": call_result["total_incidents"],
            # The distinct incident kinds THIS call carries (sorted for
            # determinism): what the recurrence-state check counts
            # ("<kind> in N of M calls this run").
            "kinds": sorted({inc["kind_key"]
                             for inc in call_result["incidents"]}),
            "worst": worst,
            "report_path": call_result["report_path"],
        })

    # Worst first: critical count, then worst measured magnitude, then the
    # stable relpath tiebreak.
    calls.sort(key=lambda c: (
        -c["critical"],
        -(c["worst"]["value"] if c["worst"] else 0.0),
        c["source"],
    ))

    categories = [by_kind[k] for k in _autopsy.COST_KIND_KEYS if k in by_kind]

    n_analyzed = len(calls)
    # The Voice Stability denominator is dual-channel deterministic-timing
    # calls ONLY: a mono call never enters the rate. Mono calls report into
    # their own best-effort observations block below.
    dual_calls = [c for c in calls if c["mode"] != "mono"]
    mono_calls = [c for c in calls if c["mode"] == "mono"]
    n_dual = len(dual_calls)
    n_dual_clean = sum(1 for c in dual_calls if c["critical"] == 0)
    if n_dual:
        pct = f"{100.0 * n_dual_clean / n_dual:.0f}"
        headline = (f"{n_dual_clean} of {n_dual} dual-channel calls had no "
                    f"critical incidents ({pct}%)")
        share = round(n_dual_clean / n_dual, 4)
        # The Voice Stability Score IS the share, times 100 -- the branded
        # number restates the measured share, with no weights and no other
        # arithmetic; the share line renders directly beneath it as the
        # formula. The machine field is critical_free_call_rate.
        critical_free_call_rate = int(round(100.0 * n_dual_clean / n_dual))
    elif mono_calls:
        # No 0/0 theater, and no score over a denominator the run did not
        # have: with zero dual-channel calls the report states why instead.
        headline = (
            f"no score: 0 dual-channel calls analyzed -- the Voice "
            f"Stability denominator counts dual-channel deterministic-"
            f"timing calls only; {len(mono_calls)} mono call"
            f"{'s' if len(mono_calls) != 1 else ''} reported below as "
            "best-effort observations")
        share = None
        critical_free_call_rate = None
    else:
        headline = (f"0 calls analyzed ({len(refused)} refused; the reasons "
                    "are listed)")
        share = None
        # No 0/0 theater: with zero analyzed calls no score exists.
        critical_free_call_rate = None

    mono_block = None
    if mono_calls:
        mono_block = {
            "label": MONO_OBSERVATIONS_LABEL,
            "note": MONO_OBSERVATIONS_NOTE,
            "calls_analyzed": len(mono_calls),
            "calls_no_critical": sum(
                1 for c in mono_calls if c["critical"] == 0),
            "critical": sum(c["critical"] for c in mono_calls),
            "warning": sum(c["warning"] for c in mono_calls),
        }

    # Evidence coverage: per-lane measured counts from what THIS run
    # actually had. A lane with no evidence in the run never renders, so
    # nothing reads as assessed on absent evidence.
    coverage = []
    if n_dual:
        coverage.append({
            "lane": "dual-channel timing", "calls": n_dual,
            "detail": "deterministic two-channel timing walk",
        })
    if mono_calls:
        coverage.append({
            "lane": "mono best-effort", "calls": len(mono_calls),
            "detail": ("measured-confidence silence timing from one mixed "
                       "channel"),
        })
    if refused:
        coverage.append({
            "lane": "refused", "calls": len(refused),
            "detail": ("unreadable as call audio; every file listed with "
                       "its reason, never scored"),
        })

    cost_summary = None
    if cost_config:
        total = 0.0
        priced = 0
        for call_result, _ in calls_raw:
            c = call_result.get("cost")
            if c:
                total += c["total"]
                priced += c["priced_incidents"]
        cost_summary = {
            "total": round(total, 2),
            "currency": cost_config["currency"],
            "priced_incidents": priced,
            "source": cost_config["source"],
        }

    scan_id = _scan_id(manifest, min_gap_sec)
    result = {
        "tool": "hotato",
        "kind": "scan-folder",
        "schema_version": "1",
        "id": scan_id,
        "dir_key": _dir_key(folder),
        "directory": os.path.basename(os.path.normpath(folder)) or folder,
        "directory_path": os.path.abspath(folder),
        "note": SCAN_FOLDER_NOTE,
        "config": {"min_gap_sec": min_gap_sec},
        "counts": {
            "scanned": len(files),
            "analyzed": n_analyzed,
            "refused": len(refused),
        },
        "policy_sha": policy_sha(min_gap_sec),
        "health": {
            "calls_no_critical": n_dual_clean,
            "calls_analyzed": n_dual,
            "share": share,
            "critical_free_call_rate": critical_free_call_rate,
            "small_sample": bool(n_dual and n_dual < SMALL_SAMPLE_MIN_CALLS),
            "headline": headline,
            "no_score_reason": None if n_dual else headline,
        },
        "mono": mono_block,
        "coverage": coverage,
        "incidents": {"critical": total_crit, "warning": total_warn},
        "categories": categories,
        "calls": calls,
        "refused": refused,
        "cost": cost_summary,
        "report_path": os.path.join(_autopsy.OUT_DIR, f"scan-{scan_id}.html"),
        "envelope_path": os.path.join(_autopsy.OUT_DIR, f"scan-{scan_id}.json"),
    }
    return result, calls_raw


# --- the summary envelope + the prior-run store -------------------------------

def build_envelope(result: dict, recorded_at: str) -> dict:
    """The machine-readable scan summary envelope: the measured aggregate
    minus the cost-rendering layer (est. cost figures exist only on surfaces
    rendered under ``--cost-config``; the stored envelope is the measured
    facts, so its bytes are the same whatever flags rendered the run).
    ``recorded_at`` is provenance -- stamped once, when the envelope is first
    written; a re-run of unchanged content resolves to the same
    content-addressed path and leaves the stored file untouched."""
    return {
        "tool": "hotato",
        "kind": "scan-summary",
        "schema_version": "1",
        "id": result["id"],
        "dir_key": result["dir_key"],
        "directory": result["directory"],
        "directory_path": result["directory_path"],
        "recorded_at": recorded_at,
        "note": result["note"],
        "config": result["config"],
        "policy_sha": result["policy_sha"],
        "counts": result["counts"],
        "health": result["health"],
        "mono": result["mono"],
        "coverage": result["coverage"],
        "incidents": result["incidents"],
        "categories": result["categories"],
        "calls": result["calls"],
        "refused": result["refused"],
    }


def load_prior_runs(out_dir: str, dir_key: str, current_id: str) -> List[dict]:
    """Prior scan summary envelopes for the same directory (matched by
    ``dir_key``) in ``out_dir``, oldest first by their stored ``recorded_at``
    provenance. The current run's own id is excluded, so re-rendering the
    same content never lists itself as a prior run. A file that is not a
    scan-summary envelope is ignored; the store holds only what this command
    wrote."""
    rows: List[dict] = []
    if not os.path.isdir(out_dir):
        return rows
    for name in sorted(os.listdir(out_dir)):
        if not (name.startswith("scan-") and name.endswith(".json")):
            continue
        try:
            with _open_regular(os.path.join(out_dir, name), "r",
                               encoding="utf-8") as fh:
                doc = json.load(fh)
        except (ValueError, OSError):
            continue
        if not isinstance(doc, dict) or doc.get("kind") != "scan-summary":
            continue
        if doc.get("dir_key") != dir_key or doc.get("id") == current_id:
            continue
        health = doc.get("health") or {}
        incidents = doc.get("incidents") or {}
        categories = doc.get("categories") or []
        # Per-call stored facts: what the recurrence states count. Every
        # number here is read from the stored envelope, never recomputed
        # from audio.
        calls_list = [c for c in (doc.get("calls") or [])
                      if isinstance(c, dict)]
        kind_call_counts: dict = {}
        dual_kind_call_counts: dict = {}
        eligible = 0
        for c in calls_list:
            kinds = c.get("kinds") or []
            dual = bool(c.get("mode")) and c.get("mode") != "mono"
            if dual:
                eligible += 1
            for k in kinds:
                kind_call_counts[k] = kind_call_counts.get(k, 0) + 1
                if dual:
                    dual_kind_call_counts[k] = (
                        dual_kind_call_counts.get(k, 0) + 1)
        lanes = sorted({"mono" if c.get("mode") == "mono" else "dual"
                        for c in calls_list if c.get("mode")})
        config = doc.get("config") or {}
        stored_policy = doc.get("policy_sha")
        if not stored_policy:
            try:
                stored_policy = policy_sha(float(config["min_gap_sec"]))
            except (KeyError, TypeError, ValueError):
                stored_policy = None
        rows.append({
            "id": doc.get("id"),
            "recorded_at": str(doc.get("recorded_at") or ""),
            "analyzed": health.get("calls_analyzed"),
            "calls_no_critical": health.get("calls_no_critical"),
            "share": health.get("share"),
            "critical_incidents": incidents.get("critical"),
            # The incident kinds that run measured (from its stored
            # per-category counts): what the recurrence check reads.
            # Stored facts only, never recomputed.
            "kind_keys": sorted({
                c["kind_key"] for c in categories
                if isinstance(c, dict) and c.get("kind_key")
                and (c.get("count") or 0) > 0
            }),
            "kind_call_counts": kind_call_counts,
            "dual_kind_call_counts": dual_kind_call_counts,
            "eligible": eligible,
            "lanes": lanes,
            "policy_sha": stored_policy,
        })
    rows.sort(key=lambda r: (r["recorded_at"], str(r["id"])))
    return rows


def persist_run(result: dict, calls_raw: List[Tuple[dict, str]]) -> List[dict]:
    """Write one folder run's artifacts exactly as ``hotato scan DIR`` does:
    the per-call autopsy reports and envelopes the worst-calls ranking links
    to, the folder report HTML, and -- only when this content was not stored
    before -- the summary envelope with its ``recorded_at`` provenance
    stamped once (a re-run of unchanged content resolves to the same
    content-addressed path and leaves the stored file untouched). Returns
    the prior runs of the same directory, loaded BEFORE this run's envelope
    lands so the current run never lists itself."""
    from . import autopsy as _autopsy
    from .cli import _atomic_write_json, _atomic_write_text

    out_dir = os.path.dirname(result["report_path"])
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    for call_result, call_html in calls_raw:
        _atomic_write_text(call_result["report_path"], call_html)
        _atomic_write_json(call_result["envelope_path"],
                           _autopsy.envelope_dict(call_result))
    prior_runs = load_prior_runs(out_dir, result["dir_key"], result["id"])
    _atomic_write_text(result["report_path"],
                       build_scan_report_html(result, prior_runs))
    if not os.path.exists(result["envelope_path"]):
        import datetime as _dt

        recorded_at = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        _atomic_write_json(result["envelope_path"],
                           build_envelope(result, recorded_at))
    return prior_runs


# --- recurrence states (stored envelopes only, never extrapolation) -----------

def fleet_alerts(result: dict, prior_runs: List[dict]) -> List[dict]:
    """Recurrence lines with a measured state, for every incident kind
    present in THIS run that also appears in at least one stored prior run
    of the same directory. Every number is a stored or freshly measured
    fact -- the current run's per-call kinds and the prior envelopes'
    per-call counts with their ``recorded_at`` provenance -- so the same
    store always yields the same lines, in the category order the report
    already uses.

    States (the occurrence count is calls carrying the kind across this
    run plus the stored prior runs; the eligible sample is dual-channel
    calls, the same denominator the score uses):

      * ``observed`` -- 1-2 occurrences in the stored window;
      * ``RECURRING`` -- 3+ occurrences;
      * ``RECURRING, LOW SAMPLE`` -- 3+ occurrences but this run's eligible
        sample is under :data:`SMALL_SAMPLE_MIN_CALLS`;
      * ``ELEVATED`` -- this run and the most recent comparable prior run
        (same policy sha, same evidence lanes, both with 20+ eligible
        dual-channel calls) have Wilson 95% intervals on the kind's
        per-call rate that do not overlap, this run higher. Reuses the
        repository's Wilson helper (``simulate._wilson_ci``)."""
    alerts = []
    calls = result["calls"]
    analyzed = result["counts"]["analyzed"]
    eligible = result["health"]["calls_analyzed"]
    this_lanes = sorted({"mono" if c.get("mode") == "mono" else "dual"
                         for c in calls})
    this_policy = result.get("policy_sha")
    for cat in result["categories"]:
        kind_key = cat["kind_key"]
        in_calls = sum(1 for c in calls if kind_key in (c.get("kinds") or ()))
        in_dual = sum(1 for c in calls
                      if c.get("mode") != "mono"
                      and kind_key in (c.get("kinds") or ()))
        prior_with = [r for r in prior_runs
                      if kind_key in (r.get("kind_keys") or ())]
        if not (in_calls and prior_with):
            continue
        prior_dates = [r["recorded_at"] for r in prior_with]
        occurrences = in_calls + sum(
            (r.get("kind_call_counts") or {}).get(kind_key, 0)
            for r in prior_with)
        state = "observed"
        elevated_vs = None
        if occurrences >= 3:
            if eligible < SMALL_SAMPLE_MIN_CALLS:
                state = "RECURRING, LOW SAMPLE"
            else:
                state = "RECURRING"
                comparable = [
                    r for r in prior_runs
                    if (r.get("eligible") or 0) >= SMALL_SAMPLE_MIN_CALLS
                    and this_policy
                    and r.get("policy_sha") == this_policy
                    and (r.get("lanes") or []) == this_lanes
                ]
                if comparable:
                    from .simulate import _wilson_ci

                    prior = comparable[-1]  # rows are oldest first
                    ci_now = _wilson_ci(in_dual, eligible)
                    ci_prior = _wilson_ci(
                        (prior.get("dual_kind_call_counts") or {})
                        .get(kind_key, 0),
                        prior["eligible"])
                    if (ci_now and ci_prior
                            and ci_now["low"] > ci_prior["high"]):
                        state = "ELEVATED"
                        elevated_vs = prior["recorded_at"]
        alerts.append({
            "kind_key": kind_key,
            "kind": cat["kind"],
            "state": state,
            "calls_this_run": in_calls,
            "calls_analyzed": analyzed,
            "occurrences": occurrences,
            "eligible": eligible,
            "prior_runs": len(prior_with),
            "prior_dates": prior_dates,
            "elevated_vs": elevated_vs,
        })
    return alerts


def fleet_alert_text(alert: dict) -> str:
    dates = ", ".join(alert["prior_dates"])
    line = (f"{alert['state']}: {alert['kind']} in "
            f"{alert['calls_this_run']} of {alert['calls_analyzed']} calls "
            f"this run ({alert['occurrences']} in the stored window). Also "
            f"present in {alert['prior_runs']} prior run(s): {dates}.")
    if alert["state"] == "RECURRING, LOW SAMPLE":
        line += (f" Eligible sample: {alert['eligible']} dual-channel call"
                 f"{'s' if alert['eligible'] != 1 else ''}, under "
                 f"{SMALL_SAMPLE_MIN_CALLS}.")
    if alert.get("elevated_vs"):
        line += (f" Wilson 95% intervals do not overlap with the run "
                 f"recorded {alert['elevated_vs']} "
                 f"({SMALL_SAMPLE_MIN_CALLS}+ eligible dual-channel calls "
                 "in both runs, same policy and coverage).")
    return line


# --- CLI text ------------------------------------------------------------------

def render_text(result: dict, prior_runs: List[dict]) -> str:
    counts = result["counts"]
    lines = [
        f"hotato scan: {result['directory']}  ({counts['scanned']} recording"
        f"{'s' if counts['scanned'] != 1 else ''}: {counts['analyzed']} "
        f"analyzed, {counts['refused']} refused)",
    ]
    health = result["health"]
    score = health.get("critical_free_call_rate")
    if score is not None:
        # The branded number IS the measured share, times 100; the share
        # line renders directly beneath it as the formula, with the
        # eligible sample size and the analysis-policy sha beside the
        # score. With zero dual-channel calls no score line exists (the
        # headline states why instead; no 0/0 theater).
        n_dual = health["calls_analyzed"]
        lines.append(
            f"  Voice Stability Score: {score}/100  "
            f"({n_dual} dual-channel call{'s' if n_dual != 1 else ''}; "
            f"policy {result['policy_sha']})")
        if health.get("small_sample"):
            lines.append(
                f"  SMALL SAMPLE: {n_dual} dual-channel call"
                f"{'s' if n_dual != 1 else ''}, under the "
                f"{SMALL_SAMPLE_MIN_CALLS}-call bar")
    lines.append(f"  health: {health['headline']}")
    lines.append(f"  {SCAN_FOLDER_NOTE}")
    mono = result.get("mono")
    if mono:
        n_mono = mono["calls_analyzed"]
        lines.append(
            f"  {mono['label']}: {n_mono} call"
            f"{'s' if n_mono != 1 else ''}, {mono['calls_no_critical']} "
            f"with no critical findings ({mono['critical']} critical, "
            f"{mono['warning']} warning)")
        lines.append(f"    {mono['note']}")
    if result.get("coverage"):
        lines.append("  evidence coverage (measured from this run):")
        for lane in result["coverage"]:
            unit = "file" if lane["lane"] == "refused" else "call"
            lines.append(
                f"    {lane['lane']}: {lane['calls']} {unit}"
                f"{'s' if lane['calls'] != 1 else ''} -- {lane['detail']}")
    for alert in fleet_alerts(result, prior_runs):
        lines.append("  " + fleet_alert_text(alert))
    if result["categories"]:
        lines.append("  incidents by category:")
        for cat in result["categories"]:
            worst = _magnitude_text(cat["worst"])
            lines.append(
                f"    {cat['kind']:<14} {cat['count']:>3}"
                f"  ({cat['critical']} critical)"
                + (f"  worst {worst}" if worst else "")
            )
    elif counts["analyzed"]:
        lines.append("  0 incidents across the analyzed calls: no overlap "
                     "onsets and no silence gaps crossed the bar")
    if result["calls"]:
        lines.append("  worst calls (critical count, then worst measured "
                     "magnitude):")
        for i, c in enumerate(result["calls"][:10], 1):
            worst = _magnitude_text(c["worst"])
            lines.append(
                f"    {i:>2}. {c['source']}  {c['autopsy_id']}  "
                f"{c['critical']} critical, {c['warning']} warning"
                + (f"  worst {worst}" if worst else "")
            )
        if len(result["calls"]) > 10:
            lines.append(f"    ... and {len(result['calls']) - 10} more in "
                         "the report")
    if result["refused"]:
        lines.append("  refused (unreadable, with the reason; never scored):")
        for r in result["refused"]:
            lines.append(f"    {r['file']}: {r['reason']}")
    cost = result.get("cost")
    if cost and cost["priced_incidents"]:
        lines.append(
            f"  est. cost total: {_autopsy._money(cost['total'], cost['currency'])} "
            f"({cost['priced_incidents']} priced incident"
            f"{'s' if cost['priced_incidents'] != 1 else ''}; "
            f"your figures from {cost['source']})"
        )
    if prior_runs:
        lines.append(
            f"  trend: {len(prior_runs)} prior run"
            f"{'s' if len(prior_runs) != 1 else ''} of this directory in the "
            "store; the report renders the run-over-run strip"
        )
    lines.append(f"  report:   {result['report_path']}")
    lines.append(f"  envelope: {result['envelope_path']}")
    if result["calls"]:
        lines.append(
            "  pin: hotato pin <autopsy-id> pins a call's top critical "
            "incident as a contract (each call's id is listed above)"
        )
    return "\n".join(lines)


# --- the self-contained HTML report --------------------------------------------

_EXTRA_CSS = """
.scoreline{font-size:27px;font-weight:800;margin:2px 0 4px}
.healthline{font-size:22px;font-weight:750;margin:2px 0 6px}
.healthnote{color:%(muted)s;font-size:12.5px}
.alertrow{color:%(red)s;font-weight:700;font-size:13px;margin:6px 0}
.trow{display:flex;gap:12px;align-items:baseline;margin:5px 0}
.tstamp{min-width:180px;color:%(muted)s;font-size:12.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tshare{color:%(cream)s;font-size:13px;font-weight:600}
.tcrit{color:%(muted)s;font-size:12.5px}
.catrow{display:flex;gap:12px;align-items:baseline;margin:6px 0}
.catk{min-width:150px;font-weight:650;font-size:13.5px}
.catn{color:%(cream)s;font-size:13px}
.catw{color:%(muted)s;font-size:12.5px}
.crow{display:flex;gap:12px;align-items:baseline;margin:6px 0;flex-wrap:wrap}
.crank{font-weight:800;font-size:12.5px;color:#15110d;background:%(caller)s;
 border-radius:7px;padding:2px 9px}
.cfile{min-width:240px;font-size:12.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.cfile a{color:%(cream)s}
.cnum{color:%(muted)s;font-size:12.5px}
.ccrit{color:%(red)s;font-weight:700;font-size:12.5px}
.skiprow{display:flex;gap:10px;margin:6px 0 2px}
.skipf{min-width:220px;color:%(cream)s;font-size:12.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.skipr{color:%(muted)s;font-size:12.5px}
.scopenote{color:%(muted)s;font-size:12.5px;margin:6px 0 12px}
"""


def build_scan_report_html(result: dict, prior_runs: List[dict]) -> str:
    """ONE self-contained HTML folder report in the report/autopsy house
    style: the measured health share as the headline (never a blended
    score), the run-over-run trend strip when prior envelopes exist, the
    per-category breakdown, the worst-calls ranking (each row linking to
    that call's per-call autopsy report next to this file), and the refused
    list with reasons. Zero external requests, zero scripts, no wall clock
    of its own -- the only timestamps on the page are the stored provenance
    of prior runs -- so the same directory + the same prior-run store render
    the same bytes."""
    from . import report as _report

    esc = _report._esc
    css = _report._CSS % _report._C + _EXTRA_CSS % _report._C
    counts = result["counts"]
    health = result["health"]
    inc = result["incidents"]

    body = [
        '<main class="wrap">',
        '<header class="top"><div class="logo"></div><div>',
        '<h1 class="h1">hotato scan</h1>',
        f'<div class="tagline">{esc(result["directory"])} &middot; '
        f'{counts["scanned"]} recording{"s" if counts["scanned"] != 1 else ""}'
        '</div>',
        '<div class="metarow">'
        f'<span class="pill"><b>{counts["analyzed"]}</b> analyzed</span>'
        f'<span class="pill"><b>{counts["refused"]}</b> refused</span>'
        f'<span class="pill"><b>{inc["critical"]}</b> critical incident'
        f'{"" if inc["critical"] == 1 else "s"}</span>'
        f'<span class="pill"><b>{inc["warning"]}</b> warning'
        f'{"" if inc["warning"] == 1 else "s"}</span>'
        '<span class="pill">offline <b>yes</b></span>'
        f'<span class="pill">id <b>{esc(result["id"])}</b></span>'
        '</div></div></header>',
    ]

    score = health.get("critical_free_call_rate")
    health_bits = ['<section class="card"><div class="ctitle">Health</div>']
    if score is not None:
        # The score IS the measured share, times 100; the share line sits
        # directly beneath it as the formula, the eligible sample size and
        # the analysis-policy sha beside the score, and the derivation note
        # points at the share line. Zero dual-channel calls render no score
        # (the headline states why; no 0/0 theater).
        n_dual = health["calls_analyzed"]
        health_bits.append(
            f'<div class="scoreline">Voice Stability Score: {score}/100</div>'
        )
        health_bits.append(
            f'<div class="healthnote">{n_dual} dual-channel call'
            f'{"s" if n_dual != 1 else ""} &middot; policy '
            f'{esc(result["policy_sha"])}</div>')
        if health.get("small_sample"):
            health_bits.append(
                f'<div class="alertrow">SMALL SAMPLE: {n_dual} dual-channel '
                f'call{"s" if n_dual != 1 else ""}, under the '
                f'{SMALL_SAMPLE_MIN_CALLS}-call bar</div>')
    health_bits.append(
        f'<div class="healthline">{esc(health["headline"])}</div>')
    if score is not None:
        health_bits.append(
            f'<div class="healthnote">{esc(SCORE_HOW_NOTE)}</div>')
    health_bits.append(
        f'<div class="healthnote">{esc(SCAN_FOLDER_NOTE)}</div></section>')
    body.append("".join(health_bits))

    mono = result.get("mono")
    if mono:
        n_mono = mono["calls_analyzed"]
        body.append(
            '<section class="card"><div class="ctitle">'
            f'{esc(mono["label"])}</div>'
            f'<div class="healthline">{n_mono} call'
            f'{"s" if n_mono != 1 else ""}, {mono["calls_no_critical"]} '
            f'with no critical findings ({mono["critical"]} critical, '
            f'{mono["warning"]} warning)</div>'
            f'<div class="scopenote">{esc(mono["note"])}</div></section>'
        )

    if result.get("coverage"):
        rows = "".join(
            '<div class="catrow">'
            f'<span class="catk">{esc(lane["lane"])}</span>'
            f'<span class="catn">{lane["calls"]} '
            f'{"file" if lane["lane"] == "refused" else "call"}'
            f'{"s" if lane["calls"] != 1 else ""}</span>'
            f'<span class="catw">{esc(lane["detail"])}</span>'
            '</div>'
            for lane in result["coverage"]
        )
        body.append(
            '<section class="card"><div class="ctitle">Evidence coverage'
            '</div><div class="scopenote">Per-lane measured counts from '
            'what this run actually had; a lane with no evidence in the '
            'run never renders as assessed.</div>' + rows + '</section>'
        )

    alerts = fleet_alerts(result, prior_runs)
    if alerts:
        rows = "".join(
            f'<div class="alertrow">{esc(fleet_alert_text(a))}</div>'
            for a in alerts
        )
        body.append(
            '<section class="card"><div class="ctitle">Recurrence</div>'
            '<div class="scopenote">Incident kinds present in this run that '
            'also appear in stored prior runs of this directory, each with '
            'its measured state (observed / RECURRING / RECURRING, LOW '
            'SAMPLE / ELEVATED); every count and date comes from a measured '
            'aggregate or a stored summary envelope.</div>'
            + rows + '</section>'
        )

    if prior_runs:
        rows = "".join(
            '<div class="trow">'
            f'<span class="tstamp">{esc(r["recorded_at"])}</span>'
            f'<span class="tshare">{r["calls_no_critical"]} of '
            f'{r["analyzed"]} calls with no critical incidents</span>'
            f'<span class="tcrit">{r["critical_incidents"]} critical '
            f'incident{"" if r["critical_incidents"] == 1 else "s"}</span>'
            '</div>'
            for r in prior_runs
        )
        rows += (
            '<div class="trow"><span class="tstamp">this run</span>'
            f'<span class="tshare">{health["calls_no_critical"]} of '
            f'{health["calls_analyzed"]} calls with no critical incidents'
            '</span>'
            f'<span class="tcrit">{inc["critical"]} critical incident'
            f'{"" if inc["critical"] == 1 else "s"}</span></div>'
        )
        body.append(
            '<section class="card"><div class="ctitle">Run over run</div>'
            '<div class="scopenote">Prior runs of this directory, from the '
            'summary envelopes stored beside this report; each timestamp is '
            'the provenance recorded when that envelope was first written.'
            '</div>' + rows + '</section>'
        )

    if result["categories"]:
        rows = "".join(
            '<div class="catrow">'
            f'<span class="catk">{esc(cat["kind"])}</span>'
            f'<span class="catn">{cat["count"]} incident'
            f'{"" if cat["count"] == 1 else "s"} &middot; '
            f'{cat["critical"]} critical</span>'
            + (f'<span class="catw">worst {esc(_magnitude_text(cat["worst"]))}'
               '</span>' if cat["worst"] else '')
            + '</div>'
            for cat in result["categories"]
        )
        body.append(
            '<section class="card"><div class="ctitle">Incidents by category'
            '</div>' + rows + '</section>'
        )
    elif counts["analyzed"]:
        body.append(
            '<section class="card"><div class="subtle">0 incidents across '
            'the analyzed calls: no overlap onsets and no silence gaps '
            'crossed the bar.</div></section>'
        )

    if result["calls"]:
        rows = []
        for i, c in enumerate(result["calls"], 1):
            href = os.path.basename(c["report_path"])
            worst = _magnitude_text(c["worst"])
            crit_html = (
                f'<span class="ccrit">{c["critical"]} critical</span>'
                if c["critical"] else '<span class="cnum">0 critical</span>'
            )
            rows.append(
                '<div class="crow">'
                f'<span class="crank">#{i}</span>'
                f'<span class="cfile"><a href="{esc(href)}">'
                f'{esc(c["source"])}</a></span>'
                + crit_html +
                f'<span class="cnum">{c["warning"]} warning'
                f'{"" if c["warning"] == 1 else "s"} &middot; '
                f'{c["duration_sec"]:.1f}s &middot; {esc(c["mode"])}</span>'
                + (f'<span class="cnum">worst {esc(worst)}</span>'
                   if worst else '')
                + '</div>'
            )
        body.append(
            '<section class="card"><div class="ctitle">Worst calls</div>'
            '<div class="scopenote">Ranked by critical count, then worst '
            'measured magnitude; each call links to its own autopsy report '
            'next to this file.</div>' + "".join(rows) + '</section>'
        )

    if result["refused"]:
        rows = "".join(
            f'<div class="skiprow"><span class="skipf">{esc(r["file"])}</span>'
            f'<span class="skipr">{esc(r["reason"])}</span></div>'
            for r in result["refused"]
        )
        body.append(
            '<section class="card"><div class="ctitle">Refused files</div>'
            '<div class="scopenote">Not readable as call audio, reported '
            'with the reason; never scored, never counted in the health '
            'share.</div>' + rows + '</section>'
        )

    cost = result.get("cost")
    if cost and cost["priced_incidents"]:
        body.append(
            '<section class="card"><div class="ctitle">est. cost total: '
            f'{esc(_autopsy._money(cost["total"], cost["currency"]))}</div>'
            f'<div class="scopenote">{cost["priced_incidents"]} priced '
            f'incident{"" if cost["priced_incidents"] == 1 else "s"} across '
            f'the analyzed calls, your figures from {esc(cost["source"])}; '
            'with no cost config no figure renders.</div></section>'
        )

    body.append(
        '<footer class="foot"><div class="scopenote"><b>Method.</b> '
        'The autopsy engine over every recording: deterministic energy '
        'measurement over time, per-frame RMS, a transparent activity '
        'threshold, and the timing walk. The health figure is the measured '
        'share of analyzed calls with zero critical incidents; categories '
        'and calls keep their own measured numbers beside it. Offline; '
        'nothing leaves this file.</div></footer>'
    )
    body.append('</main>')

    if health.get("share") is not None:
        title = (f"hotato scan: {result['directory']}, "
                 f"{health['calls_no_critical']} of "
                 f"{health['calls_analyzed']} dual-channel calls with no "
                 "critical incidents")
    else:
        title = f"hotato scan: {result['directory']}"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title>"
        f"<style>{css}</style></head><body>"
        + "".join(body)
        + "</body></html>\n"
    )
