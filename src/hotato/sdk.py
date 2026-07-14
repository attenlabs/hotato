"""hotato.sdk: a typed Python facade over the same code paths the CLI runs.

Every function here calls the internal function that backs the corresponding
``hotato`` command (``core.run_suite``/``run_single``, ``contract.verify_contracts``,
``investigate.run_investigate``, ``counterexample.compile_counterexample`` /
``verify_counterexample``, ``transcribe.transcribe_cached``). No subprocess is
spawned. The frozen dataclasses returned here mirror the JSON those commands
already emit with ``--format json``: the field names are the JSON keys, so a
result parsed from the SDK and a result parsed from the CLI carry the same data
under the same names. The JSON schemas are the stable contract; this module adds
type hints over them and invents no metric of its own.

Scoring stays deterministic and offline on the default energy backend. A
NOT-SCORABLE or INCONCLUSIVE event passes through with its own reason intact;
nothing is blended into a single number. Bad input raises a typed exception from
the shared error contract (``ValueError`` and its subclasses for input,
``BackendUnavailable`` for a missing optional extra, ``FileNotFoundError`` /
``OSError`` for files, ``CounterexampleRefusal`` for a counterexample refusal),
never a bare ``Exception``.

Example:
    from hotato.sdk import run_suite

    result = run_suite()          # the bundled labelled battery, zero files
    print(result.passed, result.failed)
    for event in result.events:
        print(event.event_id, event.passed, event.seconds_to_yield)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple, Union

from . import contract as _contract
from . import core as _core
from . import investigate as _investigate
from ._engine.vad import BackendUnavailable
from .counterexample import compile_counterexample as _compile_counterexample
from .counterexample import verify_counterexample as _verify_counterexample
from .counterexample.model import DEFAULT_BUDGET as _DEFAULT_BUDGET
from .counterexample.model import CounterexampleRefusal
from .errors import HANDLED, ChannelRangeError
from .transcribe import (
    CachedTranscribeResult,
    Transcript,
    TranscriptCache,
    TranscriptSegment,
    build_transcript_cache,
    default_transcript_cache_dir,
)
from .transcribe import transcribe_cached as _transcribe_cached

PathArg = Union[str, Path]

__all__ = [
    # scoring (hotato run)
    "run_suite",
    "run_single",
    "SuiteResult",
    "Summary",
    "Event",
    "Verdict",
    # contracts (hotato contract verify)
    "verify_contracts",
    "ContractVerifyResult",
    "ContractResult",
    # investigate (hotato investigate)
    "investigate",
    "InvestigateResult",
    # counterexamples (hotato counterexample compile / verify)
    "compile_counterexample",
    "verify_counterexample",
    "CounterexampleResult",
    "CounterexampleVerifyResult",
    # transcription (hotato run --transcribe)
    "transcribe",
    "Transcript",
    "TranscriptSegment",
    "TranscriptCache",
    "CachedTranscribeResult",
    "build_transcript_cache",
    "default_transcript_cache_dir",
    "transcribe_cached",
    # error types (all inside the shared HANDLED contract)
    "BackendUnavailable",
    "ChannelRangeError",
    "CounterexampleRefusal",
    "HANDLED",
]


def _as_path(value: PathArg) -> str:
    """Return a filesystem path as ``str``, accepting ``str`` or ``pathlib.Path``."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError(
        f"expected a str or pathlib.Path, got {type(value).__name__}"
    )


def _opt_path(value: Optional[PathArg]) -> Optional[str]:
    """Like :func:`_as_path`, but ``None`` passes through unchanged."""
    return None if value is None else _as_path(value)


# ---------------------------------------------------------------------------
# hotato run -- the turn-taking (barge-in) envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One event's timing verdict, mirroring ``event["verdict"]`` in the JSON.

    ``did_yield`` / ``seconds_to_yield`` / ``talk_over_sec`` are ``None`` on a
    not-scorable event, where there is no recording to measure.
    """

    passed: bool
    did_yield: Optional[bool]
    seconds_to_yield: Optional[float]
    talk_over_sec: Optional[float]
    reasons: Tuple[str, ...] = ()

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Verdict":
        return cls(
            passed=bool(d["passed"]),
            did_yield=d.get("did_yield"),
            seconds_to_yield=d.get("seconds_to_yield"),
            talk_over_sec=d.get("talk_over_sec"),
            reasons=tuple(d.get("reasons", ())),
        )


@dataclass(frozen=True)
class Event:
    """One scored recording, mirroring an entry of the envelope ``events`` list.

    The timing fields live under :attr:`verdict`; the ``passed`` / ``did_yield``
    / ``seconds_to_yield`` / ``talk_over_sec`` properties read them directly for
    convenience. ``scorable`` is ``True`` unless the JSON marks the event
    not-scorable, in which case :attr:`not_scorable_reason` carries the reason.
    """

    event_id: str
    verdict: Verdict
    scenario_id: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    expected_yield: Optional[bool] = None
    scorable: bool = True
    not_scorable_reason: Optional[str] = None
    measurements: Mapping[str, Any] = field(default_factory=dict)
    signals: Mapping[str, Any] = field(default_factory=dict)
    fix: Optional[Mapping[str, Any]] = None

    @property
    def passed(self) -> bool:
        return self.verdict.passed

    @property
    def did_yield(self) -> Optional[bool]:
        return self.verdict.did_yield

    @property
    def seconds_to_yield(self) -> Optional[float]:
        return self.verdict.seconds_to_yield

    @property
    def talk_over_sec(self) -> Optional[float]:
        return self.verdict.talk_over_sec

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Event":
        return cls(
            event_id=d["event_id"],
            verdict=Verdict.from_json(d["verdict"]),
            scenario_id=d.get("scenario_id"),
            title=d.get("title"),
            category=d.get("category"),
            expected_yield=d.get("expected_yield"),
            scorable=bool(d.get("scorable", True)),
            not_scorable_reason=d.get("not_scorable_reason"),
            measurements=dict(d.get("measurements", {})),
            signals=dict(d.get("signals", {})),
            fix=(dict(d["fix"]) if d.get("fix") is not None else None),
        )


@dataclass(frozen=True)
class Summary:
    """The envelope ``summary`` block. ``passed`` and ``failed`` are counts.

    ``not_scorable`` is present only when at least one event was not scorable.
    """

    events: int
    passed: int
    failed: int
    regression: bool
    not_scorable: Optional[int] = None

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Summary":
        return cls(
            events=int(d["events"]),
            passed=int(d["passed"]),
            failed=int(d["failed"]),
            regression=bool(d["regression"]),
            not_scorable=d.get("not_scorable"),
        )


@dataclass(frozen=True)
class SuiteResult:
    """The full ``hotato run`` envelope, one field per top-level JSON key.

    :attr:`passed` is the process-level pass (exit code 0), and :attr:`failed`
    is the count of failed scorable events, so ``run_suite`` reads as a boolean
    pass plus a failure count without re-deriving the exit code.
    """

    tool: str
    schema_version: str
    mode: str
    stack: str
    offline: bool
    engine: Mapping[str, Any]
    limits: Mapping[str, Any]
    summary: Summary
    events: Tuple[Event, ...]
    fix_map: Tuple[Mapping[str, Any], ...]
    funnel: Optional[Mapping[str, Any]]
    exit_code: int

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def failed(self) -> int:
        return self.summary.failed

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "SuiteResult":
        return cls(
            tool=d["tool"],
            schema_version=d["schema_version"],
            mode=d["mode"],
            stack=d["stack"],
            offline=bool(d["offline"]),
            engine=dict(d.get("engine", {})),
            limits=dict(d.get("limits", {})),
            summary=Summary.from_json(d["summary"]),
            events=tuple(Event.from_json(e) for e in d.get("events", ())),
            fix_map=tuple(dict(f) for f in d.get("fix_map", ())),
            funnel=(dict(d["funnel"]) if d.get("funnel") is not None else None),
            exit_code=int(d["exit_code"]),
        )


def run_suite(
    scenarios: Optional[PathArg] = None,
    audio: Optional[PathArg] = None,
    *,
    suite: str = _core.SUITE_ID,
    stack: Optional[str] = None,
    suffix: str = ".example.wav",
    caller_channel: int = 0,
    agent_channel: int = 1,
    echo_gate: bool = False,
) -> SuiteResult:
    """Run a labelled turn-taking battery, mirroring ``hotato run --format json``.

    Omit ``scenarios`` and ``audio`` to run the bundled 8-scenario ``barge-in``
    battery that ships in the package, which needs no files on disk. Pass a
    ``scenarios`` directory and its matching ``audio`` directory together to run
    your own labelled set. The result carries every scored event and the
    process-level :attr:`SuiteResult.passed` / :attr:`SuiteResult.failed`.

    Raises ``ValueError`` for a bad scenario pack or an audio path outside the
    audio directory, ``FileNotFoundError`` / ``OSError`` for a missing file, and
    ``BackendUnavailable`` when an optional backend is requested without its extra.

    Example:
        from hotato.sdk import run_suite

        result = run_suite()
        assert result.passed and result.failed == 0
        print(result.summary.events, "events scored")
    """
    if (scenarios is None) != (audio is None):
        raise ValueError(
            "pass both `scenarios` and `audio` for a custom set, "
            "or neither to run the bundled battery"
        )
    env = _core.run_suite(
        suite=suite,
        stack=stack,
        scenarios_dir=_opt_path(scenarios),
        audio_dir=_opt_path(audio),
        suffix=suffix,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        echo_gate=echo_gate,
    )
    return SuiteResult.from_json(env)


def run_single(
    stereo: Optional[PathArg] = None,
    *,
    caller: Optional[PathArg] = None,
    agent: Optional[PathArg] = None,
    mono: Optional[PathArg] = None,
    caller_channel: int = 0,
    agent_channel: int = 1,
    onset_sec: Optional[float] = None,
    expect: str = "yield",
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    echo_gate: bool = False,
) -> SuiteResult:
    """Score one recording, mirroring single-file ``hotato run --format json``.

    Pass a two-channel recording as ``stereo``, or a ``caller`` / ``agent`` pair
    of mono files. The return type is the same envelope as :func:`run_suite`
    with ``mode`` set to ``"single"``.

    Raises ``ValueError`` for conflicting inputs or a mono file passed as stereo,
    and ``FileNotFoundError`` / ``OSError`` for a missing recording.

    Example:
        from importlib import resources
        from hotato.sdk import run_single

        wav = str(resources.files("hotato").joinpath(
            "data", "audio", "01-hard-interruption.example.wav"))
        result = run_single(stereo=wav, expect="yield", onset_sec=0.3)
        print(result.events[0].seconds_to_yield)
    """
    env = _core.run_single(
        stereo=_opt_path(stereo),
        caller=_opt_path(caller),
        agent=_opt_path(agent),
        mono=_opt_path(mono),
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        onset_sec=onset_sec,
        expect=expect,
        stack=stack,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
        echo_gate=echo_gate,
    )
    return SuiteResult.from_json(env)


# ---------------------------------------------------------------------------
# hotato contract verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractResult:
    """One contract's verify result, mirroring an entry of ``results``.

    :attr:`authenticity` is the attestation string exactly as the JSON reports
    it: one of ``authenticated``, ``unsigned``, ``unattested``, ``unverified``,
    or ``tampered``. It is passed through with no relabeling.
    """

    id: str
    dir: str
    expect: str
    passed: bool
    scorable: bool
    verdict_eligible: bool
    authenticity: str
    authenticated: bool
    verdict_ineligible_reason: Optional[str] = None
    not_scorable_reason: Optional[str] = None
    measurement: Mapping[str, Any] = field(default_factory=dict)
    authenticity_reason: Optional[str] = None
    assertions: Optional[Mapping[str, Any]] = None

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "ContractResult":
        return cls(
            id=d["id"],
            dir=d["dir"],
            expect=d["expect"],
            passed=bool(d["passed"]),
            scorable=bool(d["scorable"]),
            verdict_eligible=bool(d["verdict_eligible"]),
            authenticity=d["authenticity"],
            authenticated=bool(d["authenticated"]),
            verdict_ineligible_reason=d.get("verdict_ineligible_reason"),
            not_scorable_reason=d.get("not_scorable_reason"),
            measurement=dict(d.get("measurement", {})),
            authenticity_reason=d.get("authenticity_reason"),
            assertions=(
                dict(d["assertions"]) if d.get("assertions") is not None else None
            ),
        )


@dataclass(frozen=True)
class ContractVerifyResult:
    """The ``hotato contract verify`` batch envelope, one field per JSON key.

    :attr:`passed` is the batch pass (exit code 0). The batch fails when any
    contract regresses, is tampered, or has a failing embedded assertion.
    """

    tool: str
    kind: str
    schema_version: str
    offline: bool
    dir: str
    count: int
    results: Tuple[ContractResult, ...]
    summary: Mapping[str, Any]
    tampered: int
    refused: int
    assertions_failed: int
    exit_code: int

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "ContractVerifyResult":
        return cls(
            tool=d["tool"],
            kind=d["kind"],
            schema_version=d["schema_version"],
            offline=bool(d["offline"]),
            dir=d["dir"],
            count=int(d["count"]),
            results=tuple(ContractResult.from_json(r) for r in d.get("results", ())),
            summary=dict(d["summary"]),
            tampered=int(d["tampered"]),
            refused=int(d["refused"]),
            assertions_failed=int(d["assertions_failed"]),
            exit_code=int(d["exit_code"]),
        )


def verify_contracts(
    path: PathArg,
    *,
    transcript_path: Optional[PathArg] = None,
) -> ContractVerifyResult:
    """Verify one contract bundle or a directory of them.

    ``path`` is a single ``<id>.hotato`` bundle directory or a parent directory
    holding several. ``transcript_path`` supplies a transcript for any embedded
    assertions. A regressed or tampered contract surfaces as
    :attr:`ContractVerifyResult.passed` ``False`` with ``exit_code`` 1; it does
    not raise. Bad input (a missing directory, a corrupt bundle) raises
    ``ValueError``.

    Example:
        from hotato.sdk import verify_contracts

        result = verify_contracts("contracts/")
        print(result.passed, result.summary["failed"])
        for c in result.results:
            print(c.id, c.passed, c.authenticity)
    """
    v = _contract.verify_contracts(
        _as_path(path), transcript_path=_opt_path(transcript_path)
    )
    return ContractVerifyResult.from_json(v)


# ---------------------------------------------------------------------------
# hotato investigate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvestigateResult:
    """The ``hotato investigate`` result, one field per top-level JSON key.

    :attr:`candidates` are the ranked candidate moments; :attr:`eligible` reads
    ``verdict_status["eligible"]``; :attr:`passed` is the process pass.
    """

    tool: str
    kind: str
    schema_version: str
    state_path: Optional[str]
    run: int
    source: str
    capture_origin: Mapping[str, Any]
    trust: Mapping[str, Any]
    verdict_status: Mapping[str, Any]
    note: Optional[str]
    total_candidates: int
    shown: int
    candidates: Tuple[Mapping[str, Any], ...]
    next: Tuple[Mapping[str, Any], ...]
    exit_code: int

    @property
    def eligible(self) -> bool:
        return bool(self.verdict_status.get("eligible"))

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "InvestigateResult":
        return cls(
            tool=d["tool"],
            kind=d["kind"],
            schema_version=d["schema_version"],
            state_path=d.get("state_path"),
            run=int(d["run"]),
            source=d["source"],
            capture_origin=dict(d.get("capture_origin", {})),
            trust=dict(d.get("trust", {})),
            verdict_status=dict(d.get("verdict_status", {})),
            note=d.get("note"),
            total_candidates=int(d["total_candidates"]),
            shown=int(d["shown"]),
            candidates=tuple(dict(c) for c in d.get("candidates", ())),
            next=tuple(dict(n) for n in d.get("next", ())),
            exit_code=int(d["exit_code"]),
        )


def investigate(
    source: Optional[PathArg] = None,
    *,
    stack: Optional[str] = None,
    call_id: Optional[str] = None,
    api_key: Optional[str] = None,
    account_sid: Optional[str] = None,
    auth_token: Optional[str] = None,
    model_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    base_url: Optional[str] = None,
    allow_mono: bool = False,
    caller_channel: int = 0,
    agent_channel: int = 1,
    min_gap: float = 2.0,
    top: int = 10,
    state_path: Optional[PathArg] = None,
    channel_map_confirmed: bool = False,
) -> InvestigateResult:
    """Scan one recording for candidate turn-taking moments, ranked by salience.

    Pass a local recording as ``source`` to scan it offline. The ``stack`` /
    ``call_id`` pair (with credentials) instead pulls one call from a provider
    before scanning. ``state_path`` is where the run state is written for the
    follow-up ``investigate label`` step. Bad usage or a missing file raises
    ``ValueError``.

    Example:
        from hotato.sdk import investigate

        result = investigate("call.wav", state_path="/tmp/state.json")
        print(result.total_candidates, result.eligible)
        for moment in result.candidates:
            print(moment["t_sec"], moment["kind"])
    """
    result, _exit_code = _investigate.run_investigate(
        _opt_path(source),
        stack=stack,
        call_id=call_id,
        api_key=api_key,
        account_sid=account_sid,
        auth_token=auth_token,
        model_id=model_id,
        agent_id=agent_id,
        base_url=base_url,
        allow_mono=allow_mono,
        caller_channel=caller_channel,
        agent_channel=agent_channel,
        min_gap=min_gap,
        top=top,
        state_path=_opt_path(state_path),
        channel_map_confirmed=channel_map_confirmed,
    )
    return InvestigateResult.from_json(result)


# ---------------------------------------------------------------------------
# hotato counterexample compile / verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CounterexampleResult:
    """A ``hotato counterexample compile`` result, one field per JSON key.

    :attr:`passed` is ``True`` when the reduced repro is one-minimal (exit code
    0); a budget-exhausted reduction is still a valid repro at exit code 1.
    """

    kind: str
    exit_code: int
    counterexample_id: str
    target: Mapping[str, Any]
    minimality: str
    reduction: Mapping[str, Any]
    output: str
    reproduce: str
    predicate: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "CounterexampleResult":
        return cls(
            kind=d["kind"],
            exit_code=int(d["exit_code"]),
            counterexample_id=d["counterexample_id"],
            target=dict(d["target"]),
            minimality=d["minimality"],
            reduction=dict(d["reduction"]),
            output=d["output"],
            reproduce=d["reproduce"],
            predicate=d["predicate"],
        )


@dataclass(frozen=True)
class CounterexampleVerifyResult:
    """A ``hotato counterexample verify`` result, one field per JSON key.

    The keys after :attr:`counterexample_id` are populated on the ``verified``
    status and stay ``None`` on a negative status. :attr:`passed` reads
    :attr:`ok`.
    """

    kind: str
    exit_code: int
    ok: bool
    status: str
    counterexample_id: str
    failure_fingerprint: Optional[str] = None
    minimality: Optional[str] = None
    single_unit_checks: Optional[Any] = None
    source_replays: Optional[int] = None
    final_replays: Optional[int] = None
    accepted_steps_replayed: Optional[int] = None
    evaluator_match: Optional[bool] = None
    output: Optional[str] = None
    preserved_deletions: Optional[Tuple[Any, ...]] = None

    @property
    def passed(self) -> bool:
        return bool(self.ok) and self.exit_code == 0

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "CounterexampleVerifyResult":
        preserved = d.get("preserved_deletions")
        return cls(
            kind=d["kind"],
            exit_code=int(d["exit_code"]),
            ok=bool(d["ok"]),
            status=d["status"],
            counterexample_id=d["counterexample_id"],
            failure_fingerprint=d.get("failure_fingerprint"),
            minimality=d.get("minimality"),
            single_unit_checks=d.get("single_unit_checks"),
            source_replays=d.get("source_replays"),
            final_replays=d.get("final_replays"),
            accepted_steps_replayed=d.get("accepted_steps_replayed"),
            evaluator_match=d.get("evaluator_match"),
            output=d.get("output"),
            preserved_deletions=(
                tuple(preserved) if preserved is not None else None
            ),
        )


def compile_counterexample(
    scenario: PathArg,
    test: PathArg,
    *,
    target: str,
    out: PathArg,
    workspace: Optional[PathArg] = None,
    budget: int = _DEFAULT_BUDGET,
    seed: Optional[int] = None,
) -> CounterexampleResult:
    """Reduce one deterministic failure to a minimal ``.hotato-repro`` bundle.

    ``scenario`` and ``test`` are the input files, ``target`` is the id of the
    deterministic assertion to preserve, and ``out`` is the output directory to
    create. ``workspace`` bounds where inputs may resolve. The compiler runs
    offline with no model, network, or subprocess. A bad target, a non-empty
    ``out`` directory, or an oversized input raises ``CounterexampleRefusal``,
    which carries a stable ``.code``.

    Example:
        from hotato.sdk import compile_counterexample

        result = compile_counterexample(
            "case.scenario.json", "case.test.json",
            target="pii-email", out="case.hotato-repro")
        print(result.minimality, result.output)
    """
    result = _compile_counterexample(
        _as_path(scenario),
        _as_path(test),
        target=target,
        out_dir=_as_path(out),
        workspace=_opt_path(workspace),
        budget=budget,
        seed=seed,
    )
    return CounterexampleResult.from_json(result)


def verify_counterexample(path: PathArg) -> CounterexampleVerifyResult:
    """Independently re-verify a ``.hotato-repro`` bundle.

    Re-checks bundle integrity, the exact failure identity, the replays, and
    minimality. A negative outcome returns with :attr:`CounterexampleVerifyResult.ok`
    ``False`` and ``exit_code`` 1. Version or evaluator drift, or a tampered
    bundle, raises ``CounterexampleRefusal``.

    Example:
        from hotato.sdk import verify_counterexample

        result = verify_counterexample("case.hotato-repro")
        print(result.status, result.passed)
    """
    result = _verify_counterexample(_as_path(path))
    return CounterexampleVerifyResult.from_json(result)


# ---------------------------------------------------------------------------
# hotato run --transcribe (optional [transcribe] extra)
# ---------------------------------------------------------------------------


def transcribe(
    path: PathArg,
    *,
    model: str = "base.en",
    device: str = "auto",
    compute_type: Optional[str] = None,
    word_timestamps: bool = False,
    vad_filter: bool = False,
    language: Optional[str] = None,
    cache: Optional[TranscriptCache] = None,
    no_cache: bool = False,
) -> Transcript:
    """Transcribe one recording through the content-addressed cache.

    A transcript is optional context beside the timing score; it never changes
    ``did_yield`` / ``seconds_to_yield`` / ``talk_over_sec``. Pass a
    :class:`~hotato.transcribe.TranscriptCache` to replay a stored transcript on
    a cache hit and skip the model. Transcription needs the optional
    ``hotato[transcribe]`` extra; without it this raises ``BackendUnavailable``,
    and a non-regular file raises ``ValueError``. For the cache-hit flag and the
    cache key, call :func:`transcribe_cached` and read its
    :class:`~hotato.transcribe.CachedTranscribeResult`.

    Example:
        from hotato.sdk import transcribe, build_transcript_cache

        cache, _warning = build_transcript_cache()
        transcript = transcribe("call.wav", cache=cache)
        print(transcript.text)
    """
    result = _transcribe_cached(
        path,
        model=model,
        device=device,
        compute_type=compute_type,
        word_timestamps=word_timestamps,
        vad_filter=vad_filter,
        language=language,
        cache=cache,
        no_cache=no_cache,
    )
    return result.transcript


# The cache-aware entry point, re-exported so callers that need the cache-hit
# flag, the cache key, or the drift report can read the full
# CachedTranscribeResult rather than only its .transcript.
transcribe_cached = _transcribe_cached
