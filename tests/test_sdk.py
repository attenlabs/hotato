"""Tests for hotato.sdk: the typed facade over the CLI's JSON contract.

Every test runs offline and deterministically on the bundled package fixtures
(the same ones tests/test_core.py, test_contract_cli.py, test_counterexample.py,
and test_investigate.py use). The round-trip tests assert the dataclass fields
carry the JSON values under the JSON key names and add no field of their own.
"""

import dataclasses
import importlib.util
import shutil
import struct
import wave
from importlib import resources
from pathlib import Path

import pytest

from hotato import cli, sdk
from hotato import contract as _contract
from hotato import core as _core
from hotato import investigate as _investigate
from hotato import transcribe as _transcribe
from hotato.counterexample import compile_counterexample as _compile
from hotato.counterexample import verify_counterexample as _verify

ROOT = Path(__file__).resolve().parent.parent
CE_FIXTURES = ROOT / "tests" / "fixtures" / "counterexample"


def _bundled_audio(scenario_id):
    return str(
        resources.files("hotato").joinpath(
            "data", "audio", scenario_id + ".example.wav"
        )
    )


def _field_names(obj):
    return {f.name for f in dataclasses.fields(obj)}


def _assert_no_invented_fields(obj, raw, optional=frozenset()):
    """Every dataclass field is a JSON key (some keys are conditionally absent)."""
    invented = _field_names(obj) - set(raw) - set(optional)
    assert not invented, f"{type(obj).__name__} has fields not in the JSON: {invented}"


# ---------------------------------------------------------------------------
# run_suite / run_single -> SuiteResult
# ---------------------------------------------------------------------------


def test_run_suite_happy_path_bundled_battery():
    result = sdk.run_suite()
    assert isinstance(result, sdk.SuiteResult)
    assert result.mode == "suite"
    assert result.passed is True
    assert result.failed == 0
    assert result.exit_code == 0
    assert result.summary.events == 8
    assert result.summary.passed == 8
    assert result.suite == "barge-in"
    assert len(result.events) == 8
    assert all(isinstance(e, sdk.Event) for e in result.events)


def test_run_suite_is_faithful_to_the_internal_envelope():
    raw = _core.run_suite(suite="barge-in")
    assert sdk.run_suite() == sdk.SuiteResult.from_json(raw)


def test_run_single_happy_path():
    result = sdk.run_single(
        stereo=_bundled_audio("01-hard-interruption"), expect="yield", onset_sec=0.3
    )
    assert isinstance(result, sdk.SuiteResult)
    assert result.mode == "single"
    assert result.suite is None
    assert len(result.events) == 1
    ev = result.events[0]
    # The four convenience properties read straight from the nested verdict.
    assert ev.passed == ev.verdict.passed
    assert ev.did_yield == ev.verdict.did_yield
    assert ev.seconds_to_yield == ev.verdict.seconds_to_yield
    assert ev.talk_over_sec == ev.verdict.talk_over_sec


def test_run_suite_requires_scenarios_and_audio_together():
    with pytest.raises(ValueError):
        sdk.run_suite(scenarios="only-one")


def test_suite_result_fields_match_json_keys():
    raw = _core.run_suite(suite="barge-in")
    result = sdk.SuiteResult.from_json(raw)
    _assert_no_invented_fields(result, raw)
    _assert_no_invented_fields(result.summary, raw["summary"], optional={"not_scorable"})
    # value round-trip at every level
    assert result.suite == raw["suite"]
    assert result.exit_code == raw["exit_code"]
    assert result.summary.failed == raw["summary"]["failed"]
    assert result.summary.passed == raw["summary"]["passed"]

    raw_ev = raw["events"][0]
    ev = result.events[0]
    _assert_no_invented_fields(
        ev, raw_ev, optional={"scorable", "not_scorable_reason"}
    )
    _assert_no_invented_fields(ev.verdict, raw_ev["verdict"])
    assert ev.event_id == raw_ev["event_id"]
    assert ev.verdict.passed == raw_ev["verdict"]["passed"]
    assert ev.seconds_to_yield == raw_ev["verdict"]["seconds_to_yield"]
    assert ev.did_yield == raw_ev["verdict"]["did_yield"]
    assert ev.talk_over_sec == raw_ev["verdict"]["talk_over_sec"]


# ---------------------------------------------------------------------------
# verify_contracts -> ContractVerifyResult
# ---------------------------------------------------------------------------


def _create_contract(out_dir, cid, *extra):
    rc = cli.main(
        [
            "contract", "create", "--stereo", _bundled_audio("01-hard-interruption"),
            "--id", cid, "--onset", "2.40", "--expect", "yield",
            "--out", str(out_dir), *extra,
        ]
    )
    assert rc == 0


def test_verify_contracts_pass(tmp_path):
    d = tmp_path / "pass"
    d.mkdir()
    _create_contract(d, "ct-ok-001")
    result = sdk.verify_contracts(d)
    assert isinstance(result, sdk.ContractVerifyResult)
    assert result.passed is True
    assert result.exit_code == 0
    assert result.results[0].passed is True
    # authenticity passes through verbatim (a freshly created bundle is unsigned)
    assert result.results[0].authenticity == "unsigned"


def test_verify_contracts_failing_case_does_not_raise(tmp_path):
    # The explicit contract: a regressed contract surfaces passed=False and
    # exit_code 1 as data, never as an exception.
    d = tmp_path / "fail"
    d.mkdir()
    _create_contract(d, "ct-bad-001", "--max-time-to-yield", "0.0")
    result = sdk.verify_contracts(d)
    assert result.passed is False
    assert result.exit_code == 1
    assert result.results[0].passed is False
    assert result.summary["failed"] == 1


def test_verify_contracts_bad_path_raises_valueerror(tmp_path):
    with pytest.raises(ValueError):
        sdk.verify_contracts(tmp_path / "does-not-exist")


def test_contract_result_fields_match_json_keys(tmp_path):
    d = tmp_path / "pass"
    d.mkdir()
    _create_contract(d, "ct-rt-001")
    raw = _contract.verify_contracts(str(d))
    result = sdk.ContractVerifyResult.from_json(raw)
    _assert_no_invented_fields(result, raw)
    _assert_no_invented_fields(result.results[0], raw["results"][0])
    assert result == sdk.verify_contracts(d)
    assert result.results[0].authenticity == raw["results"][0]["authenticity"]


# ---------------------------------------------------------------------------
# investigate -> InvestigateResult
# ---------------------------------------------------------------------------


def test_investigate_offline_on_bundled_call(tmp_path):
    call = tmp_path / "call.wav"
    shutil.copyfile(_bundled_audio("01-hard-interruption"), call)
    state = tmp_path / "state.json"
    result = sdk.investigate(call, state_path=state)
    assert isinstance(result, sdk.InvestigateResult)
    assert result.passed is True
    assert result.eligible is True
    assert result.total_candidates >= 1
    assert result.capture_origin["kind"] == "operator_asserted_local"


def test_investigate_result_fields_match_json_keys(tmp_path):
    call = tmp_path / "call.wav"
    shutil.copyfile(_bundled_audio("01-hard-interruption"), call)
    state = tmp_path / "state.json"
    raw, _code = _investigate.run_investigate(str(call), state_path=str(state))
    result = sdk.InvestigateResult.from_json(raw)
    _assert_no_invented_fields(result, raw)
    assert result.total_candidates == raw["total_candidates"]
    assert result.exit_code == raw["exit_code"]


# ---------------------------------------------------------------------------
# compile_counterexample / verify_counterexample
# ---------------------------------------------------------------------------


def test_compile_and_verify_counterexample(tmp_path):
    out = tmp_path / "case.hotato-repro"
    compiled = sdk.compile_counterexample(
        CE_FIXTURES / "pii.scenario.json",
        CE_FIXTURES / "pii.test.json",
        target="pii-email",
        out=out,
        workspace=CE_FIXTURES,
        budget=512,
    )
    assert isinstance(compiled, sdk.CounterexampleResult)
    assert compiled.passed is True
    assert compiled.exit_code == 0
    assert compiled.minimality == "one_minimal"

    verified = sdk.verify_counterexample(out)
    assert isinstance(verified, sdk.CounterexampleVerifyResult)
    assert verified.ok is True
    assert verified.passed is True
    assert verified.status == "verified"


def test_counterexample_refusal_is_typed(tmp_path):
    out = tmp_path / "x.hotato-repro"
    with pytest.raises(sdk.CounterexampleRefusal) as excinfo:
        sdk.compile_counterexample(
            CE_FIXTURES / "pii.scenario.json",
            CE_FIXTURES / "pii.test.json",
            target="not-a-real-assertion-id",
            out=out,
            workspace=CE_FIXTURES,
        )
    # a deterministic refusal carries a stable machine-readable code
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value.code, str) and excinfo.value.code


def test_counterexample_fields_match_json_keys(tmp_path):
    out = tmp_path / "case.hotato-repro"
    raw = _compile(
        str(CE_FIXTURES / "pii.scenario.json"),
        str(CE_FIXTURES / "pii.test.json"),
        target="pii-email",
        out_dir=str(out),
        workspace=str(CE_FIXTURES),
        budget=512,
    )
    compiled = sdk.CounterexampleResult.from_json(raw)
    _assert_no_invented_fields(compiled, raw)
    assert compiled.counterexample_id == raw["counterexample_id"]

    raw_v = _verify(str(out))
    verified = sdk.CounterexampleVerifyResult.from_json(raw_v)
    _assert_no_invented_fields(verified, raw_v, optional={"preserved_deletions"})
    assert verified.status == raw_v["status"]


# ---------------------------------------------------------------------------
# transcribe -> Transcript (cache-aware, model stubbed for determinism)
# ---------------------------------------------------------------------------


def _write_tiny_wav(path):
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<" + "h" * 1600, *([0] * 1600)))


def test_transcribe_returns_transcript_and_uses_cache(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    _write_tiny_wav(wav)
    canned = _transcribe.Transcript(
        text="hello there",
        segments=[_transcribe.TranscriptSegment(0.0, 1.0, "hello there")],
        language="en",
        model="stub",
        device="cpu",
        compute_type="int8",
    )
    calls = {"n": 0}

    def _fake_transcribe(
        path, model="base.en", device="auto", *, compute_type=None,
        word_timestamps=False, vad_filter=False, language=None,
    ):
        calls["n"] += 1
        return canned

    # The cache-aware entry point calls the module-level transcribe, so patching
    # the module attribute reaches it without loading any ASR model.
    monkeypatch.setattr(_transcribe, "transcribe", _fake_transcribe)

    cache = _transcribe.TranscriptCache(str(tmp_path / "c"))
    first = sdk.transcribe(wav, cache=cache)
    second = sdk.transcribe(wav, cache=cache)  # cache hit, model not re-run

    assert isinstance(first, sdk.Transcript)
    assert first.text == "hello there"
    assert first == second
    assert calls["n"] == 1


def test_transcribe_cached_surface_is_re_exported():
    assert sdk.transcribe_cached is _transcribe.transcribe_cached
    assert sdk.CachedTranscribeResult is _transcribe.CachedTranscribeResult
    assert sdk.TranscriptCache is _transcribe.TranscriptCache


# ---------------------------------------------------------------------------
# error types + typing
# ---------------------------------------------------------------------------


def test_error_types_are_the_shared_ones_inside_handled():
    # No parallel hierarchy: the SDK re-exports the existing types, all of which
    # the CLI/MCP error contract already handles.
    from hotato._engine.vad import BackendUnavailable as _BU
    from hotato.counterexample import CounterexampleRefusal as _CR
    from hotato.errors import HANDLED
    from hotato.errors import ChannelRangeError as _CRE

    assert sdk.BackendUnavailable is _BU
    assert sdk.CounterexampleRefusal is _CR
    assert sdk.ChannelRangeError is _CRE
    assert sdk.HANDLED is HANDLED
    assert issubclass(sdk.CounterexampleRefusal, ValueError)
    assert issubclass(sdk.ChannelRangeError, ValueError)


def test_package_ships_a_py_typed_marker():
    assert resources.files("hotato").joinpath("py.typed").is_file()


def test_sdk_passes_mypy_when_configured():
    # mypy is not configured for this project (no [tool.mypy], no mypy.ini, and
    # mypy is not a declared dev dependency), so this check skips. It runs only
    # where the project has adopted mypy.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    configured = "[tool.mypy]" in pyproject or (ROOT / "mypy.ini").exists()
    if not configured or importlib.util.find_spec("mypy") is None:
        pytest.skip("mypy is not configured for this project")
    from mypy import api

    stdout, stderr, code = api.run(
        [str(ROOT / "src" / "hotato" / "sdk.py"),
         "--ignore-missing-imports", "--follow-imports=silent"]
    )
    assert code == 0, stdout + stderr
