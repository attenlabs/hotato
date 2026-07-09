"""``hotato trace ingest/attach/export``: the voice-trace observability bridge.

Pinned here, against the canon in docs/TRACE.md and docs/OTEL.md:

  * ``ingest`` parses BOTH hotato's own OTel bridge JSONL (a bare-array or
    newline-delimited per-line span shape) and a standard OTel JSON export
    (a document with a top-level ``resourceSpans`` array), redacts
    ``call_id`` / ``deployment.agent_id`` / an ``asr_partial`` span's text by
    default, and writes ``hotato.voice_trace.v1`` JSONL that validates
    against ``schema/voice_trace.v1.json``;
  * a ``tool_call`` span's tool ``name`` survives ingest untouched even
    though the bridge format's OWN meta/span-kind line also uses a ``name``
    key for a different purpose (a real bug caught while building this: an
    early version of the parser popped a tool_call's tool name off every
    span, not just the meta line);
  * ``attach`` writes the trace into ``<bundle>/traces/voice_trace.jsonl``
    and re-renders ``evidence/timeline.html`` with a trace row, WITHOUT
    re-running the VAD or diarizer (it reads the bundle's own
    evidence/frames.jsonl and contract.json back in) -- this must work on a
    diarized-mono bundle with NO frame-level evidence, honestly, never a
    fabricated timeline;
  * the report wording pattern is verbatim: an "Evidence suggests ..." line
    when a TTS cancel/stop pair is present, always followed by "Hotato does
    not prove root cause.", always followed by an explicit "Unknowns: no
    client-side playout trace was attached." line;
  * ``export`` writes the SAME OTel bridge JSONL shape ``ingest`` reads, so
    ingest -> attach -> export -> ingest round-trips the identical spans;
  * every refusal (an existing --out without --force, an already-attached
    trace without --force, no trace attached yet, a missing/corrupt bundle,
    a schema-mismatched trace file, an unreadable OTel source) is exit 2 and
    leaves nothing partial behind.
"""

from __future__ import annotations

import json
import math
import os
import struct
import wave
from importlib import resources

import pytest

from hotato import cli
from hotato import diarize as _diarize
from hotato import trace as _trace

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))          # yields at 2.40

# tests/ carries no __init__.py, so resources.files("tests") is not a valid
# package lookup; resolve the shipped fixture from this file's own location
# instead, which works from a checkout and from an installed sdist tree.
DEMO_OTEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "otel", "demo-trace.otel.jsonl")


def _bundle(tmp_path, cid):
    return tmp_path / (cid + ".hotato")


def _create_contract(tmp_path, cid="ct-trace-001"):
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", cid,
        "--onset", "2.40", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    return _bundle(tmp_path, cid)


def _contract_json(bundle_dir):
    with open(bundle_dir / "contract.json", encoding="utf-8") as fh:
        return json.load(fh)


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")


def _write_mono(path, segments, *, duration_sec=6.0, sr=16000):
    n = int(duration_sec * sr)

    def _on(t):
        return any(s <= t < e for s, e in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        v = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(t) else 0
        frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _timeline(segments, *, n_frames=600, hop=0.01):
    return [any(s <= k * hop < e for s, e in segments) for k in range(n_frames)]


@pytest.fixture
def stub_diarizer():
    saved_f = dict(_diarize._DIARIZER_FACTORIES)
    saved_c = dict(_diarize._DIARIZER_CACHE)

    def _register(name, timelines=None, **kw):
        _diarize.register_diarizer_backend(
            name, _diarize.build_stub_backend(timelines, **kw)
        )

    try:
        yield _register
    finally:
        _diarize._DIARIZER_FACTORIES.clear()
        _diarize._DIARIZER_FACTORIES.update(saved_f)
        _diarize._DIARIZER_CACHE.clear()
        _diarize._DIARIZER_CACHE.update(saved_c)


# --- ingest: the shipped OTel bridge JSONL fixture -------------------------

def test_demo_fixture_exists_and_is_readable():
    assert os.path.isfile(DEMO_OTEL)


def test_ingest_bridge_jsonl_writes_voice_trace(tmp_path):
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out)])
    assert rc == 0
    vt = _trace.load_voice_trace_jsonl(str(out))
    assert vt["schema"] == "hotato.voice_trace.v1"
    assert vt["source"]["format"] == "otel-jsonl-bridge"
    types = {s["type"] for s in vt["spans"]}
    assert types == {
        "caller_audio_active", "agent_audio_active", "tts_cancel_requested",
        "tts_audio_stopped", "asr_partial", "tool_call",
    }


def test_ingest_redacts_identifiers_and_text_by_default(tmp_path):
    out = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out)])
    vt = _trace.load_voice_trace_jsonl(str(out))
    assert vt["call_id"] is None
    assert vt["deployment"]["agent_id"] is None
    # git_sha / config_hash / stack are NOT identifiers -- kept by default.
    assert vt["deployment"]["stack"] == "vapi"
    assert vt["deployment"]["git_sha"] == "deadbeefcafe"
    asr = next(s for s in vt["spans"] if s["type"] == "asr_partial")
    assert asr["text_redacted"] is True
    assert "text" not in asr


def test_ingest_include_identifiers_and_text_opts_in(tmp_path):
    out = tmp_path / "voice_trace.jsonl"
    cli.main([
        "trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out),
        "--include-identifiers", "--include-text",
    ])
    vt = _trace.load_voice_trace_jsonl(str(out))
    assert vt["call_id"] == "demo-call-001"
    assert vt["deployment"]["agent_id"] == "agent-demo-1"
    asr = next(s for s in vt["spans"] if s["type"] == "asr_partial")
    assert asr["text_redacted"] is False
    assert asr["text"] == "wait, I need a refund"


def test_ingest_tool_call_name_survives_untouched(tmp_path):
    """Regression: an early parser unconditionally popped "name" off every
    span record to use as a type fallback, destroying tool_call's own tool
    name even though "type" was already present on that record."""
    out = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out)])
    vt = _trace.load_voice_trace_jsonl(str(out))
    tool = next(s for s in vt["spans"] if s["type"] == "tool_call")
    assert tool["name"] == "lookup_order"
    assert tool["latency_ms"] == 320


def test_ingest_cli_overrides_take_precedence(tmp_path):
    out = tmp_path / "voice_trace.jsonl"
    cli.main([
        "trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out),
        "--stack", "livekit", "--git-sha", "override-sha",
    ])
    vt = _trace.load_voice_trace_jsonl(str(out))
    assert vt["deployment"]["stack"] == "livekit"
    assert vt["deployment"]["git_sha"] == "override-sha"


def test_ingest_a_bare_json_array_of_spans_is_accepted(tmp_path):
    src = tmp_path / "spans.json"
    with open(src, "w", encoding="utf-8") as fh:
        json.dump([
            {"type": "caller_audio_active", "start_sec": 1.0, "end_sec": 2.0},
            {"type": "tts_cancel_requested", "time_sec": 1.5},
        ], fh)
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", str(src), "--out", str(out)])
    assert rc == 0
    vt = _trace.load_voice_trace_jsonl(str(out))
    assert len(vt["spans"]) == 2


def test_ingest_missing_input_is_usage_error(tmp_path, capsys):
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", str(tmp_path / "nope.jsonl"), "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_ingest_empty_input_is_usage_error(tmp_path):
    src = tmp_path / "empty.jsonl"
    src.write_text("")
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", str(src), "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_ingest_meta_only_input_is_usage_error(tmp_path):
    """A file with only a resource/meta line and no actual spans is a clean
    refusal, not a voice trace with zero spans."""
    src = tmp_path / "meta_only.jsonl"
    _write_jsonl(src, [{"call_id": "x", "deployment": {"stack": "vapi"}}])
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", str(src), "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_ingest_refuses_existing_out_without_force(tmp_path):
    out = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out)])
    rc = cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out)])
    assert rc == 2
    rc = cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out), "--force"])
    assert rc == 0


def test_ingest_json_output_format(tmp_path, capsys):
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main([
        "trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out), "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "hotato"
    assert payload["kind"] == "trace-ingest"
    assert payload["voice_trace"]["schema"] == "hotato.voice_trace.v1"


# --- ingest: standard OTel JSON (resourceSpans) -----------------------------

def test_ingest_standard_otel_json_resource_spans(tmp_path):
    doc = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "vapi"}},
                {"key": "git_sha", "value": {"stringValue": "cafebabe"}},
            ]},
            "scopeSpans": [{"spans": [
                {
                    "name": "agent_audio_active",
                    "startTimeUnixNano": "1000000000",
                    "endTimeUnixNano": "4400000000",
                },
                {
                    "name": "tts.cancel_requested",
                    "startTimeUnixNano": "2600000000",
                    "events": [],
                },
                {
                    "name": "lookup_order",
                    "startTimeUnixNano": "1500000000",
                    "endTimeUnixNano": "1800000000",
                    "attributes": [{"key": "tool.name", "value": {"stringValue": "lookup_order"}}],
                    "events": [],
                },
            ]}],
        }],
    }
    src = tmp_path / "export.json"
    with open(src, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", str(src), "--out", str(out)])
    assert rc == 0
    vt = _trace.load_voice_trace_jsonl(str(out))
    assert vt["source"]["format"] == "otel-json"
    assert vt["deployment"]["stack"] == "vapi"
    assert vt["deployment"]["git_sha"] == "cafebabe"
    types = {s["type"] for s in vt["spans"]}
    assert "agent_audio_active" in types
    assert "tts_cancel_requested" in types
    # relative-seconds conversion: earliest timestamp becomes t=0
    agent = next(s for s in vt["spans"] if s["type"] == "agent_audio_active")
    assert agent["start_sec"] == pytest.approx(0.0)
    assert agent["end_sec"] == pytest.approx(3.4)
    cancel = next(s for s in vt["spans"] if s["type"] == "tts_cancel_requested")
    assert cancel["time_sec"] == pytest.approx(1.6)


def test_ingest_standard_otel_json_no_resource_spans_is_usage_error(tmp_path):
    src = tmp_path / "empty_export.json"
    with open(src, "w", encoding="utf-8") as fh:
        json.dump({"resourceSpans": []}, fh)
    out = tmp_path / "voice_trace.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", str(src), "--out", str(out)])
    assert rc == 2
    assert not out.exists()


# --- attach ------------------------------------------------------------------

def test_attach_writes_trace_and_rerenders_timeline(tmp_path):
    bundle_dir = _create_contract(tmp_path)
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])

    rc = cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)])
    assert rc == 0

    trace_dest = bundle_dir / "traces" / "voice_trace.jsonl"
    assert trace_dest.exists()
    assert not (bundle_dir / "traces" / ".gitkeep").exists()

    timeline = (bundle_dir / "evidence" / "timeline.html").read_text(encoding="utf-8")
    assert "Trace" in timeline
    assert "Evidence suggests TTS cancellation delay" in timeline
    assert "Hotato does not prove root cause." in timeline
    assert "Unknowns: no client-side playout trace was attached." in timeline

    c = _contract_json(bundle_dir)
    assert c["trace"]["attached"] is True
    assert c["trace"]["span_count"] == 6
    assert c["trace"]["path"] == "traces/voice_trace.jsonl"


def test_attach_refuses_missing_bundle(tmp_path):
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    rc = cli.main(["trace", "attach", str(tmp_path / "nope.hotato"), "--trace", str(vt_path)])
    assert rc == 2


def test_attach_refuses_non_voice_trace_file(tmp_path):
    bundle_dir = _create_contract(tmp_path)
    bogus = tmp_path / "bogus.jsonl"
    _write_jsonl(bogus, [{"hello": "world"}])
    rc = cli.main(["trace", "attach", str(bundle_dir), "--trace", str(bogus)])
    assert rc == 2
    c = _contract_json(bundle_dir)
    assert "trace" not in c


def test_attach_requires_force_to_replace(tmp_path):
    bundle_dir = _create_contract(tmp_path)
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    assert cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)]) == 0
    assert cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)]) == 2
    assert cli.main([
        "trace", "attach", str(bundle_dir), "--trace", str(vt_path), "--force",
    ]) == 0


def test_attach_json_output_format(tmp_path, capsys):
    bundle_dir = _create_contract(tmp_path)
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    capsys.readouterr()  # discard the create/ingest stdout above
    rc = cli.main([
        "trace", "attach", str(bundle_dir), "--trace", str(vt_path), "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "trace-attach"
    assert payload["span_count"] == 6


def test_attach_on_diarized_mono_bundle_has_no_fabricated_timeline(tmp_path, stub_diarizer):
    agent_segments = [(0.3, 2.0)]
    caller_segments = [(1.8, 2.6)]
    mono = _write_mono(tmp_path / "mono.wav", segments=agent_segments + caller_segments)
    stub_diarizer("pyannote", {
        _diarize.SPEAKER_A: _timeline(caller_segments),
        _diarize.SPEAKER_B: _timeline(agent_segments),
    }, posterior=0.9, embedding_margin=0.6)
    rc = cli.main([
        "contract", "create", "--mono", mono, "--diarize", "--onset", "1.8",
        "--caller-speaker", _diarize.SPEAKER_A, "--agent-speaker", _diarize.SPEAKER_B,
        "--id", "ct-diarized-trace", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    bundle_dir = _bundle(tmp_path, "ct-diarized-trace")

    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    rc = cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)])
    assert rc == 0

    timeline = (bundle_dir / "evidence" / "timeline.html").read_text(encoding="utf-8")
    assert "no frame-level timeline" in timeline
    assert "Trace" in timeline  # the trace row itself still renders


# --- export --------------------------------------------------------------

def test_export_round_trips_with_ingest(tmp_path):
    bundle_dir = _create_contract(tmp_path)
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)])

    otel_out = tmp_path / "otel_roundtrip.jsonl"
    rc = cli.main([
        "trace", "export", str(bundle_dir), "--format", "otel", "--out", str(otel_out),
    ])
    assert rc == 0

    reingested = tmp_path / "voice_trace_2.jsonl"
    rc = cli.main(["trace", "ingest", "--otel", str(otel_out), "--out", str(reingested)])
    assert rc == 0

    original = _trace.load_voice_trace_jsonl(str(vt_path))
    round_tripped = _trace.load_voice_trace_jsonl(str(reingested))
    orig_types = sorted(s["type"] for s in original["spans"])
    rt_types = sorted(s["type"] for s in round_tripped["spans"])
    assert orig_types == rt_types
    orig_tool = next(s for s in original["spans"] if s["type"] == "tool_call")
    rt_tool = next(s for s in round_tripped["spans"] if s["type"] == "tool_call")
    assert orig_tool["name"] == rt_tool["name"] == "lookup_order"


def test_export_requires_attached_trace(tmp_path):
    bundle_dir = _create_contract(tmp_path)
    out = tmp_path / "otel_out.jsonl"
    rc = cli.main(["trace", "export", str(bundle_dir), "--format", "otel", "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_export_refuses_existing_out_without_force(tmp_path):
    bundle_dir = _create_contract(tmp_path)
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)])

    out = tmp_path / "otel_out.jsonl"
    assert cli.main(["trace", "export", str(bundle_dir), "--format", "otel", "--out", str(out)]) == 0
    assert cli.main(["trace", "export", str(bundle_dir), "--format", "otel", "--out", str(out)]) == 2
    assert cli.main([
        "trace", "export", str(bundle_dir), "--format", "otel", "--out", str(out), "--force",
    ]) == 0


def test_export_json_output_format(tmp_path, capsys):
    bundle_dir = _create_contract(tmp_path)
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)])
    capsys.readouterr()  # discard the create/ingest/attach stdout above
    out = tmp_path / "otel_out.jsonl"
    rc = cli.main([
        "trace", "export", str(bundle_dir), "--format", "otel", "--out", str(out), "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "trace-export"
    assert payload["count"] == 6


# --- schema validation -------------------------------------------------------

def test_voice_trace_validates_against_its_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "voice_trace.v1.json")
        .read_text(encoding="utf-8")
    )
    out = tmp_path / "voice_trace.jsonl"
    cli.main([
        "trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out),
        "--include-identifiers", "--include-text",
    ])
    vt = _trace.load_voice_trace_jsonl(str(out))
    jsonschema.validate(instance=vt, schema=schema)


def test_voice_trace_schema_rejects_wrong_schema_const(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "voice_trace.v1.json")
        .read_text(encoding="utf-8")
    )
    out = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(out)])
    vt = _trace.load_voice_trace_jsonl(str(out))
    vt["schema"] = "hotato.voice_trace.v2"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=vt, schema=schema)


def test_contract_json_still_validates_after_trace_attach(tmp_path):
    """contract.v1.json's top-level additionalProperties: true means the new
    "trace" key attach adds is a schema-safe additive extension, never a
    schema-breaking change."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "contract.v1.json")
        .read_text(encoding="utf-8")
    )
    bundle_dir = _create_contract(tmp_path)
    vt_path = tmp_path / "voice_trace.jsonl"
    cli.main(["trace", "ingest", "--otel", DEMO_OTEL, "--out", str(vt_path)])
    cli.main(["trace", "attach", str(bundle_dir), "--trace", str(vt_path)])
    jsonschema.validate(instance=_contract_json(bundle_dir), schema=schema)
