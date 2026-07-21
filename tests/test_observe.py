"""``hotato observe``: local-first, deterministic LLM/voice observability.

One coherent command group -- capture / cost / percentiles / report -- built
on the same ``hotato.voice_trace.v1`` spans ``trace ingest`` produces. Pinned
here:

  * ``capture`` runs a child with a LOCAL FILE SINK wired through its env
    (HOTATO_OTEL_FILE + a file:// OTLP endpoint + OTEL_EXPORTER_OTLP_PROTOCOL=
    http/json), ingests whatever the child wrote, and refuses (exit 2) with
    nothing written when the child emitted no spans. hotato opens no socket
    and makes no network call of its own;
  * ``cost`` treats tokens as FACTS (per model + total, first-match aliases),
    reports a category no span carried as "not captured" (null + a missing
    count) and NEVER 0, prices only with a LOCAL table (labeled "estimated
    from <table>"), and leaves a model with no price row UNPRICED (null);
  * ``percentiles`` is nearest-rank over a folder, EXCLUDING (with a shown
    count) the traces that did not capture a hop, never counting them as 0;
  * ``report`` writes ONE self-contained HTML page (no external request, no
    wall clock) and is byte-identical across runs.
"""

from __future__ import annotations

import json
import sys

import pytest

from hotato import cli
from hotato import observe as OB
from hotato import trace as _trace

# --- fixtures -------------------------------------------------------------

# A child that writes hotato's OTel bridge JSONL to the local file sink hotato
# hands it via $HOTATO_OTEL_FILE -- the zero-config capture contract.
_CHILD_WRITES_SPANS = r"""
import os
p = os.environ["HOTATO_OTEL_FILE"]
assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"].startswith("file://")
assert os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
lines = [
 '{"type":"asr_partial","start_sec":0.10,"end_sec":0.65,"attributes":{"text":"hi"}}',
 '{"type":"llm_first_token","time_sec":0.90,"attributes":{"gen_ai.response.model":"gpt-4o","gen_ai.usage.input_tokens":1000,"gen_ai.usage.output_tokens":200,"gen_ai.usage.cached_tokens":100}}',
 '{"type":"tool_call","start_sec":1.0,"end_sec":1.32,"name":"lookup","latency_ms":320}',
 '{"type":"agent_audio_active","start_sec":1.4,"end_sec":3.0}',
]
open(p, "w").write("\n".join(lines) + "\n")
"""

_CHILD_WRITES_NOTHING = "pass"


def _capture_argv(out, script, *opts):
    # All capture options MUST precede the `--` separator; everything after it
    # is the child command line (argparse REMAINDER).
    return ["observe", "capture", "--out", str(out), *opts, "--",
            sys.executable, "-c", script]


def _bridge_lines(*lines):
    return "\n".join(lines) + "\n"


def _ingest(tmp_path, name, lines):
    raw = tmp_path / (name + ".raw.jsonl")
    raw.write_text(_bridge_lines(*lines), encoding="utf-8")
    out = tmp_path / (name + ".jsonl")
    _trace.ingest_otel(str(raw), out_path=str(out), force=True)
    return str(out)


def _load(path):
    return _trace.load_voice_trace_jsonl(path)


# --- capture --------------------------------------------------------------

def test_capture_ingests_child_spans_and_summarizes(tmp_path, capsys):
    out = tmp_path / "voice_trace.jsonl"
    code = cli.main(_capture_argv(out, _CHILD_WRITES_SPANS))
    assert code == 0
    assert out.exists()
    vt = _load(str(out))
    assert len(vt["spans"]) == 4
    summary = capsys.readouterr().out
    assert "4 spans" in summary
    assert "per-hop latency" in summary
    # tool_call latency surfaces in the per-hop summary
    assert "320.0 ms" in summary


def test_capture_json_envelope(tmp_path, capsys):
    out = tmp_path / "vt.jsonl"
    code = cli.main(_capture_argv(out, _CHILD_WRITES_SPANS, "--format", "json"))
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["tool"] == "hotato"
    assert env["kind"] == "observe-capture"
    assert env["schema_version"] == "1"
    assert env["span_count"] == 4
    assert env["child_returncode"] == 0


def test_capture_refuses_when_child_emits_no_spans(tmp_path, capsys):
    out = tmp_path / "vt.jsonl"
    code = cli.main(_capture_argv(out, _CHILD_WRITES_NOTHING))
    assert code == 2
    # Nothing partial left behind.
    assert not out.exists()
    err = capsys.readouterr().err
    assert "no spans" in err.lower()


def test_capture_refuses_existing_out_without_force(tmp_path):
    out = tmp_path / "vt.jsonl"
    out.write_text("preexisting", encoding="utf-8")
    with pytest.raises(ValueError):
        OB.capture([sys.executable, "-c", _CHILD_WRITES_SPANS],
                   out_path=str(out), force=False)
    # The pre-existing file is untouched (refused before any run).
    assert out.read_text(encoding="utf-8") == "preexisting"


def test_capture_adds_no_listener_env_is_a_file_url(tmp_path):
    env = OB._child_env(str(tmp_path / "sink.jsonl"))
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"].startswith("file://")
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"].startswith("file://")
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
    assert env["HOTATO_OTEL_FILE"].endswith("sink.jsonl")
    # no host:port endpoint anywhere -> nothing to connect to
    assert "http://" not in env["OTEL_EXPORTER_OTLP_ENDPOINT"]


# --- cost -----------------------------------------------------------------

def _cost_trace(tmp_path):
    return _ingest(tmp_path, "cost", [
        '{"type":"llm_first_token","time_sec":0.9,"attributes":{"gen_ai.response.model":"gpt-4o","gen_ai.usage.input_tokens":1000,"gen_ai.usage.output_tokens":200,"gen_ai.usage.cached_tokens":100}}',
        '{"type":"llm_first_token","time_sec":3.0,"attributes":{"gen_ai.request.model":"mystery-model","gen_ai.usage.input_tokens":500}}',
    ])


def test_cost_tokens_are_facts_per_model_and_total(tmp_path):
    rollup = OB.cost_rollup(_load(_cost_trace(tmp_path)), prices=None)
    assert rollup["llm_span_count"] == 2
    by = {m["model"]: m for m in rollup["models"]}
    assert by["gpt-4o"]["tokens"]["input"]["total"] == 1000
    assert by["gpt-4o"]["tokens"]["output"]["total"] == 200
    assert by["mystery-model"]["tokens"]["input"]["total"] == 500
    # grand totals sum the facts
    assert rollup["total_tokens"]["input"]["total"] == 1500


def test_cost_missing_category_is_not_captured_never_zero(tmp_path):
    rollup = OB.cost_rollup(_load(_cost_trace(tmp_path)), prices=None)
    by = {m["model"]: m for m in rollup["models"]}
    reasoning = by["gpt-4o"]["tokens"]["reasoning"]
    assert reasoning["total"] is None          # not captured, NOT 0
    assert reasoning["missing_spans"] == 1
    # mystery-model reported only input; output is not captured
    out = by["mystery-model"]["tokens"]["output"]
    assert out["total"] is None
    assert out["missing_spans"] == 1


def test_cost_alias_first_match(tmp_path):
    # OpenInference-style alias resolves when the gen_ai.* one is absent.
    path = _ingest(tmp_path, "alias", [
        '{"type":"llm_first_token","time_sec":1.0,"attributes":{"llm.model_name":"gpt-4o","prompt_tokens":42,"completion_tokens":7}}',
    ])
    rollup = OB.cost_rollup(_load(path), prices=None)
    m = rollup["models"][0]
    assert m["model"] == "gpt-4o"
    assert m["tokens"]["input"]["total"] == 42
    assert m["tokens"]["output"]["total"] == 7


def test_cost_starter_prices_estimate_labeled(tmp_path, capsys):
    code = cli.main(["observe", "cost", _cost_trace(tmp_path), "--prices", "starter"])
    assert code == 0
    out = capsys.readouterr().out
    # 1000/1e6*2.5 + 200/1e6*10 + 100/1e6*1.25 = 0.004625
    assert "0.004625" in out
    assert "estimated from starter" in out
    # the model with no price row is stated as unpriced, never guessed
    assert "UNPRICED" in out or "unpriced" in out


def test_cost_unpriced_model_is_null(tmp_path):
    rollup = OB.cost_rollup(_load(_cost_trace(tmp_path)), prices="starter")
    by = {m["model"]: m for m in rollup["models"]}
    assert by["gpt-4o"]["priced"] is True
    assert by["gpt-4o"]["estimated_usd"] == pytest.approx(0.004625)
    assert by["mystery-model"]["priced"] is False
    assert by["mystery-model"]["estimated_usd"] is None
    assert "mystery-model" in rollup["unpriced_models"]


def test_cost_prices_from_a_local_file(tmp_path):
    prices = tmp_path / "rates.yaml"
    prices.write_text(
        "table: my-contract\nper_tokens: 1000000\nmodels:\n"
        "  gpt-4o:\n    input: 1.0\n    output: 2.0\n",
        encoding="utf-8",
    )
    rollup = OB.cost_rollup(_load(_cost_trace(tmp_path)), prices=str(prices))
    by = {m["model"]: m for m in rollup["models"]}
    # 1000/1e6*1 + 200/1e6*2 = 0.0014 (cached has no rate -> not billed)
    assert by["gpt-4o"]["estimated_usd"] == pytest.approx(0.0014)
    assert rollup["prices"]["table"] == "my-contract"


def test_cost_no_prices_leaves_usd_null(tmp_path):
    rollup = OB.cost_rollup(_load(_cost_trace(tmp_path)), prices=None)
    assert rollup["total_estimated_usd"] is None
    assert all(m["estimated_usd"] is None for m in rollup["models"])
    assert rollup["prices"] is None


def test_cost_json_envelope(tmp_path, capsys):
    code = cli.main(["observe", "cost", _cost_trace(tmp_path), "--format", "json"])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["kind"] == "observe-cost"
    assert env["schema_version"] == "1"


def test_cost_bad_prices_table_is_exit_2(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("just: a scalar, no models section\n", encoding="utf-8")
    code = cli.main(["observe", "cost", _cost_trace(tmp_path), "--prices", str(bad)])
    assert code == 2


# --- percentiles ----------------------------------------------------------

def _trace_dir(tmp_path):
    d = tmp_path / "traces"
    d.mkdir()
    # t0: has a tool hop; t1: NO tool hop; t2: has a tool hop, slowest
    _ingest(d, "t0", [
        '{"type":"asr_partial","start_sec":0.0,"end_sec":0.5}',
        '{"type":"llm_first_token","time_sec":0.8}',
        '{"type":"tool_call","start_sec":1.0,"end_sec":1.3,"latency_ms":300}',
        '{"type":"agent_audio_active","start_sec":1.4,"end_sec":2.0}',
    ])
    _ingest(d, "t1", [
        '{"type":"asr_partial","start_sec":0.0,"end_sec":0.7}',
        '{"type":"llm_first_token","time_sec":1.2}',
        '{"type":"agent_audio_active","start_sec":1.5,"end_sec":4.0}',
    ])
    _ingest(d, "t2", [
        '{"type":"asr_partial","start_sec":0.0,"end_sec":0.9}',
        '{"type":"llm_first_token","time_sec":1.5}',
        '{"type":"tool_call","start_sec":1.6,"end_sec":2.4,"latency_ms":800}',
        '{"type":"agent_audio_active","start_sec":2.5,"end_sec":6.0}',
    ])
    return d


def test_percentiles_nearest_rank_excludes_uncaptured_hops(tmp_path):
    result = OB.percentiles_over_dir(str(_trace_dir(tmp_path)))
    assert result["trace_count"] == 3
    assert result["method"] == "nearest-rank"
    hops = {h["hop"]: h for h in result["hops"]}
    # tool captured in 2 of 3 traces -> excluded 1, never counted as 0
    assert hops["tool"]["n"] == 2
    assert hops["tool"]["excluded_null"] == 1
    # nearest-rank p50 of [300, 800] = value at ceil(.5*2)=1 -> 300
    assert hops["tool"]["p50"] == 300.0
    assert hops["tool"]["p99"] == 800.0
    # transport captured nowhere -> all null, excluded 3, never 0
    assert hops["transport"]["p50"] is None
    assert hops["transport"]["excluded_null"] == 3
    # end-to-end measured on all three
    assert result["end_to_end"]["n"] == 3


def test_percentiles_missing_dir_is_exit_2(tmp_path, capsys):
    code = cli.main(["observe", "percentiles", str(tmp_path / "nope")])
    assert code == 2


def test_percentiles_empty_dir_is_exit_2(tmp_path, capsys):
    d = tmp_path / "empty"
    d.mkdir()
    code = cli.main(["observe", "percentiles", str(d)])
    assert code == 2


def test_percentiles_html_panel_is_self_contained(tmp_path):
    panel = tmp_path / "panel.html"
    code = cli.main(["observe", "percentiles", str(_trace_dir(tmp_path)),
                     "--html", str(panel)])
    assert code == 0
    html = panel.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "<svg" in html
    assert "http://" not in html and "https://" not in html


def test_percentiles_json_envelope(tmp_path, capsys):
    code = cli.main(["observe", "percentiles", str(_trace_dir(tmp_path)),
                     "--format", "json"])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["kind"] == "observe-percentiles"
    assert env["trace_count"] == 3


# --- report ---------------------------------------------------------------

def test_report_writes_self_contained_html(tmp_path):
    out = tmp_path / "observe.html"
    code = cli.main(["observe", "report", str(_trace_dir(tmp_path)),
                     "--out", str(out), "--prices", "starter"])
    assert code == 0
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "<svg" in html
    # fully self-contained: no external fetch of any kind
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html and "<script" not in html
    # honest counter-position copy, no competitor names
    assert "derived on your machine" in html


def test_report_links_to_the_worst_traces(tmp_path):
    out = tmp_path / "observe.html"
    cli.main(["observe", "report", str(_trace_dir(tmp_path)), "--out", str(out)])
    html = out.read_text(encoding="utf-8")
    # the slowest trace (t2, end-to-end 6.0s) is linked
    assert 'href="' in html
    assert "t2.jsonl" in html


def test_report_is_byte_identical_across_runs(tmp_path):
    d = _trace_dir(tmp_path)
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    cli.main(["observe", "report", str(d), "--out", str(a), "--prices", "starter"])
    cli.main(["observe", "report", str(d), "--out", str(b), "--prices", "starter"])
    assert a.read_bytes() == b.read_bytes()


def test_report_refuses_existing_out_without_force(tmp_path):
    out = tmp_path / "observe.html"
    out.write_text("preexisting", encoding="utf-8")
    with pytest.raises(ValueError):
        OB.build_report(str(_trace_dir(tmp_path)), out_path=str(out), force=False)
    assert out.read_text(encoding="utf-8") == "preexisting"


def test_report_json_envelope(tmp_path, capsys):
    out = tmp_path / "observe.html"
    code = cli.main(["observe", "report", str(_trace_dir(tmp_path)),
                     "--out", str(out), "--format", "json"])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["kind"] == "observe-report"
    assert env["trace_count"] == 3
    assert len(env["worst_traces"]) >= 1


# --- one coherent group ---------------------------------------------------

def test_single_top_level_observe_parser_with_four_subcommands():
    import argparse
    parser = cli.build_parser()
    top = None
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            top = a
            break
    assert list(top.choices).count("observe") == 1
    observe = top.choices["observe"]
    subnames = set()
    for a in observe._actions:
        if isinstance(a, argparse._SubParsersAction):
            subnames = set(a.choices)
    assert subnames == {"capture", "cost", "percentiles", "report"}


def test_starter_price_table_ships_as_package_data():
    from importlib import resources
    text = (resources.files("hotato").joinpath("data", "prices.yaml")
            .read_text(encoding="utf-8"))
    assert "models:" in text
    assert "gpt-4o" in text
