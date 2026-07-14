"""``hotato fixture create`` / ``hotato fixture promote``: turn one bad call
moment into a permanent regression fixture.

The input is a recording you already have (one two-channel WAV, or two aligned
mono WAVs), the moment the caller took or attempted the floor (``--onset``),
and YOUR label for what the agent should have done (``--expect yield`` or
``--expect hold``). The output is a scenario JSON plus a two-channel example
WAV in the exact shape ``hotato run --scenarios DIR --audio DIR`` scores, so
the moment becomes a test the same command can run forever.

Hotato does not infer intent. You label the expected behavior for the event:
yield means the agent should stop for the caller. hold means the agent should
keep speaking through a backchannel/noise/acknowledgement. Hotato then
measures whether the timing matched that label.

By default the fixture is CLIPPED around the event (``--pre`` seconds before
the onset, ``--post`` after) and ``caller_onset_sec`` is re-based to the clip,
so the fixture stays small and the measured timings match the uncut original.
``--no-clip`` keeps the full recording and the original onset.

Every write is validated immediately by scoring the created fixture through
the same suite runner. An input that cannot be scored (for example the agent
was not talking at the onset on a should-yield label) is refused with the
honest reason and exit code 2, never written as a fixture that would report a
meaningless verdict. Everything runs offline; no audio leaves the machine.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from typing import Optional

from . import labelrecord as _labelrecord
from ._engine.audio import write_wav
from .core import (
    _read_wav,
    _require_channel,
    _require_distinct_channels,
    _stream_pcm_sha256,
    run_suite,
)
from .errors import open_regular as _open_regular

__all__ = [
    "create_fixture",
    "parse_candidate_ref",
    "promote_candidate",
    "render_promote_text",
    "promote_result_json",
    "CREATED_BY",
    "PROMOTED_BY",
]

CREATED_BY = "hotato fixture create"
PROMOTED_BY = "hotato fixture promote"

# Same slug rule as the corpus label schema (corpus/label.schema.json).
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_WHY_YIELD = ("Created from a real call moment. The label says the agent "
              "should yield.")
_WHY_HOLD = ("Created from a real call moment. The label says the agent "
             "should hold.")


def _load_channels(*, stereo, caller, agent, caller_channel, agent_channel):
    """Resolve the input form into (caller_samples, agent_samples,
    sample_rate, source_name). Malformed input raises ValueError (exit 2)."""
    if stereo and (caller or agent):
        raise ValueError(
            "provide ONE input form: --stereo FILE, or both --caller FILE "
            "and --agent FILE, not both forms at once"
        )
    if stereo:
        signal = _read_wav(stereo)
        if signal.num_channels < 2:
            raise ValueError(
                "--stereo file has one channel; a single mixed mono call is "
                "not enough to attribute talk-over reliably. Pass --caller "
                "and --agent as two mono files, or export a real two-channel "
                "recording."
            )
        _require_distinct_channels(caller_channel, agent_channel)
        _require_channel(signal, caller_channel, "caller")
        _require_channel(signal, agent_channel, "agent")
        return (signal.get(caller_channel), signal.get(agent_channel),
                signal.sample_rate, os.path.basename(stereo))
    if caller and agent:
        c = _read_wav(caller)
        a = _read_wav(agent)
        if c.sample_rate != a.sample_rate:
            raise ValueError(
                f"sample-rate mismatch (caller {c.sample_rate} Hz, agent "
                f"{a.sample_rate} Hz); resample so both match."
            )
        n = min(c.num_samples, a.num_samples)
        return (c.get(0)[:n], a.get(0)[:n], c.sample_rate,
                f"{os.path.basename(caller)}+{os.path.basename(agent)}")
    raise ValueError(
        "provide --stereo FILE, or both --caller FILE and --agent FILE"
    )


def _parse_tags(tags: Optional[str]) -> list:
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


def _default_reviewer_principal() -> str:
    """The reviewer identity a label-record cites when the caller does not
    supply one: whoever this machine's shell says ran the command."""
    return (os.environ.get("HOTATO_REVIEWER")
            or os.environ.get("USER")
            or os.environ.get("USERNAME")
            or "unknown-reviewer")


def _mint_fixture_label_record(*, audio_path: str, want_yield: bool,
                               reviewer_principal: Optional[str],
                               rationale: Optional[str] = None) -> Optional[dict]:
    """Mint a label-record for the fixture's audio (the human running this
    workflow chose --expect, i.e. the decision), if SOME signing key is
    configured. Returns ``None`` (never raises) when no key is configured at
    all: the fixture still gets created, its label just stays an "asserted"
    (operator-only) expectation rather than a signed "human"/"human-shared"
    one -- never a crash on a machine that has not set up signing yet."""
    event_pcm_sha256 = _stream_pcm_sha256(audio_path)
    try:
        return _labelrecord.mint_label_record(
            reviewer_principal=reviewer_principal or _default_reviewer_principal(),
            event_audio_pcm_sha256=event_pcm_sha256,
            decision="yield" if want_yield else "hold",
            rationale=rationale,
        )
    except _labelrecord.NoSigningKeyConfigured:
        return None


def _validate_created(scenario: dict, audio_dir: str,
                      stack: Optional[str]) -> dict:
    """Score the created fixture through the SAME suite runner `hotato run
    --scenarios --audio` uses, isolated to just this scenario, and return the
    envelope. This is the round-trip guarantee: if this envelope exists, the
    fixture is runnable as written."""
    with tempfile.TemporaryDirectory(prefix="hotato-fixture-") as tmp:
        with open(os.path.join(tmp, scenario["id"] + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(scenario, fh)
        return run_suite(scenarios_dir=tmp, audio_dir=audio_dir, stack=stack)


def create_fixture(
    *,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    fixture_id: str,
    title: Optional[str] = None,
    onset_sec: float,
    expect: str = "yield",
    out_dir: str,
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    tags: Optional[str] = None,
    category: Optional[str] = None,
    pre_sec: float = 2.0,
    post_sec: float = 6.0,
    no_clip: bool = False,
    force: bool = False,
    caller_channel: int = 0,
    agent_channel: int = 1,
    created_by: str = CREATED_BY,
    provenance_extra: Optional[dict] = None,
    reviewer_principal: Optional[str] = None,
    rationale: Optional[str] = None,
) -> dict:
    """Create one regression fixture and validate it. Returns a result dict:
    ``{"id", "paths", "scenario", "validation", "onset", "next"}``.

    Raises ValueError (CLI exit 2) for a malformed input, an unusable label,
    an existing fixture without ``force``, or a created fixture that is not
    scorable (the honest reason is in the message; partial outputs are
    removed unless ``force``).
    """
    if not _SLUG_RE.match(fixture_id or ""):
        raise ValueError(
            f"--id {fixture_id!r} is not a valid fixture id; use a lowercase "
            "slug like refund-interruption-001 (letters, digits, hyphens)"
        )
    want_yield = str(expect).strip().lower() not in ("hold", "no", "false",
                                                     "hold-floor")
    resolved_category = category or ("should_yield" if want_yield
                                     else "should_not_yield")
    if resolved_category not in ("should_yield", "should_not_yield"):
        raise ValueError(
            f"--category {resolved_category!r} is not a scenario category; "
            "use should_yield or should_not_yield"
        )
    if want_yield != (resolved_category == "should_yield"):
        raise ValueError(
            f"--category {resolved_category!r} contradicts --expect "
            f"{'yield' if want_yield else 'hold'}; drop one of the two"
        )
    if not want_yield and (max_talk_over_sec is not None
                           or max_time_to_yield_sec is not None):
        raise ValueError(
            "--max-talk-over and --max-time-to-yield bound a yield; they do "
            "not apply to --expect hold (a hold fixture fails exactly when "
            "the agent yields)"
        )
    if onset_sec is None or onset_sec < 0:
        raise ValueError(
            f"--onset must be >= 0 seconds (time from the start of the "
            f"recording); got {onset_sec}."
        )
    if pre_sec < 0 or post_sec <= 0:
        raise ValueError(
            f"--pre must be >= 0 and --post must be > 0 seconds; got "
            f"pre={pre_sec}, post={post_sec}."
        )
    if not out_dir:
        raise ValueError("--out DIR is required (e.g. --out tests/hotato)")

    caller_samples, agent_samples, sample_rate, source = _load_channels(
        stereo=stereo, caller=caller, agent=agent,
        caller_channel=caller_channel, agent_channel=agent_channel,
    )
    n = min(len(caller_samples), len(agent_samples))
    duration = n / sample_rate
    if onset_sec >= duration:
        raise ValueError(
            f"--onset {onset_sec}s is beyond the end of the recording "
            f"({duration:.2f}s)."
        )

    # Clip bounds in samples. Default: [onset - pre, onset + post], clamped
    # to the recording; an onset near the start clips from 0 and the fixture
    # onset is re-based accordingly.
    if no_clip:
        start_idx, end_idx = 0, n
    else:
        start_idx = max(0, int(round((onset_sec - pre_sec) * sample_rate)))
        end_idx = min(n, int(round((onset_sec + post_sec) * sample_rate)))
    fixture_onset = round(onset_sec - start_idx / sample_rate, 3)
    clip_start_sec = round(start_idx / sample_rate, 3)
    clip_end_sec = round(end_idx / sample_rate, 3)

    expected = {
        "yield": want_yield,
        "max_time_to_yield_sec": max_time_to_yield_sec if want_yield else None,
        "max_talk_over_sec": max_talk_over_sec if want_yield else None,
    }
    scenario = {
        "id": fixture_id,
        "title": title or fixture_id.replace("-", " "),
        "category": resolved_category,
        "tags": _parse_tags(tags),
        "sample_rate": sample_rate,
        "duration_sec": round((end_idx - start_idx) / sample_rate, 3),
        "caller_onset_sec": fixture_onset,
        "expected": expected,
        "why_it_matters": _WHY_YIELD if want_yield else _WHY_HOLD,
        "related_signals": (["did_yield", "time_to_yield", "talk_over"]
                            if want_yield else ["did_yield"]),
        "provenance": {
            "source": source,
            "source_onset_sec": round(onset_sec, 3),
            "clip_start_sec": clip_start_sec,
            "clip_end_sec": clip_end_sec,
            "created_by": created_by,
        },
    }
    if provenance_extra:
        # Additive only (promote records its candidate ref here); the core
        # provenance keys above stay authoritative.
        for key, value in provenance_extra.items():
            scenario["provenance"].setdefault(key, value)

    scenarios_dir = os.path.join(out_dir, "scenarios")
    audio_dir = os.path.join(out_dir, "audio")
    labels_dir = os.path.join(out_dir, "labels")
    scenario_path = os.path.join(scenarios_dir, fixture_id + ".json")
    audio_path = os.path.join(audio_dir, fixture_id + ".example.wav")
    label_path = _labelrecord.label_record_path(labels_dir, fixture_id)
    existing = [p for p in (scenario_path, audio_path, label_path)
                if os.path.exists(p)]
    if existing and not force:
        raise ValueError(
            f"fixture {fixture_id!r} already exists ({', '.join(existing)}); "
            "pass --force to overwrite it, or pick a new --id"
        )
    os.makedirs(scenarios_dir, exist_ok=True)
    os.makedirs(audio_dir, exist_ok=True)

    # The audio is ALWAYS one two-channel WAV (caller on channel 0, agent on
    # channel 1), also when the input arrived as two mono files.
    write_wav(audio_path, sample_rate,
              [caller_samples[start_idx:end_idx],
               agent_samples[start_idx:end_idx]])

    # A human ran this workflow and chose --expect: mint a signed label-record
    # bound to the EXACT decoded audio just written, if some signing key is
    # configured (Ed25519 via sign.py, else the shared HMAC key). Absent a key,
    # this stays None -- the fixture is still created, just with an honest
    # "asserted" (not falsely "human") label authority downstream.
    label_record = _mint_fixture_label_record(
        audio_path=audio_path, want_yield=want_yield,
        reviewer_principal=reviewer_principal, rationale=rationale)
    if label_record is not None:
        scenario["label_record"] = label_record
        os.makedirs(labels_dir, exist_ok=True)
        _labelrecord.save_label_record(label_path, label_record)

    with open(scenario_path, "w", encoding="utf-8") as fh:
        json.dump(scenario, fh, indent=2)
        fh.write("\n")

    validation = _validate_created(scenario, audio_dir, stack)
    event = next(e for e in validation["events"]
                 if e["event_id"] == fixture_id)
    if event.get("scorable") is False:
        reason = event.get("not_scorable_reason") or "not scorable"
        if not force:
            for p in (scenario_path, audio_path, label_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            kept = "the partial outputs were removed"
        else:
            kept = "--force kept the files on disk for inspection"
        raise ValueError(
            f"the created fixture is not scorable, so it was refused "
            f"({kept}). Reason: {reason}"
        )

    return {
        "id": fixture_id,
        "paths": {"scenario": scenario_path, "audio": audio_path},
        "scenario": scenario,
        "validation": validation,
        "onset": {"source_sec": round(onset_sec, 3),
                  "fixture_sec": fixture_onset},
        "next": (f"hotato run --scenarios {shlex.quote(scenarios_dir)} "
                 f"--audio {shlex.quote(audio_dir)} --format text"),
    }


def render_text(result: dict) -> str:
    onset = result["onset"]
    lines = [
        f"created Hotato fixture: {result['id']}",
        f"  scenario: {result['paths']['scenario']}",
        f"  audio:    {result['paths']['audio']}",
        f"  onset:    {onset['source_sec']:.2f}s source -> "
        f"{onset['fixture_sec']:.2f}s fixture",
        f"  expect:   "
        f"{'yield' if result['scenario']['expected']['yield'] else 'hold'}",
        "  check:    scorable",
        "next:",
        f"  {result['next']}",
    ]
    return "\n".join(lines)


def result_json(result: dict) -> dict:
    """The machine shape printed by ``--format json``."""
    return {
        "tool": "hotato",
        "kind": "fixture",
        "schema_version": "1",
        "id": result["id"],
        "paths": result["paths"],
        "onset": result["onset"],
        "scenario": result["scenario"],
        "validation": result["validation"],
        "next": result["next"],
    }


# --- ``hotato fixture promote``: one sweep/analyze candidate -> a fixture ---
#
# ``sweep`` / ``analyze`` surface candidate moments; ``promote`` is the step
# that says "this suspicious moment is real, save it forever." The ref names
# the result file and the candidate; the candidate carries the recording, the
# onset, and the kind, so the only thing you add is the label. Everything
# after resolution is the exact ``create_fixture`` path above, including the
# immediate scorability validation and its honest refusal.

_REF_FORMS = (
    "FILE#N (the Nth candidate in FILE, matching the #N rank in the report) "
    "or FILE#CALL:N (the Nth candidate from call CALL), e.g. "
    "hotato-sweep.json#3 or analyze.json#call_abc123:2"
)

_NUM_RE = re.compile(r"^[0-9]+$")


def parse_candidate_ref(ref: str):
    """Split a candidate ref into ``(path, call, number)``; ``call`` is None
    for the ``FILE#N`` form. Numbers are 1-based in the file's ranked
    candidate order -- the same ``#N`` rank the HTML report shows. A
    malformed ref raises ValueError (exit 2)."""
    path, sep, rest = (ref or "").rpartition("#")
    if not sep or not path or not rest:
        raise ValueError(f"{ref!r} is not a candidate ref; use {_REF_FORMS}")
    call = None
    num = rest
    if not _NUM_RE.match(rest):
        call, sep2, num = rest.rpartition(":")
        if not sep2 or not call or not _NUM_RE.match(num):
            raise ValueError(
                f"{ref!r} is not a candidate ref; use {_REF_FORMS}"
            )
    number = int(num)
    if number < 1:
        raise ValueError(
            f"candidate numbers start at 1 (got {ref!r}); #1 is the "
            "top-ranked candidate, the same rank the report shows"
        )
    return path, call, number


def _load_result(path: str) -> dict:
    """Read a ``hotato sweep/analyze --format json`` result file. A missing
    file raises FileNotFoundError (exit 2, file_not_found); a file that is
    not an analyze-kind envelope with a candidates list raises ValueError
    with the honest reason."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path!r} is not JSON ({exc}); pass the file written by "
                "hotato sweep --format json or hotato analyze --format json"
            ) from exc
    if (not isinstance(doc, dict) or doc.get("kind") != "analyze"
            or not isinstance(doc.get("candidates"), list)):
        raise ValueError(
            f"{path!r} is not a hotato sweep/analyze result (expected kind "
            "'analyze' with a candidates list); write one with hotato sweep "
            "--format json or hotato analyze --format json"
        )
    return doc


def _call_names(source: str) -> set:
    """Every name a candidate's source recording answers to in a
    ``FILE#CALL:N`` ref: the source path as written, its basename, the
    basename with one or all extensions stripped (``x.example.wav`` answers
    to ``x.example`` and ``x``), and -- for a pulled recording named
    ``STACK__ID.wav`` -- the bare call id."""
    base = os.path.basename(source)
    stem = os.path.splitext(base)[0]
    names = {source, base, stem, stem.split(".", 1)[0]}
    if "__" in stem:
        names.add(stem.split("__", 1)[1])
    return names


def _resolve_candidate(doc: dict, *, path: str, call, number: int) -> dict:
    cands = doc["candidates"]
    if not cands:
        raise ValueError(
            f"{path} has no candidates to promote (total_candidates is 0)"
        )
    if call is None:
        pool = cands
        scope = f"{path} has {len(pool)}"
    else:
        pool = [c for c in cands
                if call in _call_names(str(c.get("source", "")))]
        if not pool:
            known = sorted({
                os.path.splitext(os.path.basename(str(c.get("source", ""))))[0]
                for c in cands
            })
            raise ValueError(
                f"no candidate in {path} comes from call {call!r}; calls in "
                f"this file: {', '.join(known)}"
            )
        scope = f"call {call!r} in {path} has {len(pool)}"
    if number > len(pool):
        raise ValueError(
            f"candidate {number} is out of range: {scope} candidate"
            f"{'' if len(pool) == 1 else 's'}, numbered 1..{len(pool)} in "
            "rank order (the #N rank the report shows)"
        )
    return pool[number - 1]


def _resolve_source_audio(doc: dict, cand: dict, *, ref_path: str,
                          folder: Optional[str] = None) -> str:
    """Find the candidate's source recording on disk. An explicit ``folder``
    is authoritative (no silent fallback past it); otherwise the folder the
    result file recorded is tried (absolute ``folder_path`` first, then the
    ``folder`` name relative to the working directory and to the result
    file), then the source path itself. The error names every path tried."""
    if not isinstance(cand, dict) or not cand.get("source"):
        raise ValueError(
            f"the candidate has no 'source' field; {ref_path!r} does not "
            "look like a hotato sweep/analyze result"
        )
    source = str(cand["source"])
    if folder:
        p = os.path.join(folder, source)
        if os.path.isfile(p):
            return p
        raise ValueError(
            f"the source recording {source!r} was not found under --folder "
            f"{folder!r} (tried {p!r})"
        )
    ref_dir = os.path.dirname(os.path.abspath(ref_path))
    roots = [doc.get("folder_path")]
    fname = doc.get("folder")
    if fname:
        roots += [fname, os.path.join(ref_dir, fname)]
    tried = []
    for p in ([os.path.join(r, source) for r in roots if r]
              + [source, os.path.join(ref_dir, source)]):
        if p not in tried:
            tried.append(p)
    for p in tried:
        if os.path.isfile(p):
            return p
    raise ValueError(
        f"the source recording {source!r} was not found (tried: "
        + ", ".join(repr(p) for p in tried)
        + "); pass --folder DIR pointing at the folder that was "
        "swept/analyzed"
    )


def promote_candidate(
    ref: str,
    *,
    expect: str,
    fixture_id: str,
    out_dir: str,
    folder: Optional[str] = None,
    title: Optional[str] = None,
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    tags: Optional[str] = None,
    pre_sec: float = 2.0,
    post_sec: float = 6.0,
    no_clip: bool = False,
    force: bool = False,
    caller_channel: int = 0,
    agent_channel: int = 1,
    reviewer_principal: Optional[str] = None,
) -> dict:
    """Promote one sweep/analyze candidate into a permanent regression
    fixture. Resolves the ref to its candidate (source recording, onset,
    kind), then runs the exact :func:`create_fixture` path on that recording
    at that onset. Returns the ``create_fixture`` result dict plus a
    ``candidate`` block. Raises ValueError (CLI exit 2) for a bad ref, a
    file that is not a sweep/analyze result, a source recording that does
    not resolve, or a candidate whose fixture is not scorable."""
    path, call, number = parse_candidate_ref(ref)
    doc = _load_result(path)
    cand = _resolve_candidate(doc, path=path, call=call, number=number)
    audio = _resolve_source_audio(doc, cand, ref_path=path, folder=folder)
    if cand.get("t_sec") is None:
        raise ValueError(
            f"the candidate at {ref!r} has no 't_sec' (onset position); "
            f"{path!r} does not look like a hotato sweep/analyze result"
        )
    result = create_fixture(
        stereo=audio,
        fixture_id=fixture_id,
        title=title,
        onset_sec=float(cand["t_sec"]),
        expect=expect,
        out_dir=out_dir,
        stack=stack,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
        tags=tags,
        pre_sec=pre_sec,
        post_sec=post_sec,
        no_clip=no_clip,
        force=force,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        created_by=PROMOTED_BY,
        reviewer_principal=reviewer_principal,
        provenance_extra={
            "candidate_ref": ref,
            "candidate_kind": cand.get("kind"),
            "result_file": os.path.basename(path),
        },
    )
    result["candidate"] = {
        "ref": ref,
        "source": cand.get("source"),
        "t_sec": cand.get("t_sec"),
        "kind": cand.get("kind"),
        "salience": cand.get("salience"),
    }
    return result


def render_promote_text(result: dict) -> str:
    """The promote text output: one line naming what was promoted, then the
    same created-fixture block ``fixture create`` prints (paths, onset,
    label, scorability check, and the exact next command)."""
    c = result["candidate"]
    head = (f"promoted {c['ref']}: {c['kind']} at t={c['t_sec']:.2f}s in "
            f"{c['source']}")
    return head + "\n" + render_text(result)


def promote_result_json(result: dict) -> dict:
    """The machine shape for ``fixture promote --format json``: the
    ``fixture create`` envelope plus the resolved candidate block."""
    out = result_json(result)
    out["candidate"] = result["candidate"]
    return out
