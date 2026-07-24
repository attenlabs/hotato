"""``hotato autopsy <recording>``: the zero-config forensics front door.

Drop one call recording in; get the incidents out. No scenarios, no labels,
no flags required: a WAV reads natively, an mp3/m4a converts through ffmpeg
when it is on PATH, and the output is the incident list -- barge-in,
talk-over, dead air, latency spikes -- each with a timestamp and the measured
magnitude, plus ONE self-contained HTML report (waveform, incident markers,
per-incident cards). Everything runs offline; no audio leaves the machine.

Two analysis modes, split by what the recording physically supports:

  * STEREO (two channels: caller on one, agent on the other) runs the
    EXISTING deterministic whole-call scanner (``hotato.scan``) unchanged --
    the same walk, the same numbers, byte-for-byte. Same file in, same bytes
    out, every run.
  * MONO (one mixed channel) is analyzed BEST-EFFORT with the same energy
    VAD: silence timing -- dead air, long gaps -- is measurable from one
    channel and is reported with a MEASURED confidence (how far the gap's
    energy sits below the speech-activity threshold). Talk-over and barge-in
    attribution comes from a two-channel recording, where the caller and the
    agent are physically separated; that functional scope is stated once in
    the output. Nothing is guessed: every mono finding is a measured silence
    span, and its confidence is derived from the measured energy margin.

An unreadable input is refused with the reason (exit 2), never scored.

Incident vocabulary (mapped 1:1 from the scanner's candidate kinds; the
scanner's ``candidate_plain_english`` sentence rides along unchanged):

  BARGE-IN        the caller took the floor while the agent was talking
                  (scan: ``overlap_while_agent_talking``). CRITICAL when the
                  agent kept talking over the caller for more than the
                  prompt-yield ceiling (1.0 s, the same bar
                  ``investigate label`` pins) or never went quiet in the
                  search window.
  TALK-OVER       the agent started a fresh utterance over the caller
                  (scan: ``agent_start_during_caller``). CRITICAL past the
                  same 1.0 s ceiling.
  DEAD AIR        a long everything-stopped gap (scan: ``long_response_gap``
                  at >= 5.0 s, ``agent_stop_no_caller``, or a mono silence
                  span >= 5.0 s). CRITICAL at >= 5.0 s.
  LATENCY SPIKE   a response gap over the 2.0 s minimum but under the
                  dead-air bar (scan: ``long_response_gap`` in 2.0..5.0 s,
                  or the mono equivalent).
  ECHO SUSPECTED  the caller channel tracks the agent's own audio (scan:
                  ``echo_correlated_activity``); always a WARNING caveat.

The AUTOPSY ID is content-derived: ``apx-`` + the first 12 hex chars of the
sha256 of the INPUT file's bytes, so the same recording gets the same id on
every machine, every run (an mp3's id hashes the mp3 bytes, not the converted
WAV, so it is independent of the local ffmpeg build). Incidents are addressed
as ``<autopsy-id>#<rank>`` -- the id shape a later ``hotato pin <id>`` can
resolve without re-running the analysis. The HTML report is content-addressed
too (``hotato-output/autopsy-<id>.html``), so its path -- and, with no wall
clock anywhere on the page, its bytes -- are stable across runs.

est. cost lines render ONLY when the operator supplies a cost config
(``--cost-config FILE``); with no config there is no dollar figure anywhere.
The config maps incident kinds to the operator's OWN per-incident figures:

    {"currency": "USD",
     "per_incident": {"dead-air": 3.0, "barge-in": 2.0,
                      "talk-over": 2.0, "latency-spike": 1.0}}
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from typing import List, Optional, Tuple

from ._engine.audio import to_dbfs
from ._engine.score import ScoreConfig
from ._engine.vad import energy_vad
from .errors import open_regular as _open_regular
from .errors import require_regular_file as _require_regular_file

__all__ = [
    "OUT_DIR",
    "DEAD_AIR_CRITICAL_SEC",
    "TALK_OVER_CRITICAL_SEC",
    "MONO_SCOPE_NOTE",
    "COST_KIND_KEYS",
    "autopsy_id",
    "load_cost_config",
    "run_autopsy",
    "envelope_dict",
    "build_report_html",
    "render_text",
    "quick_start_text",
]

# Artifacts land here (created on demand, project-local, git-ignorable) --
# the report path stays relative so the printed line is machine-independent.
OUT_DIR = "hotato-output"

# Severity bars. The talk-over ceiling is the SAME acceptable prompt-yield
# bar `investigate label --expect yield` auto-pins (see
# investigate.YIELD_TALK_OVER_CEILING_SEC); dead air at/over 5 s is critical,
# a shorter response gap (the scanner's 2.0 s minimum applies) is a latency
# spike warning.
TALK_OVER_CRITICAL_SEC = 1.0
DEAD_AIR_CRITICAL_SEC = 5.0

# The measured-energy margin (dB below the speech-activity threshold) at which
# a mono silence finding's confidence saturates at 1.00. The confidence is
# derived from a measurement, and the derivation is printed with the number.
_MONO_CONF_FULL_MARGIN_DB = 20.0

# The one-line functional scope for a mono recording, stated once per run:
# what the single-channel evidence supports, and where attribution comes from.
MONO_SCOPE_NOTE = (
    "Mono scope: one mixed channel measures silence timing (dead air, "
    "latency gaps); talk-over and barge-in attribution comes from a "
    "two-channel recording (caller and agent on separate channels)."
)

# incident kind key (the cost-config key + JSON `kind_key`) -> display label
_KIND_LABELS = {
    "barge-in": "BARGE-IN",
    "talk-over": "TALK-OVER",
    "dead-air": "DEAD AIR",
    "latency-spike": "LATENCY SPIKE",
    "echo-suspected": "ECHO SUSPECTED",
}
COST_KIND_KEYS = tuple(_KIND_LABELS)

_FFMPEG_EXTS = (".mp3", ".m4a")

# Samples decoded per read while hashing / walking the mono file.
_CHUNK_BYTES = 1 << 20


# --- the content-derived id --------------------------------------------------

def autopsy_id(path: str) -> str:
    """``apx-`` + the first 12 hex chars of sha256(input file bytes): the same
    recording hashes to the same id on any machine, so an incident ref
    (``<id>#<rank>``) and the report path are stable across runs."""
    h = hashlib.sha256()
    with _open_regular(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
            h.update(chunk)
    return "apx-" + h.hexdigest()[:12]


# --- input resolve: WAV native, mp3/m4a through ffmpeg -----------------------

def _resolve_input(path: str, workdir: str) -> str:
    """Return a local WAV path for ``path``: the file itself for a WAV (or any
    unknown extension, which the WAV reader will refuse with its own honest
    reason), or an ffmpeg conversion into ``workdir`` for mp3/m4a. A missing
    ffmpeg is a clean, one-line actionable refusal, never a traceback."""
    if not os.path.isfile(path):
        raise ValueError(f"{path!r}: no such file.")
    ext = os.path.splitext(path)[1].lower()
    if ext not in _FFMPEG_EXTS:
        return path
    if shutil.which("ffmpeg") is None:
        raise ValueError(
            f"{path!r} is {ext[1:]} audio and converting it needs ffmpeg on "
            "PATH: install ffmpeg (apt install ffmpeg / brew install ffmpeg) "
            "and re-run, or export a PCM WAV yourself and pass that: "
            f"ffmpeg -i {os.path.basename(path)} call.wav"
        )
    converted = os.path.join(workdir, "autopsy-input.wav")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", path, "-acodec", "pcm_s16le", converted],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not os.path.isfile(converted):
        detail = (proc.stderr or "").strip().splitlines()
        raise ValueError(
            f"ffmpeg could not decode {path!r}"
            + (f" ({detail[-1]})" if detail else "")
            + ". Export a PCM WAV and pass that instead."
        )
    return converted


def _probe_channels(path: str) -> int:
    """Channel count of ``path``, with the same honest refusal shape the
    scanner uses for an unreadable input (a text file, a truncated header, a
    non-audio blob all land here with the reason; exit 2 at the CLI)."""
    _require_regular_file(path)
    try:
        # open-ok: _require_regular_file(path) guards the line above
        with wave.open(path, "rb") as wf:
            return wf.getnchannels()
    except (wave.Error, EOFError, struct.error, OSError, RuntimeError) as exc:
        raise ValueError(
            f"{path!r} is not a readable PCM WAV ({exc or type(exc).__name__}). "
            "Export a PCM WAV, e.g. ffmpeg -i input -acodec pcm_s16le output.wav"
        ) from exc


# --- mono: best-effort silence-timing findings -------------------------------

def _mono_frame_rms(
    path: str, frame_ms: float, hop_ms: float,
) -> Tuple[List[float], float, int, float]:
    """One windowed pass over a one-channel WAV: the per-frame RMS track,
    mirroring ``scan.windowed_frame_rms``'s framing math and its corrupt-file
    guards, minus the two-channel requirement (this IS the mono path).
    Returns ``(rms, hop_sec, sample_rate, duration_sec)``."""
    from . import scan as _scan

    _require_regular_file(path)
    try:
        # open-ok: _require_regular_file(path) guards the line above
        wf = wave.open(path, "rb")
    except (wave.Error, EOFError, struct.error, RuntimeError) as exc:
        raise ValueError(
            f"{path!r} is not a readable PCM WAV ({exc or type(exc).__name__}). "
            "Export a PCM WAV, e.g. ffmpeg -i input -acodec pcm_s16le output.wav"
        ) from exc
    with wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        declared_frames = wf.getnframes()
        if sample_rate <= 0:
            raise ValueError(
                f"{path!r} declares an invalid sample rate ({sample_rate} Hz); "
                "the file is corrupt or was mis-exported. Re-export a PCM WAV."
            )
        frame_len = max(1, int(round(sample_rate * frame_ms / 1000.0)))
        hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
        hop_sec = hop / sample_rate

        buf: List[float] = []
        buf_offset = 0
        next_start = 0
        rms: List[float] = []

        while True:
            try:
                raw = wf.readframes(_scan._CHUNK_FRAMES)
            except (wave.Error, EOFError, struct.error, RuntimeError) as exc:
                raise ValueError(
                    f"{path!r} is not a readable PCM WAV "
                    f"({exc or type(exc).__name__}). Export a PCM WAV."
                ) from exc
            if not raw:
                break
            frame_bytes = sampwidth * n_channels
            whole = len(raw) - (len(raw) % frame_bytes)
            if whole == 0:
                break
            samples = _scan._decode(raw[:whole], sampwidth)
            buf.extend(samples[0::n_channels])
            end = buf_offset + len(buf)
            while next_start + frame_len <= end:
                s = next_start - buf_offset
                rms.append(_scan._rms(buf[s:s + frame_len]))
                next_start += hop
            drop = next_start - buf_offset
            if drop > 0:
                buf = buf[drop:]
                buf_offset = next_start

        n_total = buf_offset + len(buf)
        if n_total == 0 and not rms:
            raise ValueError(
                f"{path!r} contains no audio samples (empty or header-only WAV)."
            )
        if declared_frames and n_total < declared_frames:
            raise ValueError(
                f"{path!r} is truncated or corrupt: its header declares "
                f"{declared_frames} frames but only {n_total} are present. "
                "Re-export the full recording."
            )
        while next_start < n_total:
            s = next_start - buf_offset
            rms.append(_scan._rms(buf[s:s + frame_len]))
            next_start += hop
        return rms, hop_sec, sample_rate, n_total / sample_rate


def _mono_findings(
    rms: List[float], hop: float, cfg: ScoreConfig, min_gap_sec: float,
) -> List[dict]:
    """Silence-timing findings from ONE mixed channel: every gap of
    ``min_gap_sec`` or more between sustained activity runs (plus a trailing
    all-quiet span at the end of the recording), each carrying a confidence
    DERIVED FROM A MEASUREMENT: how far the gap's mean energy sits below the
    speech-activity threshold the VAD measured for this recording
    (saturating at a 20 dB margin). No attribution is claimed anywhere:
    a mono gap says everything stopped, not who stopped."""
    from . import scan as _scan

    vad = energy_vad(rms, hop, cfg.caller_vad)
    active = vad.active
    n = len(active)
    min_run = max(1, int(round(cfg.onset_min_run_sec / hop)))
    runs = _scan._runs(active, min_run)
    db = to_dbfs(rms)

    def _confidence(f0: int, f1: int) -> Tuple[float, float]:
        seg = db[f0:f1]
        mean_db = sum(seg) / len(seg) if seg else vad.threshold_db
        margin = vad.threshold_db - mean_db
        conf = max(0.0, min(1.0, margin / _MONO_CONF_FULL_MARGIN_DB))
        return round(conf, 2), round(margin, 1)

    gaps: List[Tuple[int, int, bool]] = []  # (start_frame, end_frame, trailing)
    for (s0, e0), (s1, _e1) in zip(runs, runs[1:]):
        gaps.append((e0, s1, False))
    if runs and n - runs[-1][1] > 0:
        gaps.append((runs[-1][1], n, True))

    findings = []
    for f0, f1, trailing in gaps:
        gap_sec = (f1 - f0) * hop
        if gap_sec < min_gap_sec:
            continue
        conf, margin_db = _confidence(f0, f1)
        t0 = round(f0 * hop, 3)
        if trailing:
            plain = (f"all activity stopped {gap_sec:.2f} s before the end "
                     "of the recording")
        else:
            plain = (f"all activity stopped for {gap_sec:.2f} s before "
                     "speech-band energy resumed")
        kind_key = ("dead-air" if gap_sec >= DEAD_AIR_CRITICAL_SEC
                    else "latency-spike")
        findings.append({
            "t_sec": t0,
            "kind_key": kind_key,
            "severity": ("CRITICAL" if gap_sec >= DEAD_AIR_CRITICAL_SEC
                         else "WARNING"),
            "measurements": {"silence_sec": round(gap_sec, 3),
                             "trailing": trailing},
            "detail": (f"silence={gap_sec:.2f}s  from t={t0:.2f}s to "
                       f"t={round(f1 * hop, 3):.2f}s"),
            "plain_english": plain,
            "confidence": conf,
            "confidence_basis": (
                f"measured: the gap's mean energy sits {margin_db:.1f} dB "
                "below the speech-activity threshold; a 20 dB margin or more "
                "scores 1.00"
            ),
            "_salience": gap_sec,
        })
    findings.sort(key=lambda f: (-f["_salience"], f["t_sec"]))
    for f in findings:
        del f["_salience"]
    return findings


# --- stereo: the existing deterministic scan path, relabeled -----------------

def _incident_from_candidate(c: dict) -> dict:
    """Map one scan candidate to an autopsy incident: severity + display kind
    from the measured numbers, with the scanner's own detail and plain-English
    sentence riding along unchanged."""
    from . import scan as _scan

    kind = c["kind"]
    d = c.get("durations") or {}
    r = c.get("agent_reaction") or {}
    if kind == "overlap_while_agent_talking":
        overlap = float(d.get("overlap_sec") or 0.0)
        critical = (overlap > TALK_OVER_CRITICAL_SEC
                    or not r.get("went_silent_within_search"))
        kind_key = "barge-in"
    elif kind == "agent_start_during_caller":
        overlap = float(d.get("overlap_sec") or 0.0)
        critical = overlap > TALK_OVER_CRITICAL_SEC
        kind_key = "talk-over"
    elif kind == "long_response_gap":
        gap = float(d.get("gap_sec") or 0.0)
        critical = gap >= DEAD_AIR_CRITICAL_SEC
        kind_key = "dead-air" if critical else "latency-spike"
    elif kind == "agent_stop_no_caller":
        critical = float(d.get("trailing_silence_sec") or 0.0) >= DEAD_AIR_CRITICAL_SEC
        kind_key = "dead-air"
    else:  # echo_correlated_activity: a caveat, never critical
        critical = False
        kind_key = "echo-suspected"
    return {
        "t_sec": c["t_sec"],
        "kind_key": kind_key,
        "severity": "CRITICAL" if critical else "WARNING",
        "measurements": d,
        "detail": _scan.candidate_detail(c),
        "plain_english": _scan.candidate_plain_english(c),
        "scan_kind": kind,
    }


# --- cost config: the operator's own figures, never a default ----------------

def load_cost_config(path: str) -> dict:
    """Read and validate a cost config: ``{"currency": "USD", "per_incident":
    {kind-key: number}}``. Every figure is the operator's own; hotato ships no
    default dollar amount, so with no config no cost renders anywhere."""
    try:
        with _open_regular(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"--cost-config {path!r} is not readable JSON ({exc})."
        ) from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("per_incident"), dict):
        raise ValueError(
            f"--cost-config {path!r} must be a JSON object with a "
            "'per_incident' mapping, e.g. "
            '{"currency": "USD", "per_incident": {"dead-air": 3.0}}'
        )
    currency = obj.get("currency", "USD")
    if not isinstance(currency, str) or not currency:
        raise ValueError(f"--cost-config {path!r}: 'currency' must be a string.")
    per = {}
    for k, v in obj["per_incident"].items():
        if k not in COST_KIND_KEYS:
            raise ValueError(
                f"--cost-config {path!r}: unknown incident kind {k!r}; "
                f"the keys are {', '.join(COST_KIND_KEYS)}."
            )
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(
                f"--cost-config {path!r}: per_incident[{k!r}] must be a "
                f"non-negative number, got {v!r}."
            )
        per[k] = float(v)
    return {"currency": currency, "per_incident": per, "source": os.path.basename(path)}


def _money(amount: float, currency: str) -> str:
    return (f"${amount:.2f}" if currency == "USD"
            else f"{amount:.2f} {currency}")


def _apply_costs(incidents: List[dict], cost_config: Optional[dict]) -> Optional[dict]:
    """Attach ``est_cost`` to each incident whose kind the operator priced, and
    return the summary total. With no config: attach nothing, return None --
    no dollar figure exists anywhere on any surface."""
    if not cost_config:
        return None
    currency = cost_config["currency"]
    per = cost_config["per_incident"]
    total = 0.0
    priced = 0
    for inc in incidents:
        amount = per.get(inc["kind_key"])
        if amount is None:
            continue
        inc["est_cost"] = {"amount": round(amount, 2), "currency": currency}
        total += amount
        priced += 1
    return {
        "total": round(total, 2),
        "currency": currency,
        "priced_incidents": priced,
        "source": cost_config["source"],
    }


# --- the run -----------------------------------------------------------------

def run_autopsy(
    path: str,
    *,
    cost_config: Optional[dict] = None,
    min_gap_sec: float = 2.0,
) -> Tuple[dict, str]:
    """Analyze one recording end to end and return ``(result, report_html)``.

    Stereo runs the existing deterministic scanner unchanged; mono runs the
    best-effort silence-timing pass; an unreadable input raises ``ValueError``
    with the reason (exit 2 at the CLI). The caller writes the HTML to
    ``result["report_path"]``."""
    from . import scan as _scan

    if not os.path.isfile(path):
        raise ValueError(f"{path!r}: no such file.")
    apx = autopsy_id(path)
    cfg = ScoreConfig()
    workdir = tempfile.mkdtemp(prefix="hotato-autopsy-")
    try:
        wav_path = _resolve_input(path, workdir)
        channels = _probe_channels(wav_path)

        if channels >= 2:
            mode = "stereo"
            scan_result = _scan.scan_recording(
                wav_path, cfg=cfg, min_gap_sec=min_gap_sec)
            duration = scan_result["duration_sec"]
            incidents = [_incident_from_candidate(c)
                         for c in scan_result["candidates"]]
            rms_c, rms_a, _hop, _sr, _dur = _scan.windowed_frame_rms(
                wav_path, 0, 1, cfg.frame_ms, cfg.hop_ms)
            tracks = [("caller", rms_c), ("agent", rms_a)]
            scope_note = _scan.SCAN_NOTE
        else:
            mode = "mono"
            rms, hop, _sr, duration = _mono_frame_rms(
                wav_path, cfg.frame_ms, cfg.hop_ms)
            duration = round(duration, 3)
            incidents = _mono_findings(rms, hop, cfg, min_gap_sec)
            tracks = [("audio", rms)]
            scope_note = MONO_SCOPE_NOTE
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    for rank, inc in enumerate(incidents, 1):
        inc["rank"] = rank
        inc["id"] = f"{apx}#{rank}"
        inc["kind"] = _KIND_LABELS[inc["kind_key"]]
    cost_summary = _apply_costs(incidents, cost_config)

    n_crit = sum(1 for i in incidents if i["severity"] == "CRITICAL")
    n_warn = len(incidents) - n_crit
    report_path = os.path.join(OUT_DIR, f"autopsy-{apx}.html")

    result = {
        "tool": "hotato",
        "kind": "autopsy",
        "schema_version": "1",
        "id": apx,
        "source": os.path.basename(path),
        # The absolute source path, so `hotato pin apx-...#N` can resolve the
        # recording from the persisted envelope alone and re-check its bytes
        # against the content-derived id. Machine-local by construction, like
        # every path in a result envelope (analyze's folder_path precedent).
        "source_path": os.path.abspath(path),
        "duration_sec": duration,
        "channels": channels,
        "mode": mode,
        "note": scope_note,
        "total_incidents": len(incidents),
        "summary": {"critical": n_crit, "warning": n_warn},
        "incidents": incidents,
        "cost": cost_summary,
        "report_path": report_path,
        "envelope_path": os.path.join(OUT_DIR, f"autopsy-{apx}.json"),
    }
    html_str = build_report_html(result, tracks)
    return result, html_str


def envelope_dict(result: dict) -> dict:
    """The persisted autopsy envelope (``hotato-output/autopsy-<id>.json``):
    the measured result minus the cost-rendering layer. est. cost figures
    exist only on surfaces rendered under ``--cost-config``; the stored
    envelope is the measured facts -- source path, incidents with onset /
    kind / scan_kind -- so its bytes are the same whatever flags rendered
    the run: content-addressed and deterministic, the offline store a later
    ``hotato pin <id>`` resolves without re-running the analysis."""
    env = {k: v for k, v in result.items() if k != "cost"}
    env["incidents"] = [
        {k: v for k, v in inc.items() if k != "est_cost"}
        for inc in result["incidents"]
    ]
    return env


# --- CLI text ----------------------------------------------------------------

def render_text(result: dict) -> str:
    lines = [
        f"hotato autopsy: {result['source']}  "
        f"({result['duration_sec']:.1f}s, {result['channels']} channel"
        f"{'s' if result['channels'] != 1 else ''}, {result['mode']})",
    ]
    if result["mode"] == "mono":
        lines.append(f"  {MONO_SCOPE_NOTE}")
    for inc in result["incidents"]:
        lines.append(
            f"  {'[' + inc['severity'] + ']':<10} {inc['kind']:<14} "
            f"t={inc['t_sec']:.2f}s  {inc['detail']}"
        )
        lines.append(f"      {inc['plain_english']}")
        if result["mode"] == "mono":
            lines.append(
                f"      confidence {inc['confidence']:.2f} "
                f"({inc['confidence_basis']})"
            )
        cost = inc.get("est_cost")
        if cost:
            lines.append(
                f"      est. cost: {_money(cost['amount'], cost['currency'])} "
                f"(your figure for {inc['kind_key']})"
            )
    s = result["summary"]
    total = result["total_incidents"]
    if total == 0:
        lines.append("  0 incidents: no overlap onsets and no silence gaps "
                     "over 2.0s crossed the bar")
    else:
        lines.append(
            f"  {total} incident{'s' if total != 1 else ''}: "
            f"{s['critical']} critical, {s['warning']} warning"
        )
    cost = result.get("cost")
    if cost and cost["priced_incidents"]:
        lines.append(
            f"  est. cost total: {_money(cost['total'], cost['currency'])} "
            f"({cost['priced_incidents']} priced incident"
            f"{'s' if cost['priced_incidents'] != 1 else ''}; "
            f"your figures from {cost['source']})"
        )
    lines.append(f"  report: {result['report_path']}")
    lines.append(
        f"  pin: {result['id']}"
        + (f"  (incidents {result['id']}#1..#{total})" if total else "")
    )
    return "\n".join(lines)


def quick_start_text() -> str:
    """``hotato autopsy`` with no recording: the usage line and the one command
    to try, on the bundled rendered example call."""
    return (
        "usage: hotato autopsy RECORDING [--cost-config FILE] "
        "[--format text|json]\n"
        "\n"
        "Drop one call recording in; get the incidents out: barge-in, "
        "talk-over,\n"
        "dead air, latency spikes, each with a timestamp and the measured "
        "magnitude,\n"
        "plus a self-contained HTML report. WAV reads natively; mp3/m4a "
        "convert\n"
        "through ffmpeg. Offline; no audio leaves the machine.\n"
        "\n"
        "Quick start: hotato autopsy "
        "examples/autopsy/audio/autopsy-01-barge-in-say-do.example.wav"
    )


# --- the self-contained HTML report ------------------------------------------

_WAVE_W = 624       # plot width, matching report._PW
_WAVE_GUT = 92      # left gutter, matching report._GUT
_WAVE_H = 76        # per-track height
_WAVE_BUCKETS = 312

_EXTRA_CSS = """
.ihead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.sev{font-weight:800;font-size:12px;letter-spacing:0.05em;color:#15110d;
 border-radius:7px;padding:3px 10px}
.sev-critical{background:%(red)s}
.sev-warning{background:%(caller)s}
.ikind{font-size:15px;font-weight:650}
.iid{margin-left:auto;color:%(muted)s;font-size:12px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.idetail{color:%(muted)s;font-size:12.5px;margin:4px 0 2px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.iplain{color:%(cream)s;font-size:13.5px;margin:6px 0 2px}
.iconf{color:%(muted)s;font-size:12.5px;margin:4px 0 2px}
.icost{color:%(cream)s;font-size:12.5px;margin:6px 0 2px;font-weight:600}
.wavewrap{overflow-x:auto;margin:8px 0 2px;padding-bottom:4px}
.scopenote{color:%(muted)s;font-size:12.5px;margin:6px 0 12px}
"""


def _bucket_peaks(rms: List[float], buckets: int) -> List[float]:
    """Downsample a per-frame RMS track to ``buckets`` peak values in 0..1
    (normalized to the track's own peak), for the waveform drawing."""
    if not rms:
        return [0.0] * buckets
    peak = max(rms) or 1.0
    out = []
    n = len(rms)
    for b in range(buckets):
        lo = b * n // buckets
        hi = max(lo + 1, (b + 1) * n // buckets)
        out.append(max(rms[lo:hi]) / peak)
    return out


def _waveform_svg(tracks, duration: float, incidents: List[dict],
                  colors: dict) -> str:
    """One SVG: the per-channel RMS envelope (drawn from the same measured
    frame values the analysis walked) with a marker line per incident,
    labeled by rank. Deterministic text: fixed-precision coordinates only."""
    n_tracks = len(tracks)
    height = n_tracks * _WAVE_H + 34
    width = _WAVE_GUT + _WAVE_W + 30
    dur = duration or 1.0
    track_color = {"caller": colors["caller"], "agent": colors["agent"],
                   "audio": colors["caller"]}

    def X(t: float) -> float:
        return _WAVE_GUT + max(0.0, min(t, dur)) / dur * _WAVE_W

    parts = [
        f'<svg role="img" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" '
        'aria-label="Waveform with one marker per incident">',
    ]
    for ti, (name, rms) in enumerate(tracks):
        base = ti * _WAVE_H
        mid = base + _WAVE_H / 2
        parts.append(
            f'<text x="8" y="{mid + 4:.1f}" fill="{colors["muted"]}" '
            f'font-size="12">{name}</text>'
        )
        parts.append(
            f'<line x1="{_WAVE_GUT}" y1="{mid:.1f}" '
            f'x2="{_WAVE_GUT + _WAVE_W}" y2="{mid:.1f}" '
            f'stroke="{colors["grid"]}" stroke-width="1" />'
        )
        peaks = _bucket_peaks(rms, _WAVE_BUCKETS)
        bw = _WAVE_W / _WAVE_BUCKETS
        for b, p in enumerate(peaks):
            h = max(1.0, p * (_WAVE_H / 2 - 6))
            x = _WAVE_GUT + b * bw
            parts.append(
                f'<rect x="{x:.1f}" y="{mid - h:.1f}" width="{bw:.1f}" '
                f'height="{2 * h:.1f}" fill="{track_color[name]}" '
                'fill-opacity="0.75" />'
            )
    # Incident markers, over the full height, labeled by rank.
    for inc in incidents:
        x = X(inc["t_sec"])
        color = (colors["red"] if inc["severity"] == "CRITICAL"
                 else colors["ember"])
        parts.append(
            f'<line x1="{x:.1f}" y1="6" x2="{x:.1f}" '
            f'y2="{n_tracks * _WAVE_H:.1f}" stroke="{color}" '
            'stroke-width="2" stroke-opacity="0.9" />'
        )
        parts.append(
            f'<text x="{x + 3:.1f}" y="16" fill="{color}" '
            f'font-size="11" font-weight="700">#{inc["rank"]}</text>'
        )
    # Time axis: start / end labels.
    axis_y = n_tracks * _WAVE_H + 22
    parts.append(
        f'<text x="{_WAVE_GUT}" y="{axis_y}" fill="{colors["muted"]}" '
        'font-size="11">0.0s</text>'
    )
    parts.append(
        f'<text x="{_WAVE_GUT + _WAVE_W}" y="{axis_y}" text-anchor="end" '
        f'fill="{colors["muted"]}" font-size="11">{dur:.1f}s</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def build_report_html(result: dict, tracks) -> str:
    """ONE self-contained HTML file, in the report/analyze house style: the
    waveform with incident markers, then a card per incident with the same
    measured numbers the CLI printed. Zero external requests: every style is
    inline and no src/href leaves the file. No wall clock anywhere, so the
    same recording renders the same bytes on every run."""
    from . import report as _report

    esc = _report._esc
    css = _report._CSS % _report._C + _EXTRA_CSS % _report._C
    total = result["total_incidents"]
    s = result["summary"]

    body = [
        '<main class="wrap">',
        '<header class="top"><div class="logo"></div><div>',
        '<h1 class="h1">hotato autopsy</h1>',
        f'<div class="tagline">{esc(result["source"])} &middot; '
        f'{result["duration_sec"]:.1f}s &middot; {result["channels"]} channel'
        f'{"s" if result["channels"] != 1 else ""} ({esc(result["mode"])})</div>',
        '<div class="metarow">'
        f'<span class="pill"><b>{total}</b> incident{"" if total == 1 else "s"}</span>'
        f'<span class="pill"><b>{s["critical"]}</b> critical</span>'
        f'<span class="pill"><b>{s["warning"]}</b> warning</span>'
        '<span class="pill">offline <b>yes</b></span>'
        f'<span class="pill">id <b>{esc(result["id"])}</b></span>'
        '</div></div></header>',
    ]
    if result["mode"] == "mono":
        body.append(f'<div class="scopenote">{esc(MONO_SCOPE_NOTE)}</div>')

    body.append('<section class="card"><div class="ctitle">Waveform</div>')
    body.append('<div class="wavewrap">'
                + _waveform_svg(tracks, result["duration_sec"],
                                result["incidents"], _report._C)
                + '</div>')
    body.append('<div class="scopenote">The envelope is the same measured '
                'per-frame energy the analysis walked; each marker is one '
                'incident below.</div></section>')

    if total == 0:
        body.append(
            '<section class="card"><div class="subtle">0 incidents: no '
            'overlap onsets and no silence gaps over 2.0s crossed the bar.'
            '</div></section>'
        )
    for inc in result["incidents"]:
        sev_class = "sev-critical" if inc["severity"] == "CRITICAL" else "sev-warning"
        parts = [
            '<section class="card">',
            '<div class="ihead">'
            f'<span class="sev {sev_class}">{esc(inc["severity"])}</span>'
            f'<span class="ikind">#{inc["rank"]} {esc(inc["kind"])} &middot; '
            f't={inc["t_sec"]:.2f}s</span>'
            f'<span class="iid">{esc(inc["id"])}</span>'
            '</div>',
            f'<div class="idetail">{esc(inc["detail"])}</div>',
            f'<div class="iplain">{esc(inc["plain_english"])}</div>',
        ]
        if "confidence" in inc:
            parts.append(
                f'<div class="iconf">confidence {inc["confidence"]:.2f} '
                f'({esc(inc["confidence_basis"])})</div>'
            )
        cost = inc.get("est_cost")
        if cost:
            parts.append(
                '<div class="icost">est. cost: '
                f'{esc(_money(cost["amount"], cost["currency"]))} '
                f'(your figure for {esc(inc["kind_key"])})</div>'
            )
        parts.append('</section>')
        body.append("".join(parts))

    cost = result.get("cost")
    if cost and cost["priced_incidents"]:
        body.append(
            '<section class="card"><div class="ctitle">est. cost total: '
            f'{esc(_money(cost["total"], cost["currency"]))}</div>'
            f'<div class="scopenote">{cost["priced_incidents"]} priced '
            f'incident{"" if cost["priced_incidents"] == 1 else "s"}, your '
            f'figures from {esc(cost["source"])}; with no cost config no '
            'figure renders.</div></section>'
        )

    body.append(
        '<footer class="foot"><div class="scopenote"><b>Method.</b> '
        'Deterministic energy measurement over time: per-frame RMS, a '
        'transparent activity threshold, and the timing walk between the two '
        'tracks. Timing and floor-holding, not intent or transcription; a '
        'two-channel recording attributes talk-over, a mono recording '
        'measures silence timing with a stated confidence. Offline; nothing '
        'leaves this file.</div></footer>'
    )
    body.append('</main>')

    title = (f"hotato autopsy: {result['source']}, {total} incident"
             f"{'' if total == 1 else 's'}")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title>"
        f"<style>{css}</style></head><body>"
        + "".join(body)
        + "</body></html>\n"
    )
