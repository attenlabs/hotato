"""``hotato start --demo``: the guided, credential-less first run.

One command, no account, no network. Two acts:

* **Act one (timing, first -- the wedge):** sweeps the two bundled real demo
  calls (the same recordings ``hotato demo`` scores), writes the sweep result
  as JSON and as a self-contained HTML dashboard, renders the threshold-funnel
  card, creates and verifies one demo failure contract, and projects the
  share-safe Failure Record.
* **Act two (say-do):** runs one conversation check end to end over the
  bundled scripted say-do conversation (``data/demo/saydo/``, mirroring the
  reference agent's ``refund-claimed-not-issued`` job) through the SAME
  evaluate path ``hotato test run`` uses. The agent's words claim the refund
  was sent; the trace carries no ``issue_refund`` tool span and the post-call
  state's ``refund_status`` stays ``"none"`` -- so the check FAILS, by design,
  and the first stdout shows a timing catch AND a say-do catch.

It then prints the exact next commands -- promote a candidate into a permanent
fixture, run those fixtures in CI, verify the demo contract, replay the say-do
check, and render a card from any candidate or from the say-do result.

Everything is offline by construction: the demo pulls from packaged audio and
packaged conversation files, the analyze/card/contract/test steps touch no
network, and no credential is read.
"""

from __future__ import annotations

import os
import sys
from importlib import resources
from typing import Optional

from . import __version__
from . import card as _card
from . import errors as _errors

_SWEEP_JSON = "hotato-sweep.json"
_SWEEP_HTML = "hotato-sweep.html"
_FUNNEL_CARD = "hotato-no-single-threshold.svg"
_CONTRACTS_DIR = "contracts"
_DEMO_CONTRACT_ID = "demo-missed-interruption"
# The canonical share-safe Failure Record the first run leaves behind: one
# evidence-specific artifact a reader attaches to a PR or verifies in one
# command, rendered as JSON/Markdown/HTML/SVG. The directory holds ONLY these
# four record files -- the contract-verify source envelope, audio, transcript,
# trace, and state stay OUT of it (the demo contract bundle under contracts/
# remains separately available for local replay).
_DEMO_RECORD_DIR = "hotato-failure-record"
_DEMO_RECORD_FILES = ("failure-record.json", "failure-record.md",
                      "failure-record.html", "failure-record.svg")
# The stack-agnostic durable adoption path the demo now leads with, plus the
# stack-tuned alternatives documented beside it.
_DEMO_STARTER_PRIMARY = "hotato init starter --stack generic --out ."
_DEMO_STARTER_STACKS = ("vapi", "retell", "twilio", "livekit", "pipecat")
# --- act two: the bundled say-do conversation check -------------------------
# The packaged scripted conversation (data/demo/saydo/) mirrors the reference
# agent's refund-claimed-not-issued job: the words claim success, the tool and
# state evidence fail. Each packaged file is verified against the sha256 its
# manifest records before use (content-addressed like the rest of the demo
# data), and the check runs through the REAL conversation-test machinery.
_SAYDO_DIR = "saydo"
_SAYDO_BUNDLE_FILES = ("test.json", "transcript.json", "trace.jsonl",
                       "state.json")
_SAYDO_RESULT = "test-run.json"
_SAYDO_CARD = "saydo-card.svg"
_SAYDO_AGENT = "demo-agent"
# The claim-vs-evidence shape the second act narrates, verified against the
# evaluated results before a word of it is printed: the transcript really
# carries the claim (the phrase assertion PASSes) and the evidence really
# fails (the tool_result and state assertions FAIL).
_SAYDO_CLAIM_ID = "agent-said-refund-sent"
_SAYDO_EVIDENCE_IDS = ("outcome-refund-tool", "outcome-refund-state")
# The demo contract's moment is selected SEMANTICALLY, never by rank. The
# packaged should-yield scenario (fd-01-missed-interruption.json) declares the
# audio file, the caller onset, and the expectation; the selector below finds
# the one sweep candidate that matches that declaration (an
# overlap_while_agent_talking event in that audio at that onset). Rank-based
# selection shipped a wrong moment once: the sweep's #2 candidate was an
# agent_stop_no_caller event, so the "the agent talked over the caller" story
# verified with talk_over=0.0.
_DEMO_ONSET_TOLERANCE_SEC = 0.5


class DemoSelectionError(RuntimeError):
    """Internal contract: the packaged demo sweep and the packaged scenario
    declaration no longer agree. The first run must fail loudly here rather
    than build the demo contract from the wrong moment."""


class DemoCandidateNotFound(DemoSelectionError):
    """No sweep candidate matches the scenario's declared event."""


class DemoCandidateAmbiguous(DemoSelectionError):
    """More than one sweep candidate matches the scenario's declared event."""


class DemoSayDoError(RuntimeError):
    """Internal contract: the packaged say-do conversation, its recorded
    content hashes, and its conversation-test no longer agree -- or no longer
    produce the claim-PASS / evidence-FAIL shape act two narrates. The first
    run must fail loudly here rather than print a say-do catch the evaluated
    evidence does not back."""


def _demo_scenario() -> dict:
    """The packaged should-yield demo scenario (the missed interruption)."""
    import json as _json

    path = resources.files("hotato").joinpath(
        "data", "demo", "failing", "scenarios", "fd-01-missed-interruption.json")
    with path.open(encoding="utf-8") as fh:
        return _json.load(fh)


def _select_demo_candidate(sweep: dict, scenario: dict) -> int:
    """Return the 1-based rank of the sweep candidate that IS the scenario's
    declared missed interruption: an overlap-while-agent-talking event in the
    scenario's audio at the declared caller onset. Malformed candidate entries
    are skipped and can never be selected. Zero and multiple matches raise
    distinct internal-contract errors; reordering the sweep cannot change
    which moment is chosen."""
    audio = scenario["audio"]
    onset = float(scenario["caller_onset_sec"])
    matches = []
    for i, cand in enumerate(sweep.get("candidates") or [], 1):
        if not isinstance(cand, dict):
            continue
        if cand.get("kind") != "overlap_while_agent_talking":
            continue
        if cand.get("source") != audio:
            continue
        try:
            t_sec = float(cand.get("t_sec"))
        except (TypeError, ValueError):
            continue
        if abs(t_sec - onset) <= _DEMO_ONSET_TOLERANCE_SEC:
            matches.append(i)
    if not matches:
        raise DemoCandidateNotFound(
            f"no sweep candidate matches the declared demo event "
            f"(kind=overlap_while_agent_talking, source={audio!r}, "
            f"t within {_DEMO_ONSET_TOLERANCE_SEC}s of {onset}s)")
    if len(matches) > 1:
        raise DemoCandidateAmbiguous(
            f"{len(matches)} sweep candidates match the declared demo event "
            f"at ranks {matches}; the declaration must identify exactly one")
    return matches[0]


def _demo_audio_dir() -> str:
    return str(resources.files("hotato").joinpath("data", "demo", "failing",
                                                   "audio"))


# --- act two: the say-do check on the bundled scripted conversation ---------

def _saydo_source_dir():
    return resources.files("hotato").joinpath("data", "demo", "saydo")


def _load_saydo_bundle() -> dict:
    """Read the packaged say-do bundle and VERIFY each file's bytes against the
    sha256 its packaged manifest records (content-addressed, like the rest of
    the demo data), returning ``{name: bytes}``. A file whose bytes drift from
    the manifest raises :class:`DemoSayDoError` -- the demo never evaluates
    say-do evidence its manifest does not vouch for."""
    import hashlib as _hashlib
    import json as _json

    src = _saydo_source_dir()
    # open-ok: packaged bundled-resource path (importlib.resources), no
    # user-supplied path can reach it.
    manifest = _json.loads(
        src.joinpath("manifest.json").read_text(encoding="utf-8"))
    declared = manifest.get("files") or {}
    out = {}
    for name in _SAYDO_BUNDLE_FILES:
        # open-ok: packaged bundled-resource path (importlib.resources).
        blob = src.joinpath(name).read_bytes()
        want = (declared.get(name) or {}).get("sha256")
        got = _hashlib.sha256(blob).hexdigest()
        if got != want:
            raise DemoSayDoError(
                f"internal contract: packaged say-do file {name!r} hashes to "
                f"{got}, but the packaged manifest records {want!r}; the demo "
                "refuses to evaluate say-do evidence its manifest does not "
                "vouch for")
        out[name] = blob
    return out


def _saydo_next_command() -> str:
    """The one-line replay of act two against the copies in ``--dir``: the
    public ``hotato test run`` gate over the same files, exit 1."""
    return (f"hotato test run {_SAYDO_DIR}/test.json --agent {_SAYDO_AGENT} "
            f"--transcript {_SAYDO_DIR}/transcript.json "
            f"--trace {_SAYDO_DIR}/trace.jsonl --state {_SAYDO_DIR}/state.json")


def _saydo_card_command() -> str:
    return f"hotato card {_SAYDO_DIR}/{_SAYDO_RESULT} --out {_SAYDO_CARD}"


def _run_saydo_check(out_dir: str) -> dict:
    """Act two of the guided demo: ONE say-do conversation check, end to end,
    on the bundled scripted conversation -- through the REAL conversation-test
    machinery (the same ``evaluate_conversation_test`` path ``hotato test run``
    drives), offline, no credential.

    Copies the hash-verified bundle into ``<out_dir>/saydo/`` (so the printed
    replay command runs against the exact files this run evaluated), builds
    the assert Context from the copies (transcript + voice-trace spans + the
    mock post-call state sandbox), evaluates the test, and writes the full
    ``hotato.test-run.v1`` result as ``saydo/test-run.json`` (the input the
    say-do card renders).

    Like the Failure Record, this is an ESSENTIAL demo invariant: before a
    word of the say-do story is printed, the evaluated results must hold the
    claim-vs-evidence shape it narrates -- the claim assertion PASSes (the
    transcript really carries the agent's claim) and every evidence assertion
    FAILs (the trace and post-call state really contradict it) with the check
    exiting 1. Anything else raises :class:`DemoSayDoError` instead of
    shipping a narrated catch the evidence does not back."""
    import json as _json

    from . import assert_ as A
    from . import conversation_test as CT
    from . import test_run as TR
    from .state_adapter import MockStateAdapter

    bundle = _load_saydo_bundle()
    saydo_dir = os.path.join(out_dir, _SAYDO_DIR)
    os.makedirs(saydo_dir, exist_ok=True)
    for name in _SAYDO_BUNDLE_FILES:
        _write_text(os.path.join(saydo_dir, name),
                    bundle[name].decode("utf-8"))

    doc = CT.validate_conversation_test_doc(
        _json.loads(bundle["test.json"].decode("utf-8")))
    ctx = A.build_context(
        transcript_path=os.path.join(saydo_dir, "transcript.json"),
        trace_path=os.path.join(saydo_dir, "trace.jsonl"),
        state_adapter=MockStateAdapter(
            _json.loads(bundle["state.json"].decode("utf-8"))),
    )
    result = TR.evaluate_conversation_test(doc, ctx, agent_id=_SAYDO_AGENT)

    by_id = {r.get("id"): r for r in result["assertions"]["results"]}
    claim = by_id.get(_SAYDO_CLAIM_ID)
    if not isinstance(claim, dict) or claim.get("status") != "PASS":
        raise DemoSayDoError(
            "internal contract: the packaged transcript no longer carries the "
            f"agent's claim (assertion {_SAYDO_CLAIM_ID!r} did not PASS); the "
            "demo must never narrate a claim the transcript does not hold")
    evidence = []
    for aid in _SAYDO_EVIDENCE_IDS:
        r = by_id.get(aid)
        if not isinstance(r, dict) or r.get("status") != "FAIL":
            raise DemoSayDoError(
                f"internal contract: packaged say-do evidence assertion "
                f"{aid!r} did not FAIL; the demo must never present a say-do "
                "catch its evaluated evidence does not back")
        evidence.append({"id": r["id"], "kind": r["kind"],
                         "status": r["status"],
                         "public_reason": r.get("public_reason")})
    if result["exit_code"] != 1:
        raise DemoSayDoError(
            "internal contract: the say-do check exited "
            f"{result['exit_code']}, expected the by-design failing exit 1")

    _write_text(os.path.join(saydo_dir, _SAYDO_RESULT),
                _errors.safe_json_dumps(result, indent=2) + "\n")
    return {
        "dir": _SAYDO_DIR,
        "test_id": result["test_id"],
        "agent": _SAYDO_AGENT,
        "exit_code": result["exit_code"],
        "passed": result["success"]["passed"],
        "verified_fail_as_expected": result["success"]["passed"] is False,
        "claim_assertion": {"id": claim["id"], "kind": claim["kind"],
                            "status": claim["status"]},
        "evidence_assertions": evidence,
        "files": [f"{_SAYDO_DIR}/{name}"
                  for name in (*_SAYDO_BUNDLE_FILES, _SAYDO_RESULT)],
    }


def _write_text(path: str, text: str) -> None:
    from .cli import _atomic_write_text
    _atomic_write_text(path, text)


def _ensure_out_dir(out_dir: str) -> None:
    """Resolve ``--dir`` for a guided first run.

    A first run into a brand-new folder is the common case, so a ``--dir`` that
    does not exist yet is CREATED -- every missing parent in one ``os.makedirs``
    call, so ``--dir out/2026/first-run`` just works instead of erroring on the
    missing nesting.

    The containment/refusal check stays: a ``--dir`` that already exists as a
    NON-directory (a regular file, or a symlink to one) is refused rather than
    clobbered -- a clean exit-2 usage error via the shared HANDLED boundary, not
    a truncated file. Any creation ``OSError`` (a parent path component is a
    file, permission denied, a dangling symlink at the target) is HANDLED the
    same way, so the failure surface is the same exit-2 it always was."""
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise ValueError(
            f"--dir {out_dir!r} exists but is not a directory; point it at a "
            "directory (a missing one is created for you)")
    os.makedirs(out_dir, exist_ok=True)


def _funnel_plan() -> dict:
    """Build the threshold-funnel fix plan from the bundled failing demo
    battery, in process (no subprocess, no network). Same code the CLI's
    ``plan`` path runs: score the demo suite, diagnose it, build the plan."""
    from .core import run_suite
    from .diagnose import diagnose_envelope
    from .fixplan import build_plan

    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    return build_plan(diagnosis=diagnose_envelope(env))


def _sweep_demo(out_dir: str) -> dict:
    """Sweep the bundled demo calls into ``out_dir``: writes the JSON result and
    the HTML dashboard, and returns the aggregate. Mirrors ``capture.run_sweep``
    ``--demo``'s JSON envelope so the printed commands work against a real sweep
    unchanged."""
    from . import analyze as _analyze

    audio_dir = _demo_audio_dir()
    aggregate, per_file = _analyze.analyze_folder(audio_dir)

    top = 25
    capped = dict(aggregate)
    capped["candidates"] = aggregate["candidates"][:top]
    capped["shown"] = len(capped["candidates"])
    capped["pull"] = {"stack": "demo", "listed": aggregate["calls_scanned"],
                      "pulled": aggregate["calls_scanned"], "skipped": 0}

    json_path = os.path.join(out_dir, _SWEEP_JSON)
    _write_text(json_path, _errors.safe_json_dumps(capped, indent=2) + "\n")

    html = _analyze.build_dashboard_html(
        aggregate, per_file, top=top, audio_top=8, report_json=_SWEEP_JSON)
    _write_text(os.path.join(out_dir, _SWEEP_HTML), html)
    return aggregate


def _demo_contract_bundle_rel() -> str:
    """The demo contract's bundle path, relative to ``--dir`` (never joined
    with ``out_dir``): the same bare-relative convention ``_SWEEP_JSON`` and
    ``_FUNNEL_CARD`` use, since every printed/returned path here is meant to
    be read from inside ``--dir``."""
    from . import contract as _contract
    return f"{_CONTRACTS_DIR}/{_DEMO_CONTRACT_ID}{_contract.BUNDLE_SUFFIX}"


def _create_and_verify_demo_contract(out_dir: str, sweep_json_path: str) -> dict:
    """Create ``contracts/demo-missed-interruption.hotato`` from the bundled
    demo sweep candidate that matches the packaged scenario's declared
    missed interruption (selected by evidence fields, never by rank) with ``--expect yield``, then verify it
    immediately with ``hotato contract verify``. This turns the whole loop
    the README promises -- a real failure becomes a candidate, becomes a
    portable contract, and ``contract verify`` catches it -- into something a
    first run actually sees once, offline, with no credential.

    Returns ``{"bundle_dir", "bundle_rel", "contracts_dir", "candidate_rank",
    "passed", "scorable", "verify"}`` -- ``candidate_rank`` is the 1-based sweep
    rank of the evidence-selected missed interruption (so the printed golden
    path promotes THAT moment with ``--expect yield``, never a hardcoded #1),
    and ``verify`` is the FULL in-memory ``contract-verify`` envelope, so the
    caller can project the canonical Failure Record from the same evidence
    without re-reading or copying anything to disk. Raises on the SAME conditions ``contract create``/
    ``contract verify`` would (never on the bundled demo audio in practice);
    the caller treats a failure here the same defensive way it treats a card
    render failure -- the guided run still finishes.
    """
    import json as _json

    from . import contract as _contract
    from .errors import open_regular as _open_regular

    with _open_regular(sweep_json_path, "r", encoding="utf-8") as fh:
        sweep = _json.load(fh)
    rank = _select_demo_candidate(sweep, _demo_scenario())

    contracts_dir = os.path.join(out_dir, _CONTRACTS_DIR)
    create_result = _contract.create_contract(
        from_candidate=f"{sweep_json_path}#{rank}",
        contract_id=_DEMO_CONTRACT_ID,
        expect="yield",
        out_dir=contracts_dir,
        force=True,
    )
    verify = _contract.verify_contracts(contracts_dir)
    result = verify["results"][0]
    if result["scorable"] is not True:
        raise DemoSelectionError(
            "internal contract: the selected demo candidate verified as "
            f"not scorable ({result.get('not_scorable_reason')!r}); the demo "
            "must never present an unscorable moment as a failure")
    # Display-layer enrichments (never written into the byte-stable machine
    # verify output above): the measured-timing stat row -- including the
    # response gap the golden path otherwise drops -- and the caught moment's
    # caller/agent overlap timeline, both read from the SAME bundled event.wav
    # at the SAME onset the verify used.
    try:
        onset = float(sweep["candidates"][rank - 1].get("t_sec"))
    except (TypeError, ValueError, IndexError, KeyError):
        onset = None
    event_wav = os.path.join(create_result["dir"], "audio", "event.wav")
    have_audio = os.path.isfile(event_wav)
    timing = _demo_timing(event_wav, onset) if have_audio else None
    timeline = _demo_timeline_model(event_wav, onset) if have_audio else None
    return {
        "bundle_dir": create_result["dir"],
        "bundle_rel": _demo_contract_bundle_rel(),
        "contracts_dir": contracts_dir,
        "candidate_rank": rank,
        "passed": result["passed"],
        "scorable": result["scorable"],
        "verify": verify,
        "timing": timing,
        "timeline": timeline,
    }


def _projection_envelope(verify: dict) -> dict:
    """A DETERMINISTIC, path-free view of the in-memory ``contract-verify``
    envelope, ready to project. ``verify_contracts`` stamps an absolute bundle
    ``dir`` on the envelope and on every result (the run's temp path); those
    vary per run and per machine, so a record projected straight from the raw
    envelope would get a different content address every time. They are dropped
    here so the projected record and its ``record_id`` depend ONLY on the
    re-scored evidence, never on where the demo happened to run. Nothing else is
    copied or altered; the raw envelope stays available for local replay."""
    import copy as _copy

    doc = _copy.deepcopy(verify)
    doc.pop("dir", None)
    for result in doc.get("results") or []:
        if isinstance(result, dict):
            result.pop("dir", None)
    return doc


def _demo_timing(audio_path: str, onset_sec) -> Optional[dict]:
    """The measured-timing stat row for the demo's caught moment: the barge-in
    verdict AND the endpointing latency the scorer ALREADY computes on the same
    two VAD tracks (``core.run_single`` -> ``score_channels``), for the human
    report and the share card. A DISPLAY read of the same scorer on the same
    bundled clip+onset the contract verify used -- it surfaces the response gap
    the golden path otherwise drops; it is never written into the machine
    ``contract verify`` output (which stays byte-stable). Best-effort: any hiccup
    returns ``None`` so the guided run still finishes."""
    if onset_sec is None:
        return None
    try:
        from .core import run_single

        env = run_single(stereo=audio_path, onset_sec=float(onset_sec),
                         expect="yield", caller_channel=0, agent_channel=1)
        ev = env["events"][0]
        v = ev.get("verdict") or {}
        meas = ev.get("measurements") or {}
        lat = (ev.get("signals") or {}).get("latency") or {}
        return {
            "caller_onset_sec": meas.get("caller_onset_sec"),
            "seconds_to_yield": v.get("seconds_to_yield"),
            "talk_over_sec": v.get("talk_over_sec"),
            "response_gap_sec": lat.get("response_gap_sec"),
            "premature_start_sec": lat.get("premature_start_sec"),
        }
    except Exception:  # pragma: no cover - defensive; enrichment is additive
        return None


def _demo_timeline_model(audio_path: str, onset_sec) -> Optional[dict]:
    """The caller/agent activity model for the demo's caught moment, from the
    SAME two VAD tracks the scan/score path walks (``scan.activity_tracks`` +
    ``report._spans``, the geometry the sweep dashboard already draws). Feeds the
    Failure Record card's overlap timeline. Best-effort: any read/decode hiccup
    returns ``None`` so the guided run still finishes -- the timeline is an
    additive enrichment, never load-bearing."""
    try:
        from . import report as _report
        from . import scan as _scan

        caller, agent, hop, _sr, dur = _scan.activity_tracks(audio_path)
        n = min(len(caller), len(agent))
        frames = [{"t_sec": i * hop, "caller_active": caller[i],
                   "agent_active": agent[i],
                   "both": bool(caller[i] and agent[i])} for i in range(n)]
        return {
            "duration": dur,
            "caller_spans": _report._spans(frames, "caller_active", hop),
            "agent_spans": _report._spans(frames, "agent_active", hop),
            "talkover_spans": _report._spans(frames, "both", hop),
            "onset": onset_sec,
            "yield_abs": None,
        }
    except Exception:  # pragma: no cover - defensive; enrichment is additive
        return None


def _write_demo_failure_record(out_dir: str, contract_info: dict) -> dict:
    """Project the demo's failing contract into the canonical share-safe
    Failure Record and render its four formats -- JSON, Markdown, HTML, SVG --
    into ``hotato-failure-record/``.

    The contract is picked by SELECTOR ``_DEMO_CONTRACT_ID`` (never by
    position), so reordering the verify results can never change which moment
    the record freezes. This is the artifact the whole growth loop turns on: a
    first run leaves one evidence-specific, share-safe record a reader can
    attach to a PR or verify in one command, with no second step.

    Record generation is an ESSENTIAL demo invariant: a selection, projection,
    or render failure raises here rather than being silently swallowed -- a
    first run never drops or fakes the record. The share directory is safe to
    attach as-is: ONLY the four record files are written into it; the
    contract-verify source envelope, audio, transcript, trace, and state are
    never copied there (the demo contract bundle under ``contracts/`` remains
    separately available for local replay).

    Returns the JSON ``failure_record`` metadata block: ``{"dir", "record_id",
    "headline", "privacy_profile", "files"}``.
    """
    from . import failure_record as _failure_record
    from . import failure_render as _failure_render

    verify = contract_info["verify"]
    record = _failure_record.project(
        _projection_envelope(verify), selector=_DEMO_CONTRACT_ID)
    # Render-only enrichments (from evidence this run already measured): the
    # measured-timing stat row and the caught moment's caller/agent overlap
    # timeline. They enrich the shareable Markdown/HTML/SVG so the share card
    # shows the timing that was caught, not only grey lane boxes; the canonical
    # JSON record is the same content-addressed record either way.
    outputs = _failure_render.render_all(
        record, timing=contract_info.get("timing"),
        timeline=contract_info.get("timeline"))
    if set(outputs) != set(_DEMO_RECORD_FILES):
        raise DemoSelectionError(
            "internal contract: the Failure Record renderer produced "
            f"{sorted(outputs)}, expected {list(_DEMO_RECORD_FILES)}")

    record_dir = os.path.join(out_dir, _DEMO_RECORD_DIR)
    os.makedirs(record_dir, exist_ok=True)
    for name in _DEMO_RECORD_FILES:
        _write_text(os.path.join(record_dir, name), outputs[name])

    return {
        "dir": _DEMO_RECORD_DIR,
        "record_id": record["record_id"],
        "headline": record["headline"],
        "privacy_profile": record["privacy"]["profile"],
        "files": list(_DEMO_RECORD_FILES),
    }


def _demo_record_verify_command() -> str:
    """The public one-command verifier for the share-safe record, pinned to the
    version that produced it (the same pin ``hotato record verify`` renders in
    the record's own footer)."""
    return (f"uvx --from hotato=={__version__} hotato record verify "
            f"{_DEMO_RECORD_DIR}/failure-record.json")


def _next_commands_text(event_wav_rel: Optional[str] = None) -> str:
    # One clear next step that RUNS AS PRINTED: the demo just wrote a scorable
    # two-channel recording (the contract's audio/event.wav), so `hotato
    # investigate` on THAT file is the next command -- no placeholder path, and
    # no recording of your own needed to see step two. Swapping in your own call
    # is the same command; the CI-kit scaffold is one line below. (promote/run/
    # card are what `hotato init starter` and the docs walk you through, so they
    # stay out of the first-run closing.)
    alt = ", ".join(_DEMO_STARTER_STACKS)
    invest = (f"hotato investigate {event_wav_rel}" if event_wav_rel
              else "hotato investigate path/to/your-call.wav")
    return "\n".join([
        "",
        "Your next step -- score a call now (the demo just wrote one you can "
        "score):",
        f"  {invest}",
        "  Run the same command on your own recording once you have one.",
        "",
        f"  Scaffold a CI gate for your repo:  {_DEMO_STARTER_PRIMARY}   "
        f"(--stack {alt} to tune)",
    ])


def _secs(x) -> str:
    """Format a seconds value for the human report: ``n/a`` for a missing
    measurement, never a raw ``None`` (report.py / capture.py do the same)."""
    return "n/a" if x is None else f"{x:.2f}s"


def run_start(*, demo: bool = False, stereo: Optional[str] = None,
              out_dir: Optional[str] = None, fmt: str = "text",
              label: Optional[str] = None, onset_sec: Optional[float] = None,
              caller_channel: int = 0, agent_channel: int = 1, confirm_channels: bool = False) -> int:
    """``hotato start``. Two guided first-run modes: ``--demo`` (the bundled,
    credential-less demo: the timing act first, then the say-do conversation
    check) and ``--stereo <call.wav>`` (your own dual-channel recording). To
    score a live provider stack or a folder of recordings, use ``hotato
    sweep`` / ``hotato analyze``."""
    modes = [m for m, on in (("--demo", demo), ("--stereo", stereo)) if on]
    if not modes:
        raise ValueError(
            "choose a mode: hotato start --demo (the guided, credential-less "
            "first run) or hotato start --stereo <call.wav> (the guided own-call "
            "review-and-contract flow). To score a live provider stack or a "
            "folder of recordings, use hotato sweep / hotato analyze."
        )
    if stereo:
        return _run_stereo_flow(
            stereo, out_dir=out_dir or ".", fmt=fmt,
            label=label, onset_sec=onset_sec,
            caller_channel=caller_channel, agent_channel=agent_channel,
            confirm_channels=confirm_channels)

    out_dir = out_dir or "."
    _ensure_out_dir(out_dir)

    aggregate = _sweep_demo(out_dir)

    # Render the hero card. "If the plan path works": the bundled demo always
    # funnels, but never let a card hiccup break the guided first run.
    card_written = False
    try:
        svg = _card.render_plan_card(_funnel_plan())
        _write_text(os.path.join(out_dir, _FUNNEL_CARD), svg)
        card_written = True
    except Exception:  # pragma: no cover - the demo plan is always the funnel
        card_written = False

    # Create and verify the demo failure contract. Defensive the same way the
    # card step is: the bundled demo audio always produces a scorable, always
    # -failing (by design) contract, but a hiccup here must never break the
    # rest of the guided first run.
    contract_info = None
    try:
        contract_info = _create_and_verify_demo_contract(
            out_dir, os.path.join(out_dir, _SWEEP_JSON))
    except DemoSelectionError:
        # Internal contract violated: the packaged sweep and the packaged
        # scenario declaration disagree. Fail loudly; a first run must never
        # quietly drop (or fake) the contract step.
        raise
    except Exception:  # pragma: no cover - defensive for non-contract hiccups
        contract_info = None
    contract_written = contract_info is not None
    # The 1-based sweep rank of the evidence-selected missed interruption, so
    # the golden path promotes THAT moment with --expect yield (never #1, the
    # backchannel). None only if the defensive contract step above hiccuped.
    candidate_rank = (contract_info["candidate_rank"]
                      if contract_written else None)

    # Project + render the canonical share-safe Failure Record from the SAME
    # verified evidence. Unlike the card/contract steps above, this is an
    # ESSENTIAL demo invariant: NO try/except swallows a selection, projection,
    # or render failure -- the whole point of the guided first run is that it
    # leaves one valid, evidence-specific, share-safe record without a second
    # command, so a broken record fails the run loudly instead of shipping a
    # first run with a missing or faked artifact.
    record_info = None
    if contract_written:
        record_info = _write_demo_failure_record(out_dir, contract_info)
    record_written = record_info is not None

    # Act two: the say-do check on the bundled scripted conversation, through
    # the real conversation-test machinery. ESSENTIAL like the record step: no
    # try/except swallows it -- the first run either shows a say-do catch its
    # evaluated evidence backs, or fails loudly (DemoSayDoError), never a
    # silently dropped or faked second act.
    saydo_info = _run_saydo_check(out_dir)

    written = ([_SWEEP_JSON, _SWEEP_HTML]
               + ([_FUNNEL_CARD] if card_written else [])
               + ([contract_info["bundle_rel"] + "/contract.json"]
                  if contract_written else [])
               + ([f"{_DEMO_RECORD_DIR}/{name}" for name in _DEMO_RECORD_FILES]
                  if record_written else [])
               + saydo_info["files"])
    sys.stderr.write(
        f"[start] demo: swept 2 bundled calls, {aggregate['total_candidates']} "
        f"candidate moments; ran 1 say-do conversation check; "
        f"wrote {', '.join(written)}\n")

    if fmt == "json":
        # Same evidence-selected rank as the text path: the promote/card refs
        # point at the missed interruption (#{candidate_rank}), never a
        # hardcoded #1 (the backchannel the agent yielded to).
        # The activation on-ramp leads, converging with the text closing
        # (_next_commands_text): score the scorable event.wav the demo just wrote
        # -- no recording of your own needed to reach step two -- before the
        # CI-gate scaffold, so `--format json` (agent) consumers get the same
        # first step a human sees. Same guard as the text path: only when the
        # demo wrote the contract bundle that holds audio/event.wav.
        event_wav_rel = (f"{contract_info['bundle_rel']}/audio/event.wav"
                         if contract_written else None)
        next_commands = (
            [f"hotato investigate {event_wav_rel}"] if event_wav_rel else []
        )
        next_commands.append(_DEMO_STARTER_PRIMARY)
        if candidate_rank is not None:
            next_commands.append(
                f"hotato fixture promote {_SWEEP_JSON}#{candidate_rank} "
                "--expect yield --id my-first-fixture --out tests/hotato")
        next_commands.append(
            "hotato run --scenarios tests/hotato/scenarios --audio "
            "tests/hotato/audio")
        if candidate_rank is not None:
            next_commands.append(
                f"hotato card {_SWEEP_JSON}#{candidate_rank} "
                "--out candidate.svg")
        payload = {
            "tool": "hotato", "kind": "start", "mode": "--demo", "ran": True,
            "offline": True, "written": written,
            "total_candidates": aggregate["total_candidates"],
            "next_commands": next_commands,
        }
        if contract_written:
            payload["next_commands"].append(
                f"hotato contract verify {_CONTRACTS_DIR}/")
            payload["contract"] = {
                "id": _DEMO_CONTRACT_ID,
                "dir": contract_info["bundle_rel"],
                "expect": "yield",
                "scorable": contract_info["scorable"],
                "passed": contract_info["passed"],
                "verified_fail_as_expected": contract_info["passed"] is False,
            }
            if contract_info.get("timing"):
                # The measured-timing lenses on the same clip (barge-in verdict
                # + the endpointing response gap the golden path otherwise
                # drops), for agent consumers -- a display read, not the machine
                # verify measurement.
                payload["contract"]["measured_timing"] = contract_info["timing"]
        if record_written:
            payload["next_commands"].append(_demo_record_verify_command())
            payload["failure_record"] = {
                "dir": record_info["dir"],
                "record_id": record_info["record_id"],
                "headline": record_info["headline"],
                "privacy_profile": record_info["privacy_profile"],
                "files": record_info["files"],
            }
        payload["next_commands"].append(_saydo_next_command())
        payload["next_commands"].append(_saydo_card_command())
        payload["saydo"] = {k: saydo_info[k] for k in (
            "dir", "test_id", "agent", "exit_code", "passed",
            "verified_fail_as_expected", "claim_assertion",
            "evidence_assertions", "files")}
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        measurement = (contract_info.get("timing") or {}) if contract_written \
            else {}
        print("hotato start: swept the 2 bundled demo calls offline.")
        print(f"  sweep result:    {_SWEEP_JSON}")
        print(f"  sweep dashboard: {_SWEEP_HTML}")
        if card_written:
            print(f"  funnel card:     {_FUNNEL_CARD}")
        if contract_written:
            print(f"  demo contract:   {contract_info['bundle_rel']}")
        # The CATCH first (the hit above the hedging), visually isolated: the
        # one quotable headline between two rules, one plain-English sentence,
        # and the two DISTINCT measured signals on the same clip (talk-over is
        # not the response gap), with the share pointers beneath it.
        if record_written:
            md_path = f"{_DEMO_RECORD_DIR}/failure-record.md"
            svg_path = f"{_DEMO_RECORD_DIR}/failure-record.svg"
            to = measurement.get("talk_over_sec")
            rg = measurement.get("response_gap_sec")
            ps = measurement.get("premature_start_sec")
            rule = "  " + "-" * 66
            print("")
            print(rule)
            print(record_info["headline"])
            print(rule)
            if to is not None:
                print("  In plain terms: after the caller took the floor, the "
                      "agent kept talking")
                print(f"  over them for {_secs(to)} instead of yielding.")
            print("  Two measured signals on this one call:")
            print(f"    talk-over     {_secs(to)}  seconds the agent kept "
                  "talking while the caller held the floor")
            if rg is not None:
                print(f"    response gap  {_secs(rg)}  seconds of dead air from "
                      "the caller's turn end to the reply")
            elif ps is not None and ps > 0:
                print(f"    premature start {_secs(ps)}  seconds the agent's "
                      "reply led the caller's turn end")
            print("")
            print(f"  Share in a PR:      {md_path}")
            print(f"  Share as an image:  {svg_path}")
            print(f"  Verify the record:  {_demo_record_verify_command()}")
        # The provenance + gate caveats, now BELOW the hit: what the frozen
        # contract does and does not prove, and the exit-0 explanation.
        if contract_written:
            if contract_info["passed"] is False:
                print("")
                print("  re-scored FAIL, by design: this call talked over "
                      "the caller.")
                print("  The contract freezes that moment as evidence, so a "
                      "CI gate flags any")
                print("  later change to its evidence or policy. Proving the "
                      "live agent")
                print("  improved uses a fresh recapture (docs/RECAPTURE.md).")
                print("  Setup finished, so start --demo exits 0. See the "
                      "gate return exit 1:")
                print("      hotato contract verify contracts/")
            else:  # pragma: no cover - the bundled demo candidate always fails
                mark = "PASS" if contract_info["passed"] else "NOT SCORABLE"
                print(f"  verified contract: {mark}")
        # Act two: the say-do check. Every line below is backed by the
        # evaluated results (_run_saydo_check raised unless the claim PASSed
        # and the tool + state evidence FAILed).
        print("")
        print("Act two: the say-do check, on the bundled scripted conversation")
        print("(same rules: offline, no account, no credential).")
        print(f"  conversation:    {_SAYDO_DIR}/ (transcript + tool trace + "
              "post-call state + test)")
        print(f"  test result:     {_SAYDO_DIR}/{_SAYDO_RESULT}")
        print("  say-do check:    FAIL, by design: the agent said the refund "
              "was sent;")
        print("                   the trace shows no such tool call succeeded "
              "(no")
        print("                   issue_refund span), and the order's "
              "post-call")
        print("                   refund_status stayed \"none\".")
        print("  Act one measured how the call sounded; this act checks what "
              "the")
        print("  agent did. Tool and state evidence decide the outcome, never "
              "the")
        print("  agent's words.")
        print("  Setup finished, so start --demo still exits 0. See this gate "
              "return")
        print("  exit 1:")
        print(f"      {_saydo_next_command()}")
        event_wav_rel = (f"{contract_info['bundle_rel']}/audio/event.wav"
                         if contract_written else None)
        print(_next_commands_text(event_wav_rel))
        # One quiet footer on the successful demo path only (never on errors or
        # other subcommands): a star is how the next team finds this.
        print("")
        print("Useful? A star helps other teams find it:  "
              "github.com/attenlabs/hotato")
    return 0


# --- start --stereo: the guided own-call first run --------------------------

_STEREO_REVIEW_HTML = "hotato-review.html"
_STEREO_CARD = "hotato-candidate.svg"
_STEREO_CONTRACTS = "contracts"


def _run_stereo_flow(stereo, *, out_dir, fmt, label, onset_sec,
                     caller_channel, agent_channel, confirm_channels=False):
    """The guided own-call flow: trust preflight -> channel-mapping check ->
    candidate scan -> local review page -> (human label) -> contract + a result
    card, ending with one sentence on what the result proves and one on what it
    does not. No credential, no network.

    Without ``--label`` it stops at the review page and prints the exact command
    to finish; with ``--label yield|hold`` (a HUMAN decision) it creates the
    contract and writes its result card. A single recording is MEASURED evidence,
    never a paired fresh-recapture proof -- the flow says so explicitly."""
    from . import contract as _contract
    from . import evidence as _evidence
    from . import ingest as _ingest
    from . import scan as _scan
    from . import trust as _trust
    from .core import run_single

    _ensure_out_dir(out_dir)

    # 1) trust preflight
    report = _trust.trust_report(stereo, caller_channel=caller_channel,
                                 agent_channel=agent_channel)
    input_health = report.get("input_health") or (
        "clean" if report.get("scorable") else "not_scorable")
    if not report.get("scorable"):
        msg = f"NOT SCORABLE: {report.get('recommendation')}"
        _emit_stereo(fmt, {"tool": "hotato", "kind": "start", "mode": "--stereo",
                           "ran": False, "scorable": False,
                           "input_health": input_health,
                           "recommendation": report.get("recommendation")}, msg)
        return 2
    channels = report.get("channels") or {}
    possible_swap = bool(channels.get("possible_swap"))

    # 2) candidate scan + review page
    scanned = _scan.scan_recording(stereo, caller_channel=caller_channel,
                                   agent_channel=agent_channel)
    cands = scanned.get("candidates", [])
    review_html = _ingest.render_candidates_html(scanned, top=10)
    _write_text(os.path.join(out_dir, _STEREO_REVIEW_HTML), review_html)

    top = cands[0] if cands else None
    top_onset = onset_sec if onset_sec is not None else (
        top.get("t_sec") if top else None)

    # 3) measure the top candidate. WITHOUT a human label a candidate is a timing
    # MOMENT for review, never a yield/hold verdict: emit only the raw timing
    # (onset frame, decision margin, boundary sensitivity) and add a verdict ONLY
    # once a human supplies --label yield|hold. `expect` is needed to run the
    # scorer, but its verdict is withheld from the output until then.
    measured = None
    if top_onset is not None:
        labeled = label in ("yield", "hold")
        env = run_single(stereo=stereo, onset_sec=top_onset,
                         expect=(label if labeled else "yield"),
                         caller_channel=caller_channel, agent_channel=agent_channel)
        ev = env["events"][0]
        measured = {"onset_sec": top_onset,
                    "measurements": {k: ev["measurements"].get(k) for k in (
                        "onset_frame_index", "onset_effective_sec",
                        "decision_margin_sec", "decision_margin_hops",
                        "boundary_sensitive", "hop_sec")}}
        if labeled:
            measured["verdict"] = ev["verdict"]
        else:
            measured["needs_label"] = (
                "a candidate is a timing moment for review, not a verdict; "
                "label it with --label yield|hold to score it")

    # 4) optional human label -> contract + evidence-tier card
    contract_info = None
    card_written = False
    swap_blocked = bool(possible_swap and not confirm_channels and label in ("yield", "hold"))
    if label in ("yield", "hold") and top_onset is not None and not swap_blocked:
        cdir = os.path.join(out_dir, _STEREO_CONTRACTS)
        os.makedirs(cdir, exist_ok=True)
        res = _contract.create_contract(
            stereo=stereo, expect=label, out_dir=cdir, onset_sec=top_onset,
            contract_id="own-call-001", caller_channel=caller_channel,
            agent_channel=agent_channel,
            max_time_to_yield_sec=None, max_talk_over_sec=None,
            # K6: forward the SAME confirmation that already unblocked us above
            # (or the honest False when there was no swap to confirm), so
            # create_contract's own channel-mapping gate never re-refuses a
            # verdict this flow already got a human confirmation for.
            confirm_channels=confirm_channels)
        # Forward-slash the stored contract "dir" so the emitted artifact is
        # portable: os.sep is "/" on POSIX (no-op) but "\\" on Windows.
        contract_info = {"id": "own-call-001", "dir": os.path.relpath(
            res.get("dir", cdir), out_dir).replace(os.sep, "/")}
        # evidence tier for ONE measured recording (never paired here)
        vector = {"input_health": input_health,
                  "channel_mapping": "suspect" if possible_swap else "confirmed",
                  "label_authority": "human",
                  "score_integrity": "recomputed", "audio_identity": "recomputed"}
        classification = _evidence.classify(
            vector, required=_evidence.REQUIRED_FOR_MEASURED)
        # A single own-call recording is MEASURED evidence at most: there is no
        # before/after pairing or experiment here, so it can never read as a
        # paired/attested proof. Cap the tier and use measured-tier language.
        capped = min(classification["tier"], _evidence.TIER_MEASURED)
        classification["tier"] = capped
        classification["headline"] = _evidence.headline_for(capped, vector)
        contract_info["evidence_tier"] = classification["tier"]
        contract_info["evidence_headline"] = classification["headline"]
        # Render the result card for this labelled own-call and write it next to
        # the review page: the MEASURED-tier artifact the flow promises. Uses
        # card.py's public contract-card path on the freshly-written bundle.
        # Defensive like the demo flow -- a card hiccup must not sink the run.
        try:
            svg = _card.make_card(res["paths"]["contract"])
            _write_text(os.path.join(out_dir, _STEREO_CARD), svg)
            card_written = True
            contract_info["card"] = _STEREO_CARD
        except Exception:  # pragma: no cover - the own-call contract always renders
            card_written = False

    # 5) assemble output
    if label in ("yield", "hold"):
        proves = ("This measures whether THIS recording met the yield/hold policy "
                  "you labelled, under the recorded conditions.")
    else:
        proves = ("This surfaces candidate timing MOMENTS (onset, overlap, boundary "
                  "sensitivity) for you to review. It assigns no yield/hold verdict "
                  "until you label a moment.")
    not_proves = ("It does not prove the call was fresh, that any change caused "
                  "the result, or that future calls will pass. A paired "
                  "fresh-recapture proof needs a before/after trial "
                  "(hotato fix trial) with recomputed audio.")
    payload = {
        "tool": "hotato", "kind": "start", "mode": "--stereo", "ran": True,
        "offline": True, "scorable": True, "input_health": input_health,
        "possible_channel_swap": possible_swap,
        "recommendation": report.get("recommendation"),
        "total_candidates": scanned.get("total_candidates"),
        "review_page": _STEREO_REVIEW_HTML,
        "card": _STEREO_CARD if card_written else None,
        "top_candidate": measured,
        "contract": contract_info,
        "proves": proves, "does_not_prove": not_proves,
        "next": _stereo_next(label, contract_info),
    }
    if fmt == "json":
        print(_errors.safe_json_dumps(payload, indent=2))
        return 0
    # text
    lines = ["hotato start --stereo: guided own-call review (offline).",
             f"  input health:    {input_health}"
             + ("  [!] possible channel swap -- confirm mapping" if possible_swap else ""),
             f"  recommendation:  {report.get('recommendation')}",
             f"  candidates:      {scanned.get('total_candidates')} moments -> {_STEREO_REVIEW_HTML}"]
    if measured:
        m = measured["measurements"]
        bs = " [BOUNDARY-SENSITIVE]" if m.get("boundary_sensitive") else ""
        if "verdict" in measured:
            v = measured["verdict"]
            lines += [f"  top candidate:   onset {measured['onset_sec']:.2f}s "
                      f"(frame {m.get('onset_frame_index')}), "
                      f"time-to-yield {_secs(v.get('seconds_to_yield'))}, "
                      f"talk-over {_secs(v.get('talk_over_sec'))}{bs}"]
        else:
            dm = m.get("decision_margin_sec")
            lines += [f"  top candidate:   onset {measured['onset_sec']:.2f}s "
                      f"(frame {m.get('onset_frame_index')}), decision margin "
                      f"{_secs(dm)}{bs} -- unlabeled (a timing moment, not a verdict)"]
    if contract_info:
        lines += [f"  contract:        {contract_info['dir']} "
                  f"(evidence: {contract_info['evidence_headline']}, tier "
                  f"{contract_info['evidence_tier']})"]
    if card_written:
        lines += [f"  card:            {_STEREO_CARD}"]
    lines += ["", f"  What this proves:     {proves}",
              f"  What it does NOT:     {not_proves}", ""]
    lines += _stereo_next(label, contract_info)
    print("\n".join(lines))
    return 0


def _stereo_next(label, contract_info):
    if label not in ("yield", "hold"):
        return [
            "Next: review the candidates in the page above, then label the real "
            "one to create a contract:",
            "  hotato start --stereo <call.wav> --label yield   # or --label hold",
            "  (add --onset <sec> to pin a specific moment)",
        ]
    return [
        "Next: connect your stack and recapture to build a paired proof:",
        "  hotato connect vapi --api-key <key>",
        "  hotato fix trial <patch.json> --before <before/> --after <after/> --battery <before/>",
    ]


def _emit_stereo(fmt, payload, text_msg):
    if fmt == "json":
        print(_errors.safe_json_dumps(payload, indent=2))
    else:
        print(text_msg)
