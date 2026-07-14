"""``hotato analyze <folder>``: zero-config, drop-a-folder candidate discovery.

Point it at a folder of real dual-channel call recordings and it walks EVERY
recording label-free with the existing whole-call scanner (``hotato scan``),
aggregates the candidate turn-taking moments across all of them, and ranks
them by the scanner's own salience (overlap seconds / gap seconds / echo
coherence) so the worst moments float to the top. No scenarios, no labels, no
onset, no flags required.

Three outputs, all offline and self-contained:

  1. a ranked HTML DASHBOARD reusing the ``report.py`` house style and its
     timeline SVG renderer: one card per top moment with the call file, the
     timestamp, the candidate kind, the measured number, a to-scale
     caller/agent timeline of that exact moment, and three actions: two
     copy buttons carrying the exact ``hotato fixture promote`` command for
     a yield or hold label (you pick the label; the page never does), and
     Ignore, which hides the card on the page only, client side, no state;
  2. the HEAR-THE-BUG player: for the top-ranked moments the REAL audio around
     the moment is embedded inline (base64 WAV data URI, nothing uploaded) with
     a PLAYHEAD that sweeps the moment's timeline in sync with ``audio``
     playback, so you press play and HEAR the overlap or gap land exactly where
     the chart marks it. Reduced-motion safe: the playhead still tracks
     playback without the smooth animation;
  3. ``--format json``: the ranked candidates plus their metadata, so an agent
     can drive the same discovery.

HONESTY, stated on the page and repeated here: these are MEASURED CANDIDATE
timing moments, never inferred verdicts and never intent. Energy is not intent;
the scanner cannot know whether a caller sound was "mhm" or "stop". You decide
the expected behavior and label the moments that matter with
``hotato fixture create`` / ``promote``. There is no pass/fail here, no failure
count, and no accuracy number anywhere.

Non-dual-channel or otherwise unscorable files are reported cleanly as skipped
with their reason; a bad file never crashes the run.
"""

from __future__ import annotations

import base64
import io
import os
import re
import wave
from typing import List, Optional, Tuple

from ._engine.score import ScoreConfig
from .errors import ChannelRangeError
from .errors import wav_read as _wav_read
from .scan import SCAN_NOTE, activity_tracks, scan_recording

__all__ = [
    "analyze_folder",
    "build_dashboard_html",
    "suggest_fixture_id",
    "validate_scan_args",
    "DEFAULT_TOP",
    "DEFAULT_AUDIO_TOP",
    "DEFAULT_PRE_SEC",
    "DEFAULT_POST_SEC",
    "DEFAULT_REPORT_JSON",
]


def validate_scan_args(
    *, caller_channel: int, agent_channel: int, min_gap_sec: float
) -> None:
    """Validate the GLOBAL scan flags ONCE, up front, for the folder-batch
    commands (analyze / loop / sweep). A bad ``--min-gap`` or a bad
    ``--caller-channel`` / ``--agent-channel`` is a single usage mistake, not a
    property of any one recording, so it must raise here (-> exit-2 usage error)
    rather than be swallowed per file as a 'skip' -- which would turn a typo'd
    flag into a false clean 'found nothing' result and, for ``loop``, conclude
    there is nothing to fix. (Out-of-range-high channels are still caught per
    file as ``ChannelRangeError`` and re-raised, since the channel count is only
    known once a file is opened.)"""
    if min_gap_sec <= 0:
        raise ValueError(f"--min-gap must be > 0 seconds; got {min_gap_sec}.")
    if caller_channel < 0 or agent_channel < 0:
        raise ValueError(
            "--caller-channel and --agent-channel must be >= 0 "
            f"(got caller={caller_channel}, agent={agent_channel})."
        )
    if caller_channel == agent_channel:
        raise ValueError(
            f"--caller-channel and --agent-channel must be different (both are "
            f"{caller_channel}); pass distinct channels for a 2-channel "
            "recording (the caller and the agent are on separate channels)."
        )

DEFAULT_TOP = 25            # moments shown in the dashboard / capped in stdout json
DEFAULT_AUDIO_TOP = 8       # top moments that get the embedded hear-the-bug player
DEFAULT_PRE_SEC = 2.0       # audio/timeline window kept BEFORE the moment
DEFAULT_POST_SEC = 4.0      # audio/timeline window kept AFTER the moment
# Total embedded-audio budget for one page: the hear-the-bug clips are the point,
# but the page must still open instantly, so clips past this ceiling are noted in
# plain text and skipped rather than silently ballooning the file. Tests
# monkeypatch this to exercise the budget path cheaply.
_EMBED_BUDGET_BYTES = 12 * 1024 * 1024


# --- salience + headline (recomputed from the scanner's own numbers) --------

_ECHO_KIND = "echo_correlated_activity"


def _is_echo_kind(c: dict) -> bool:
    """An echo_correlated_activity candidate is a CAVEAT ('this may be the agent
    hearing its own leaked TTS'), never a talk-over. It must rank strictly beneath
    every real talk-over/gap candidate regardless of its coherence value."""
    return c["kind"] == _ECHO_KIND


def _salience(c: dict) -> float:
    """The scanner's own salience for one candidate, recomputed from its
    measured numbers so moments can be ranked across calls and kinds (bigger =
    worse). Overlap and gap seconds dominate.

    Echo coherence lives on a DIFFERENT scale (0..1) from overlap/gap SECONDS, so
    it is never mixed into this number for ordering: echo candidates are demoted
    beneath every non-echo candidate by the two-level sort key (see
    ``_sort_key``), which is what actually enforces the 'a caveat sits below them
    by construction' promise. This value is still reported per candidate as the
    honest measured salience; for echo that is its coherence."""
    d = c.get("durations", {})
    k = c["kind"]
    if k in ("overlap_while_agent_talking", "agent_start_during_caller"):
        return float(d.get("overlap_sec", 0.0) or 0.0)
    if k == "long_response_gap":
        return float(d.get("gap_sec", 0.0) or 0.0)
    if k == "agent_stop_no_caller":
        return float(d.get("trailing_silence_sec", 0.0) or 0.0)
    if k == _ECHO_KIND:
        return float((c.get("agent_reaction") or {}).get("coherence", 0.0) or 0.0)
    return 0.0


def _sort_key(c: dict):
    """Ranking key: every non-echo talk-over/gap candidate first (by descending
    salience seconds), then all echo caveats. ``_is_echo_kind`` is the primary
    key so a short (sub-second) genuine overlap can never be buried under a
    high-coherence echo caveat -- the exact invariant analyze promises."""
    return (_is_echo_kind(c), -c["salience"], c["source"], c["t_sec"], c["kind"])


def _headline(c: dict) -> str:
    """The one measured number for this candidate, for the card's chip."""
    d = c.get("durations", {})
    k = c["kind"]
    if k in ("overlap_while_agent_talking", "agent_start_during_caller"):
        return f"{d.get('overlap_sec', 0.0):.2f}s overlap"
    if k == "long_response_gap":
        return f"{d.get('gap_sec', 0.0):.2f}s gap"
    if k == "agent_stop_no_caller":
        return f"{d.get('trailing_silence_sec', 0.0):.2f}s trailing silence"
    if k == "echo_correlated_activity":
        coh = (c.get("agent_reaction") or {}).get("coherence", 0.0)
        return f"coherence {coh:.2f}"
    return ""


# --- promote copy commands (the actions on each candidate card) -------------

# The result-file name the copied promote command reads. Deliberately the
# COMMAND'S default json name, never derived from the --out path: the
# dashboard bytes stay identical whatever the page was saved as, and the
# page tells the reader how to write this exact file.
DEFAULT_REPORT_JSON = "hotato-analyze.json"

_KEBAB_RE = re.compile(r"[^a-z0-9]+")


def suggest_fixture_id(source: str, kind: str, rank: int) -> str:
    """A ready-to-run fixture id for one ranked candidate: call id, kind, and
    rank, kebab-cased to the same slug rule ``fixture create --id`` enforces.
    The call id is the recording's stem with extensions dropped; a pulled
    recording named ``STACK__ID.wav`` contributes its bare call id, the same
    name a ``FILE#CALL:N`` ref answers to."""
    stem = os.path.splitext(os.path.basename(source))[0]
    if "__" in stem:
        stem = stem.split("__", 1)[1]
    else:
        stem = stem.split(".", 1)[0]
    slug = _KEBAB_RE.sub("-", f"{stem}-{kind}-{rank}".lower()).strip("-")
    return slug or f"candidate-{rank}"


def _promote_command(report_json: str, rank: int, cand: dict,
                     expect: str) -> str:
    """The exact ``hotato fixture promote`` command one copy button carries.
    The ref is ``REPORT_JSON#RANK``: the same 1-based rank the card's #N chip
    shows, which is the order ``parse_candidate_ref`` resolves."""
    sid = suggest_fixture_id(cand["source"], cand["kind"], rank)
    return (f"hotato fixture promote {report_json}#{rank} "
            f"--expect {expect} --id {sid} --out tests/hotato")


# --- discover WAVs deterministically ---------------------------------------

def _iter_wavs(folder: str) -> List[Tuple[str, str]]:
    """Every ``.wav`` under ``folder`` as ``(relpath, abspath)``, sorted by
    relpath so the scan order (and therefore every byte of output) is
    deterministic across runs and machines."""
    found = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in files:
            if name.lower().endswith(".wav"):
                ap = os.path.join(root, name)
                rel = os.path.relpath(ap, folder)
                found.append((rel, ap))
    found.sort(key=lambda pair: pair[0])
    return found


# --- the aggregate (the JSON surface + the render feed) ---------------------

def analyze_folder(
    folder: str,
    *,
    caller_channel: int = 0,
    agent_channel: int = 1,
    cfg: Optional[ScoreConfig] = None,
    min_gap_sec: float = 2.0,
    pre_sec: float = DEFAULT_PRE_SEC,
    post_sec: float = DEFAULT_POST_SEC,
) -> Tuple[dict, dict]:
    """Scan every dual-channel WAV under ``folder`` and aggregate + rank the
    candidate moments across all of them.

    Returns ``(aggregate, per_file)``:
      * ``aggregate`` is the JSON-serializable result: the ranked candidates
        (each carrying its source file, salience, and audio/timeline window),
        the per-file scan summary, and the clean skipped list;
      * ``per_file`` maps a source id to the frame-level activity tracks and the
        on-disk path the dashboard needs to draw the timeline and embed the
        audio. It is never serialized.

    Ranking is by the scanner's own salience, then source then timestamp for a
    stable, byte-identical order.
    """
    if cfg is None:
        cfg = ScoreConfig()
    if not os.path.isdir(folder):
        raise ValueError(
            f"{folder!r} is not a folder. Pass a directory of dual-channel call "
            "recordings, e.g. hotato analyze ./recordings"
        )
    if pre_sec < 0 or post_sec <= 0:
        raise ValueError(
            f"--pre must be >= 0 and --post > 0 (got pre={pre_sec}, post={post_sec})."
        )
    # Validate the GLOBAL scan flags ONCE, up front, exactly like pre/post above:
    # a bad --min-gap or bad channel flag is one usage mistake, not a per-file
    # problem, so it must not degrade into a per-file skip below.
    validate_scan_args(
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        min_gap_sec=min_gap_sec,
    )

    scanned: List[dict] = []
    skipped: List[dict] = []
    ranked: List[dict] = []
    per_file: dict = {}

    for rel, ap in _iter_wavs(folder):
        try:
            scan = scan_recording(
                ap,
                caller_channel=caller_channel,
                agent_channel=agent_channel,
                cfg=cfg,
                min_gap_sec=min_gap_sec,
            )
            caller, agent, hop, sample_rate, duration = activity_tracks(
                ap, caller_channel=caller_channel, agent_channel=agent_channel,
                cfg=cfg,
            )
        except ChannelRangeError:
            # An out-of-range channel index is a GLOBAL flag mistake (the same
            # --caller-channel/--agent-channel for every file), not a per-file
            # problem, so it must propagate as a usage error (exit 2) instead of
            # skipping every file and reporting a false clean 'found nothing'.
            raise
        except (ValueError, FileNotFoundError, OSError, wave.Error,
                RuntimeError) as exc:
            # A single bad file is reported cleanly, never fatal: a mono mix, a
            # corrupt header, an unsupported width, an empty file, or a valid
            # RIFF/WAVE header with a malformed inner sub-chunk (stdlib ``wave``
            # raises a bare RuntimeError for that last one) all land here with
            # their honest reason, so one bad file never aborts the whole batch.
            skipped.append({"file": rel, "reason": str(exc)})
            continue

        per_file[rel] = {
            "path": ap,
            "caller": caller,
            "agent": agent,
            "hop": hop,
            "sample_rate": sample_rate,
            "duration": duration,
        }
        scanned.append({
            "source": rel,
            "duration_sec": scan["duration_sec"],
            "total_candidates": scan["total_candidates"],
        })
        for c in scan["candidates"]:
            t = c["t_sec"]
            w0 = round(max(0.0, t - pre_sec), 3)
            w1 = round(min(duration, t + post_sec), 3)
            ranked.append({
                "source": rel,
                "t_sec": t,
                "kind": c["kind"],
                "salience": round(_salience(c), 3),
                "durations": c["durations"],
                "agent_reaction": c["agent_reaction"],
                "window": {"start_sec": w0, "end_sec": w1},
            })

    ranked.sort(key=_sort_key)

    aggregate = {
        "tool": "hotato",
        "kind": "analyze",
        "schema_version": "1",
        "folder": os.path.basename(os.path.normpath(folder)) or folder,
        # The absolute folder path, so `hotato fixture promote FILE#N` can
        # resolve a candidate's source recording from the result file alone
        # (the JSON often lands far from the analyzed folder, e.g. a sweep's
        # stdout redirect). Machine-local by construction, like every path in
        # a result envelope.
        "folder_path": os.path.abspath(folder),
        "note": SCAN_NOTE,
        "config": {
            "min_gap_sec": min_gap_sec,
            "pre_sec": pre_sec,
            "post_sec": post_sec,
            "search_window_sec": cfg.max_search_sec,
        },
        "calls_scanned": len(scanned),
        "calls_skipped": len(skipped),
        "scanned": scanned,
        "skipped": skipped,
        "total_candidates": len(ranked),
        "candidates": ranked,
    }
    return aggregate, per_file


# --- per-moment timeline model + audio clip --------------------------------

def _window_model(pf: dict, cand: dict) -> dict:
    """Build a ``report._svg_timeline`` model for one moment's window from the
    real frame tracks: caller/agent activity, both-active (talk-over) spans, an
    onset marker at the moment, and a yield marker where the scanner measured
    the agent going silent. All times are window-relative so the drawing scale
    matches the embedded clip (playhead time maps straight onto it)."""
    from . import report as _report

    caller, agent, hop = pf["caller"], pf["agent"], pf["hop"]
    w = cand["window"]
    w0, w1 = w["start_sec"], w["end_sec"]
    n = min(len(caller), len(agent))
    fi0 = max(0, int(w0 / hop))
    fi1 = min(n, int(round(w1 / hop)))
    frames = []
    for fi in range(fi0, fi1):
        ca, ag = bool(caller[fi]), bool(agent[fi])
        frames.append({
            "t_sec": round(fi * hop - w0, 6),
            "caller_active": ca,
            "agent_active": ag,
            "both": ca and ag,
        })

    dur = round(w1 - w0, 6)
    onset = round(cand["t_sec"] - w0, 6)
    # A yield marker only where the scanner actually measured the agent going
    # silent within the window (overlap kind); never invented elsewhere.
    yield_abs = None
    r = cand.get("agent_reaction") or {}
    if (cand["kind"] == "overlap_while_agent_talking"
            and r.get("after_sec") is not None):
        cand_ya = onset + r["after_sec"]
        if cand_ya <= dur + 1e-9:
            yield_abs = round(cand_ya, 6)

    return {
        "duration": dur if dur > 0 else 1.0,
        "caller_spans": _report._spans(frames, "caller_active", hop),
        "agent_spans": _report._spans(frames, "agent_active", hop),
        "talkover_spans": _report._spans(frames, "both", hop),
        "onset": onset,
        "yield_abs": yield_abs,
    }


def _clip_wav_bytes(path: str, w0: float, w1: float) -> Optional[bytes]:
    """Losslessly copy the PCM frames of ``[w0, w1)`` out of ``path`` into a new
    self-contained WAV (same channels, width, rate) and return its bytes. No
    resampling, no re-quantization: the same input yields the same bytes every
    run. Returns None if the range is empty or the file cannot be framed."""
    try:
        with _wav_read(path) as wf:
            sr = wf.getframerate()
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            nframes = wf.getnframes()
            s0 = max(0, int(w0 * sr))
            s1 = min(nframes, int(round(w1 * sr)))
            if s1 <= s0:
                return None
            wf.setpos(s0)
            raw = wf.readframes(s1 - s0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(nch)
            out.setsampwidth(sw)
            out.setframerate(sr)
            out.writeframes(raw)
        return buf.getvalue()
    except (wave.Error, EOFError, OSError, ValueError, RuntimeError):
        return None


# --- the dashboard -----------------------------------------------------------

# The requestAnimationFrame playhead: on play the line sweeps the timeline in
# lockstep with audio.currentTime; on pause/seek/end it snaps to the current
# position. Reduced-motion callers skip the rAF loop and ride 'timeupdate'
# instead, so the playhead still tracks playback without a smooth animation.
_PLAYER_JS = """
(function(){
  var reduce=false;
  try{reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;}catch(e){}
  var cards=document.querySelectorAll('.moment[data-dur]');
  Array.prototype.forEach.call(cards,function(m){
    var audio=m.querySelector('audio'), ph=m.querySelector('.ph');
    if(!audio||!ph) return;
    var gut=parseFloat(m.getAttribute('data-gut'));
    var pw=parseFloat(m.getAttribute('data-pw'));
    var dur=parseFloat(m.getAttribute('data-dur'));
    if(!(dur>0)) return;
    var raf=null;
    function put(){
      var t=audio.currentTime||0, f=t/dur;
      if(f<0)f=0; if(f>1)f=1;
      var x=(gut+f*pw).toFixed(1);
      ph.setAttribute('x1',x); ph.setAttribute('x2',x);
    }
    function loop(){put(); raf=window.requestAnimationFrame(loop);}
    function start(){ if(reduce){put(); return;} if(raf)window.cancelAnimationFrame(raf); loop(); }
    function stop(){ if(raf){window.cancelAnimationFrame(raf); raf=null;} put(); }
    audio.addEventListener('play',start);
    audio.addEventListener('pause',stop);
    audio.addEventListener('ended',stop);
    audio.addEventListener('seeked',put);
    audio.addEventListener('timeupdate',put);
  });
})();
"""

# The card actions: the two promote buttons copy their exact command from the
# data-cmd attribute (navigator.clipboard first, a hidden-textarea execCommand
# fallback for file:// pages where the async API is unavailable); Ignore hides
# the card on this page only, client side, no state, and pauses its player if
# one is embedded. No animation anywhere, so reduced-motion needs no gating:
# feedback is an instant text swap that reverts after a moment.
_ACTIONS_JS = """
(function(){
  function flash(btn,msg){
    var old=btn.getAttribute('data-label')||btn.textContent;
    btn.textContent=msg;
    btn.setAttribute('disabled','');
    window.setTimeout(function(){
      btn.textContent=old;
      btn.removeAttribute('disabled');
    },1400);
  }
  function fallbackCopy(text){
    var ta=document.createElement('textarea');
    ta.value=text;
    ta.setAttribute('readonly','');
    ta.style.position='fixed';
    ta.style.left='-9999px';
    document.body.appendChild(ta);
    ta.select();
    var ok=false;
    try{ok=document.execCommand('copy');}catch(e){ok=false;}
    document.body.removeChild(ta);
    return ok;
  }
  Array.prototype.forEach.call(
      document.querySelectorAll('button.copycmd'),function(btn){
    btn.setAttribute('data-label',btn.textContent);
    btn.addEventListener('click',function(){
      var cmd=btn.getAttribute('data-cmd')||'';
      function done(ok){flash(btn,ok?'copied':'copy blocked');}
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(cmd).then(function(){done(true);},
          function(){done(fallbackCopy(cmd));});
      }else{
        done(fallbackCopy(cmd));
      }
    });
  });
  Array.prototype.forEach.call(
      document.querySelectorAll('button.dismiss'),function(btn){
    btn.addEventListener('click',function(){
      var card=btn.closest('.moment');
      if(!card)return;
      var audio=card.querySelector('audio');
      if(audio){try{audio.pause();}catch(e){}}
      card.setAttribute('hidden','');
    });
  });
})();
"""

_EXTRA_CSS = """
.moment .mhead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.rank{font-weight:800;font-size:12.5px;color:#15110d;background:%(caller)s;
 border-radius:7px;padding:2px 9px;letter-spacing:0.03em}
.mkind{font-size:15px;font-weight:650}
.msrc{color:%(muted)s;font-size:12.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.mnum{margin-left:auto;color:#15110d;font-weight:800;font-size:12.5px;
 background:%(ember)s;border-radius:8px;padding:4px 11px;letter-spacing:0.02em}
.mdetail{color:%(muted)s;font-size:12.5px;margin:6px 0 2px}
.ph{pointer-events:none}
.hearcap{color:%(muted)s;font-size:12.5px;margin:2px 0 10px}
.skiprow{display:flex;gap:10px;margin:6px 0 2px}
.skipf{min-width:220px;color:%(cream)s;font-size:12.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.skipr{color:%(muted)s;font-size:12.5px}
.callrow{display:flex;gap:10px;align-items:baseline;margin:5px 0}
.callf{min-width:260px;color:%(cream)s;font-size:12.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.calln{color:%(muted)s;font-size:12.5px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 2px}
.actions button{background:%(card2)s;border:1px solid %(line)s;
 color:%(cream)s;border-radius:8px;padding:5px 12px;font-size:12.5px;
 font-weight:600;font-family:inherit;cursor:pointer}
.actions button:hover{border-color:%(muted)s}
.actions button[disabled]{cursor:default;color:%(muted)s}
.actions .dismiss{color:%(muted)s;font-weight:500}
.moment[hidden]{display:none}
"""


def _moment_card(pf: dict, cand: dict, rank: int, *, embed: bool,
                 clip_b64: Optional[str], clip_name: str,
                 report_json: str) -> str:
    from . import report as _report

    esc = _report._esc
    model = _window_model(pf, cand)
    svg = _report._svg_timeline(model)
    playhead = (
        f'<line class="ph" x1="{_report._GUT}" y1="{_report._MARK_TOP}" '
        f'x2="{_report._GUT}" y2="{_report._MARK_BOT}" '
        f'stroke="{_report._C["cream"]}" stroke-width="2" stroke-opacity="0.9" />'
    )
    cut = svg.rfind("</svg>")
    svg = svg[:cut] + playhead + svg[cut:]

    detail = _detail_text(cand)
    dur = model["duration"]
    parts = [
        f'<section class="card moment" data-dur="{dur}" '
        f'data-gut="{_report._GUT}" data-pw="{_report._PW}">',
        '<div class="mhead">',
        f'<span class="rank">#{rank}</span>',
        f'<span class="mkind">{esc(cand["kind"])}</span>',
        f'<span class="msrc">{esc(cand["source"])} &middot; t={cand["t_sec"]:.2f}s</span>',
        f'<span class="mnum">{esc(_headline(cand))}</span>',
        '</div>',
        f'<div class="mdetail">{esc(detail)}</div>',
        f'<div class="tl">{svg}</div>',
    ]
    if embed and clip_b64 is not None:
        w = cand["window"]
        parts.append(
            '<div class="audio"><div class="audcap">The real audio around this '
            'moment, embedded in this file. Nothing is uploaded. Press play: the '
            'playhead sweeps the timeline above in sync.</div>'
            '<div class="audrow">'
            f'<span class="audk">t {w["start_sec"]:.2f}s to {w["end_sec"]:.2f}s</span>'
            f'<audio controls preload="none" '
            f'aria-label="Play the audio around {esc(cand["kind"])} at '
            f'{cand["t_sec"]:.2f} seconds in {esc(cand["source"])}" '
            f'src="data:audio/wav;base64,{clip_b64}"></audio>'
            f'<span class="audnote">{esc(clip_name)}</span></div></div>'
        )
    elif embed:
        parts.append(
            '<div class="audio"><span class="audnote">audio not embedded for '
            'this moment (over the page budget or the clip window was empty)'
            '</span></div>'
        )
    yield_cmd = _promote_command(report_json, rank, cand, "yield")
    hold_cmd = _promote_command(report_json, rank, cand, "hold")
    parts.append(
        '<div class="actions">'
        f'<button type="button" class="copycmd" data-cmd="{esc(yield_cmd)}" '
        f'title="copies: {esc(yield_cmd)}">Promote as yield fixture</button>'
        f'<button type="button" class="copycmd" data-cmd="{esc(hold_cmd)}" '
        f'title="copies: {esc(hold_cmd)}">Promote as hold fixture</button>'
        '<button type="button" class="dismiss" title="hides this card on '
        'this page only; nothing is saved">Ignore</button>'
        '</div>'
    )
    parts.append('</section>')
    return "".join(parts)


def _detail_text(cand: dict) -> str:
    """A plain restatement of the measured timing for the card, no intent and no
    verdict: what physically happened at this moment."""
    d = cand.get("durations", {})
    r = cand.get("agent_reaction") or {}
    k = cand["kind"]
    if k == "overlap_while_agent_talking":
        if r.get("went_silent_within_search"):
            tail = f"the agent went silent {r['after_sec']:.2f}s later"
        else:
            tail = (f"the agent did not go silent within "
                    f"{r.get('search_window_sec', 0.0):.1f}s")
        return (f"the caller channel became active while the agent channel was "
                f"active for {d.get('overlap_sec', 0.0):.2f}s; {tail}")
    if k == "agent_start_during_caller":
        base = (f"the agent channel started a fresh run while the caller channel "
                f"was active, overlapping {d.get('overlap_sec', 0.0):.2f}s")
        kept = d.get("caller_kept_talking_sec")
        if kept is not None:
            base += f"; the caller channel kept activity {kept:.2f}s longer"
        return base
    if k == "long_response_gap":
        nxt = r.get("next_agent_onset_sec")
        tail = (f"the next agent run began at {nxt:.2f}s" if nxt is not None
                else "no agent run began before the recording ended")
        return (f"the caller channel finished a run and {d.get('gap_sec', 0.0):.2f}s "
                f"of quiet followed; {tail}")
    if k == "agent_stop_no_caller":
        return (f"the agent channel went quiet for {d.get('trailing_silence_sec', 0.0):.2f}s "
                f"with no caller-channel activity within "
                f"{d.get('caller_proximity_sec', 0.0):.2f}s on either side")
    if k == "echo_correlated_activity":
        return (f"the caller channel over {d.get('activity_sec', 0.0):.2f}s tracks the "
                f"agent channel at lag {d.get('lag_sec', 0.0):.2f}s "
                f"(coherence {r.get('coherence', 0.0):.2f}): this activity looks "
                f"like leaked agent audio, not an independent caller")
    return ""


def build_dashboard_html(
    aggregate: dict,
    per_file: dict,
    *,
    top: int = DEFAULT_TOP,
    audio_top: int = DEFAULT_AUDIO_TOP,
    embed_budget_bytes: int = _EMBED_BUDGET_BYTES,
    report_json: Optional[str] = None,
) -> str:
    """Render the ranked candidate moments as ONE self-contained, offline HTML
    dashboard, reusing the ``report.py`` house style + timeline SVG renderer.
    The top ``audio_top`` moments carry the hear-the-bug player (embedded audio
    + a playhead synced to it); the rest show the timeline only. Every clip is
    embedded as a base64 WAV data URI, so the page has zero external requests.

    Every card also carries the promote actions: two copy buttons with the
    exact ``hotato fixture promote REPORT_JSON#RANK`` command for a yield or a
    hold label, and Ignore, which hides the card client side only.
    ``report_json`` is the result-file name those commands read -- the
    producing command's DEFAULT json name (sweep passes its own), never the
    --out path, so the page's bytes stay identical whatever it was saved as.
    """
    from . import report as _report

    esc = _report._esc
    report_json = report_json or DEFAULT_REPORT_JSON
    ranked = aggregate["candidates"]
    shown = ranked if top <= 0 else ranked[:top]
    total = aggregate["total_candidates"]
    n_calls = aggregate["calls_scanned"]
    n_skip = aggregate["calls_skipped"]

    css = _report._CSS + (_EXTRA_CSS % _report._C)

    body = [
        '<main class="wrap">',
        '<header class="top"><div class="logo"></div><div>',
        '<h1 class="h1">hotato analyze</h1>',
        '<div class="tagline">Ranked candidate turn-taking moments across '
        f'{esc(aggregate["folder"])}.</div>',
        f'<div class="subtle">{esc(aggregate["note"])}</div>',
        '<div class="metarow">'
        f'<span class="pill"><b>{n_calls}</b> calls scanned</span>'
        f'<span class="pill"><b>{total}</b> candidate moment'
        f'{"" if total == 1 else "s"}</span>'
        + (f'<span class="pill"><b>{n_skip}</b> skipped</span>' if n_skip else '')
        + '<span class="pill">offline <b>yes</b></span>'
        '</div></div></header>',
    ]

    # Summary strip.
    body.append(
        '<div class="summary">'
        f'<div><div class="bignum">{total}</div>'
        '<div class="subtle" style="color:' + _report._C["muted"] + '">candidate '
        f'moments ranked by salience across {n_calls} call'
        f'{"" if n_calls == 1 else "s"}</div></div>'
        '</div>'
    )

    if total == 0:
        body.append(
            '<section class="card"><div class="subtle">No candidate moments in '
            'this folder: no overlap onsets and no response gaps over the minimum '
            'crossed the threshold. Nothing to label yet.</div></section>'
        )
    else:
        if len(shown) < total:
            body.append(
                f'<div class="hearcap">Showing the top {len(shown)} of {total} '
                'by salience (longest overlap or gap first).</div>'
            )
        body.append(
            '<div class="hearcap">Press play on a top moment to HEAR it: the '
            'playhead sweeps the timeline in sync with the audio, landing on the '
            'measured overlap or gap. These are timing candidates you review and '
            'label, never a decided outcome.</div>'
        )
        body.append(
            '<div class="hearcap">The promote buttons copy a '
            '<span class="mono">hotato fixture promote</span> command that '
            f'reads <span class="mono">{esc(report_json)}</span>; write that '
            'file with the same run and <span class="mono">--format json'
            '</span>. You pick the label. Ignore hides the card on this page '
            'only; nothing is saved.</div>'
        )
        spent = 0
        for i, cand in enumerate(shown, 1):
            pf = per_file.get(cand["source"])
            if pf is None:  # defensive; every shown candidate has a scanned file
                continue
            want_audio = i <= audio_top
            clip_b64 = None
            clip_name = ""
            if want_audio:
                w = cand["window"]
                clip = _clip_wav_bytes(pf["path"], w["start_sec"], w["end_sec"])
                if clip is not None and spent + len(clip) <= embed_budget_bytes:
                    spent += len(clip)
                    clip_b64 = base64.b64encode(clip).decode("ascii")
                    clip_name = (f"{os.path.basename(cand['source'])} "
                                 f"[{w['start_sec']:.2f}s..{w['end_sec']:.2f}s]")
            body.append(_moment_card(
                pf, cand, i, embed=want_audio,
                clip_b64=clip_b64, clip_name=clip_name,
                report_json=report_json,
            ))

    # Skipped inputs (clean, never a crash and never a failure count).
    if n_skip:
        rows = "".join(
            f'<div class="skiprow"><span class="skipf">{esc(s["file"])}</span>'
            f'<span class="skipr">{esc(s["reason"])}</span></div>'
            for s in aggregate["skipped"]
        )
        body.append(
            '<section class="card"><div class="ctitle">Skipped files</div>'
            '<div class="tnote">Not dual-channel or not readable as PCM WAV, so '
            'talk-over could not be attributed. Reported here with the reason; '
            'never scored, never counted.</div>' + rows + '</section>'
        )

    # Per-call scan summary.
    if aggregate["scanned"]:
        rows = "".join(
            f'<div class="callrow"><span class="callf">{esc(s["source"])}</span>'
            f'<span class="calln">{s["duration_sec"]:.1f}s &middot; '
            f'{s["total_candidates"]} candidate'
            f'{"" if s["total_candidates"] == 1 else "s"}</span></div>'
            for s in aggregate["scanned"]
        )
        body.append(
            '<section class="card"><div class="ctitle">Calls scanned</div>'
            '<div class="tnote">Every dual-channel recording walked, with its '
            'duration and how many candidate moments it surfaced.</div>'
            + rows + '</section>'
        )

    body.append(
        '<div class="hearcap">Promote a moment that matters to a permanent '
        'regression test with <span class="mono">hotato fixture create --onset '
        '&lt;t&gt; --expect yield|hold</span>.</div>'
    )
    body.append(_report._footer())
    body.append('</main>')

    folder = esc(aggregate["folder"])
    desc = (f"Self-contained hotato analyze dashboard: {total} candidate "
            f"turn-taking moments ranked across {n_calls} calls in {folder}, "
            "each with a to-scale timeline and, for the top moments, the real "
            "audio embedded with a synced playhead. Offline; measured timing "
            "candidates you review and label.")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>hotato analyze: {folder}, {total} candidate moments</title>"
        f"<meta name=\"description\" content=\"{esc(desc)}\">"
        f"<style>{css}</style></head><body>"
        + "".join(body)
        + f"<script>{_PLAYER_JS}</script><script>{_ACTIONS_JS}</script>"
        "</body></html>\n"
    )
