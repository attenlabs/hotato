"""``hotato observe``: local-first, deterministic LLM/voice observability.

The observability that an LLM or voice agent already emits as OpenTelemetry
spans, read on YOUR machine instead of shipped to an account. Four commands,
one coherent group, all built on the ``hotato.voice_trace.v1`` spans the
existing ``trace ingest`` path already produces:

* ``observe capture -- <command...>`` runs a child process with a LOCAL FILE
  SINK wired through its environment (``HOTATO_OTEL_FILE`` plus a ``file://``
  OTLP endpoint and ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json``), so any
  OTel-emitting process writes its spans to a file hotato names -- no account,
  no config, and hotato itself opens no socket, no listener, and makes no
  network call of its own. On exit hotato ingests that file through the same
  ``trace ingest`` code and writes a ``voice_trace.jsonl``, then prints a
  one-screen summary. The child's own network is the child's business.

* ``observe cost <voice_trace.jsonl>`` rolls up per-span LLM token usage
  (``gen_ai.usage.input_tokens`` / ``output_tokens`` / cached / reasoning, by
  first-match alias) per model and in total. Tokens are FACTS read from the
  spans. With ``--prices`` (a LOCAL per-model $/1M table, or ``starter`` for
  the bundled one) it also computes an ESTIMATED USD cost, labeled "estimated
  from <table>". A category no span reported is "not captured" (null plus a
  missing-span count), never 0; a model with no price row is UNPRICED (null),
  never a guessed rate.

* ``observe percentiles DIR`` reads a folder of ingested traces and reports
  p50 / p90 / p99 of each per-hop latency and of end-to-end latency by the
  nearest-rank method (``hotato._stats.nearest_rank``), every percentile an
  observed measurement. A hop a trace did not capture is EXCLUDED with a shown
  excluded-count, never counted as 0.

* ``observe report DIR --out observe.html`` writes ONE self-contained HTML
  page (inline CSS and SVG, no external request) summarizing trace and span
  counts, per-hop latency and its percentiles, token totals and an estimated
  USD line (with ``--prices``), and links to the slowest traces.

Deterministic and offline: the same inputs render byte-identical text, JSON,
and HTML, and no artifact embeds a wall clock.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from importlib import resources
from typing import List, Optional, Sequence

from . import _stats
from . import report as _report
from . import trace as _trace
from .errors import open_regular as _open_regular

__all__ = [
    "TOKEN_CATEGORIES",
    "capture",
    "cost_rollup",
    "percentiles_over_dir",
    "build_report_html",
    "render_capture_text",
    "render_cost_text",
    "render_percentiles_text",
    "capture_result_json",
    "cost_result_json",
    "percentiles_result_json",
    "report_result_json",
]

# The four token categories, in a fixed display order. Each maps to the OTel
# GenAI semantic-convention attribute plus the aliases seen in the wild; the
# FIRST alias a span carries wins (never summed across aliases on one span).
TOKEN_CATEGORIES = ("input", "output", "cached", "reasoning")

_TOKEN_ALIASES = {
    "input": (
        "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens",
        "llm.token_count.prompt", "input_tokens", "prompt_tokens",
    ),
    "output": (
        "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens",
        "llm.token_count.completion", "output_tokens", "completion_tokens",
    ),
    "cached": (
        "gen_ai.usage.cached_tokens", "gen_ai.usage.cache_read_input_tokens",
        "cached_tokens", "cache_read_input_tokens",
    ),
    "reasoning": (
        "gen_ai.usage.reasoning_tokens", "reasoning_tokens",
    ),
}

# Model-name attribute, first-match. `gen_ai.response.model` is the model that
# actually served the call, so it is preferred over the requested model.
_MODEL_ALIASES = (
    "gen_ai.response.model", "gen_ai.request.model", "gen_ai.model",
    "llm.model_name", "model",
)

_STARTER_PRICES_RESOURCE = ("data", "prices.yaml")


# --- small value helpers --------------------------------------------------

def _as_number(v):
    """A real numeric token count, or None. OTel exports carry integer
    attributes as strings (``{"intValue": "1200"}``), so a numeric string is
    coerced; a bool is never a token count."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None
    return None


def _span_attrs(span: dict) -> dict:
    """The lookup surface for token/model attributes: the span's own
    ``attributes`` bag layered over its top-level keys, so both the OTel
    export shape (attributes nested) and a flat bridge span are read."""
    merged = {k: v for k, v in span.items() if k != "attributes"}
    attrs = span.get("attributes")
    if isinstance(attrs, dict):
        merged.update(attrs)
    return merged


def _first_alias(attrs: dict, aliases: Sequence[str]):
    for a in aliases:
        if a in attrs:
            return attrs[a]
    return None


def _model_of(attrs: dict) -> Optional[str]:
    raw = _first_alias(attrs, _MODEL_ALIASES)
    if raw is None:
        return None
    return str(raw)


def _token_of(attrs: dict, category: str):
    return _as_number(_first_alias(attrs, _TOKEN_ALIASES[category]))


# --- capture --------------------------------------------------------------

def _child_env(sink_path: str) -> dict:
    """The child environment that points an OTel exporter (or any cooperating
    process) at a LOCAL FILE hotato names. hotato opens no socket and no
    listener: the endpoint is a ``file://`` URL, the sink path is also handed
    over plainly as ``HOTATO_OTEL_FILE`` for a process that writes hotato's
    bridge JSONL directly, and the protocol is the plain-text ``http/json``
    OTLP encoding. No egress of hotato's own is added; the child's own network
    is the child's business."""
    env = dict(os.environ)
    file_url = "file://" + os.path.abspath(sink_path)
    env["HOTATO_OTEL_FILE"] = os.path.abspath(sink_path)
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = file_url
    env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = file_url
    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/json"
    env["OTEL_TRACES_EXPORTER"] = "otlp"
    return env


def capture(command: Sequence[str], *, out_path: str, force: bool = False) -> dict:
    """Run ``command`` with a local file sink wired through its environment,
    then ingest whatever spans it wrote into ``out_path`` (a
    ``voice_trace.jsonl``). Raises ``ValueError`` (CLI exit 2) when ``out_path``
    exists without ``force``, or when the child wrote no spans to the sink."""
    if not command:
        raise ValueError(
            "no command to run; usage: hotato observe capture [--out PATH] "
            "-- <command...>"
        )
    if os.path.exists(out_path) and not force:
        raise ValueError(
            f"{out_path!r} already exists; pass --force to overwrite it, or "
            "choose a new --out"
        )
    sink_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(sink_dir, exist_ok=True)
    fd, sink = tempfile.mkstemp(dir=sink_dir, prefix=".hotato-otel-sink-",
                                suffix=".jsonl")
    os.close(fd)
    try:
        proc = subprocess.run(list(command), env=_child_env(sink))
        child_returncode = proc.returncode
        if (not os.path.exists(sink)) or os.path.getsize(sink) == 0:
            raise ValueError(
                "no spans were captured: the child process wrote nothing to "
                "the local OTel file sink ($HOTATO_OTEL_FILE / the file:// "
                "OTLP endpoint). Point its OTel exporter at that file, or have "
                "it write hotato's OTel bridge JSONL there (see docs/OBSERVE.md)."
            )
        ingest = _trace.ingest_otel(sink, out_path=out_path, force=True)
    finally:
        try:
            os.unlink(sink)
        except OSError:
            pass

    vt = ingest["voice_trace"]
    waterfall = _report._latency_waterfall(vt)
    tokens = cost_rollup(vt, prices=None)
    return {
        "out_path": out_path,
        "span_count": ingest["count"],
        "child_returncode": child_returncode,
        "waterfall": waterfall,
        "tokens": tokens,
    }


def render_capture_text(result: dict) -> str:
    lines = [
        f"observe capture: {result['span_count']} spans -> {result['out_path']}",
        f"  child exit: {result['child_returncode']}",
    ]
    lines.append("  per-hop latency:")
    lines.extend("    " + ln for ln in _waterfall_text_lines(result["waterfall"]))
    lines.append("  tokens:")
    lines.extend("    " + ln for ln in _tokens_text_lines(result["tokens"]))
    return "\n".join(lines)


def capture_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "observe-capture", "schema_version": "1",
        "out_path": result["out_path"],
        "span_count": result["span_count"],
        "child_returncode": result["child_returncode"],
        "latency_waterfall": result["waterfall"],
        "tokens": result["tokens"],
    }


# --- cost -----------------------------------------------------------------

def _load_price_table(prices: Optional[str]) -> Optional[dict]:
    """Load a LOCAL per-model price table. ``None`` means no pricing was
    requested (USD stays null everywhere). ``"starter"`` loads the bundled
    ``data/prices.yaml``; any other value is a path to a YAML/JSON table.
    Raises ``ValueError`` (CLI exit 2) on an unreadable or malformed table."""
    if prices is None:
        return None
    from .assert_ import parse_assertions_yaml  # zero-dep YAML/JSON reader
    if prices == "starter":
        text = (
            resources.files("hotato")
            .joinpath(*_STARTER_PRICES_RESOURCE)
            .read_text(encoding="utf-8")
        )
        table_label = "starter"
    else:
        try:
            with _open_regular(prices, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise ValueError(f"{prices!r} is not a readable price table: {exc}") from exc
        table_label = os.path.basename(prices)
    try:
        doc = parse_assertions_yaml(text)
    except ValueError as exc:
        raise ValueError(f"{prices!r} is not a valid price table: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict):
        raise ValueError(
            f"{prices!r} is not a valid price table: expected a mapping with a "
            "'models' section (per-model input/output/cached/reasoning rates)"
        )
    per_tokens = _as_number(doc.get("per_tokens")) or 1_000_000
    label = doc.get("table") or table_label
    return {
        "label": str(label),
        "per_tokens": per_tokens,
        "currency": str(doc.get("currency") or "USD"),
        "models": doc["models"],
    }


def _price_row(table: Optional[dict], model: Optional[str]) -> Optional[dict]:
    if table is None or model is None:
        return None
    row = table["models"].get(model)
    return row if isinstance(row, dict) else None


def cost_rollup(trace: dict, *, prices: Optional[str]) -> dict:
    """Roll up per-span token usage per model, and (when ``prices`` is given)
    an estimated USD cost. ``prices`` is ``None`` (tokens only), ``"starter"``
    (the bundled table), or a path. Returns a stable envelope whether or not a
    span reported anything."""
    price_table = _load_price_table(prices)
    spans = trace.get("spans") or []

    # Attribute each token-bearing span to a model.
    per_model: dict = {}
    llm_span_count = 0
    for s in spans:
        attrs = _span_attrs(s)
        cat_values = {c: _token_of(attrs, c) for c in TOKEN_CATEGORIES}
        model_name = _model_of(attrs)
        if model_name is None and all(v is None for v in cat_values.values()):
            continue  # not an LLM/token-bearing span
        llm_span_count += 1
        key = model_name if model_name is not None else "unknown"
        bucket = per_model.setdefault(key, {
            "spans": 0,
            "reported": {c: [] for c in TOKEN_CATEGORIES},
        })
        bucket["spans"] += 1
        for c in TOKEN_CATEGORIES:
            if cat_values[c] is not None:
                bucket["reported"][c].append(cat_values[c])

    def _tokens_block(bucket: dict) -> dict:
        out = {}
        for c in TOKEN_CATEGORIES:
            vals = bucket["reported"][c]
            total = sum(vals) if vals else None
            out[c] = {
                "total": total,
                "reported_spans": len(vals),
                "missing_spans": bucket["spans"] - len(vals),
            }
        return out

    models_out = []
    total_tokens = {c: {"total": None, "reported_spans": 0, "missing_spans": 0}
                    for c in TOKEN_CATEGORIES}
    total_usd = None
    unpriced_models = []
    for key in sorted(per_model):
        bucket = per_model[key]
        tokens = _tokens_block(bucket)
        model_out = {"model": key, "spans": bucket["spans"], "tokens": tokens}

        # Accumulate the grand totals (facts).
        for c in TOKEN_CATEGORIES:
            tb = total_tokens[c]
            if tokens[c]["total"] is not None:
                tb["total"] = (tb["total"] or 0) + tokens[c]["total"]
            tb["reported_spans"] += tokens[c]["reported_spans"]
            tb["missing_spans"] += tokens[c]["missing_spans"]

        row = _price_row(price_table, key)
        if price_table is None:
            model_out["estimated_usd"] = None
            model_out["priced"] = None  # pricing not requested at all
        elif row is None:
            model_out["estimated_usd"] = None
            model_out["priced"] = False  # UNPRICED: no row for this model
            unpriced_models.append(key)
        else:
            usd = 0.0
            priced_categories = []
            for c in TOKEN_CATEGORIES:
                tot = tokens[c]["total"]
                rate = _as_number(row.get(c))
                if tot is not None and rate is not None:
                    usd += tot / price_table["per_tokens"] * rate
                    priced_categories.append(c)
            model_out["estimated_usd"] = round(usd, 6)
            model_out["priced"] = True
            model_out["priced_categories"] = priced_categories
            total_usd = (total_usd or 0.0) + usd
        models_out.append(model_out)

    return {
        "llm_span_count": llm_span_count,
        "models": models_out,
        "total_tokens": total_tokens,
        "total_estimated_usd": (round(total_usd, 6) if total_usd is not None else None),
        "unpriced_models": unpriced_models,
        "prices": (
            None if price_table is None
            else {"table": price_table["label"],
                  "currency": price_table["currency"],
                  "per_tokens": price_table["per_tokens"]}
        ),
    }


def _tok(v) -> str:
    return "not captured" if v is None else str(v)


def _tokens_text_lines(rollup: dict) -> List[str]:
    tt = rollup["total_tokens"]
    parts = []
    for c in TOKEN_CATEGORIES:
        block = tt[c]
        suffix = ""
        if block["total"] is None and block["missing_spans"]:
            suffix = f" ({block['missing_spans']} spans without it)"
        parts.append(f"{c} {_tok(block['total'])}{suffix}")
    lines = ["  ".join(parts)] if parts else ["(no token-bearing spans)"]
    if rollup["prices"] is not None:
        usd = rollup["total_estimated_usd"]
        if usd is None:
            lines.append("estimated cost: null "
                         f"(no priced model matched; table {rollup['prices']['table']})")
        else:
            lines.append(f"estimated cost: {rollup['prices']['currency']} "
                         f"{usd:.6f} (estimated from {rollup['prices']['table']})")
        if rollup["unpriced_models"]:
            lines.append("unpriced models: " + ", ".join(rollup["unpriced_models"]))
    return lines


def render_cost_text(rollup: dict) -> str:
    lines = [f"observe cost: {rollup['llm_span_count']} token-bearing span(s)"]
    if not rollup["models"]:
        lines.append("  no LLM/token-bearing spans in this trace")
        return "\n".join(lines)
    for m in rollup["models"]:
        lines.append(f"  model: {m['model']}  ({m['spans']} span(s))")
        for c in TOKEN_CATEGORIES:
            b = m["tokens"][c]
            miss = f", {b['missing_spans']} span(s) without it" if b["missing_spans"] else ""
            lines.append(f"    {c:9s}: {_tok(b['total'])} "
                         f"({b['reported_spans']} span(s) reported{miss})")
        if m["priced"] is None:
            lines.append("    cost     : not requested (pass --prices to estimate USD)")
        elif m["priced"] is False:
            lines.append("    cost     : UNPRICED (no row for this model in the price table)")
        else:
            cur = rollup["prices"]["currency"]
            lines.append(f"    cost     : {cur} {m['estimated_usd']:.6f} "
                         f"(estimated from {rollup['prices']['table']})")
    lines.append("  totals:")
    lines.extend("    " + ln for ln in _tokens_text_lines(rollup))
    return "\n".join(lines)


def cost_result_json(rollup: dict) -> dict:
    return {
        "tool": "hotato", "kind": "observe-cost", "schema_version": "1",
        **rollup,
    }


# --- percentiles ----------------------------------------------------------

def _trace_end2end_ms(trace: dict) -> Optional[float]:
    """End-to-end span coverage of one trace in milliseconds: latest span end
    minus earliest span start. A point event's "end" is its own time. None
    when the trace has no timed span (never a fabricated 0)."""
    spans = trace.get("spans") or []
    starts, ends = [], []
    for s in spans:
        st = _report._fx_span_start(s)
        if st is not None:
            starts.append(st)
            end = s.get("end_sec")
            if end is None:
                end = s.get("time_sec")
            if end is None:
                end = st
            ends.append(end)
    if not starts or not ends:
        return None
    return round((max(ends) - min(starts)) * 1000.0, 3)


def _iter_trace_files(dir_path: str) -> List[str]:
    if not os.path.isdir(dir_path):
        raise ValueError(f"{dir_path!r} is not a directory")
    out = []
    for name in sorted(os.listdir(dir_path)):
        if name.endswith(".jsonl") and not name.startswith("."):
            out.append(os.path.join(dir_path, name))
    return out


def _load_traces(dir_path: str) -> List[dict]:
    """Load every readable ``*.jsonl`` voice trace in ``dir_path``. A file that
    is not a voice trace is skipped (recorded in ``skipped``), never a crash;
    raises ``ValueError`` only when NO readable trace is found at all."""
    files = _iter_trace_files(dir_path)
    traces = []
    skipped = []
    for path in files:
        try:
            vt = _trace.load_voice_trace_jsonl(path)
        except ValueError:
            skipped.append(os.path.basename(path))
            continue
        vt["_path"] = path
        traces.append(vt)
    if not traces:
        raise ValueError(
            f"{dir_path!r} has no readable {_trace.SCHEMA} voice traces "
            "(ingest some with `hotato observe capture` or `hotato trace "
            "ingest` first)"
        )
    return traces, skipped


_HOP_ORDER = ("stt", "llm", "tool", "tts", "transport")


def percentiles_over_dir(dir_path: str) -> dict:
    """p50 / p90 / p99 of each per-hop latency and of end-to-end latency over
    every readable trace in ``dir_path``, by the nearest-rank method. A hop a
    trace did not capture is excluded (``excluded_null``), never a 0."""
    traces, skipped = _load_traces(dir_path)
    total = len(traces)

    hop_values: dict = {h: [] for h in _HOP_ORDER}
    hop_labels: dict = {}
    span_total = 0
    e2e_values = []
    for vt in traces:
        span_total += len(vt.get("spans") or [])
        wf = _report._latency_waterfall(vt)
        by_hop = {h["hop"]: h for h in wf["hops"]}
        for h in _HOP_ORDER:
            hop = by_hop.get(h)
            if hop is not None:
                hop_labels.setdefault(h, hop["label"])
                if hop.get("ms") is not None:
                    hop_values[h].append(hop["ms"])
        e2e = _trace_end2end_ms(vt)
        if e2e is not None:
            e2e_values.append(e2e)

    hops = []
    for h in _HOP_ORDER:
        pct = _stats.corpus_percentiles(hop_values[h], total)
        hops.append({"hop": h, "label": hop_labels.get(h, _report._HOP_LABEL[h]),
                     "unit": "ms", **pct})
    end_to_end = {"unit": "ms", **_stats.corpus_percentiles(e2e_values, total)}

    return {
        "dir": dir_path,
        "trace_count": total,
        "span_count": span_total,
        "skipped": skipped,
        "method": "nearest-rank",
        "method_note": (
            "Nearest-rank over the traces that captured each hop: with the "
            "measured values sorted ascending and n of them, rank = ceil(q*n) "
            "and the percentile is the value at that rank -- always an observed "
            "measurement. A trace that did not capture a hop is excluded "
            "(excluded shown), never counted as 0."
        ),
        "hops": hops,
        "end_to_end": end_to_end,
    }


def _pct_row_text(label: str, block: dict) -> str:
    def f(x):
        return "n/a" if x is None else f"{x:.1f}"
    return (f"    {label:26s} p50 {f(block['p50']):>8s}  p90 {f(block['p90']):>8s}  "
            f"p99 {f(block['p99']):>8s}  (n={block['n']}, excluded {block['excluded_null']})")


def render_percentiles_text(result: dict) -> str:
    lines = [
        f"observe percentiles: {result['trace_count']} trace(s), "
        f"{result['span_count']} span(s) in {result['dir']}",
        f"  method: {result['method']} (each percentile is an observed value)",
        "  per-hop latency (ms):",
    ]
    for h in result["hops"]:
        lines.append(_pct_row_text(h["label"], h))
    lines.append("  end-to-end latency (ms):")
    lines.append(_pct_row_text("end to end", result["end_to_end"]))
    if result["skipped"]:
        lines.append("  skipped (not a voice trace): " + ", ".join(result["skipped"]))
    return "\n".join(lines)


def percentiles_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "observe-percentiles", "schema_version": "1",
        **result,
    }


# --- self-contained HTML --------------------------------------------------

_OBSERVE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:%(bg)s;color:%(cream)s;
 font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:24px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:20px;margin:0 0 2px;color:%(cream)s}
.sub{color:%(muted)s;margin:0 0 18px;font-size:12.5px}
.card{background:%(card)s;border:1px solid %(line)s;border-radius:10px;
 padding:16px 18px;margin:0 0 16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;
 color:%(muted)s;margin:0 0 12px}
.kpis{display:flex;flex-wrap:wrap;gap:20px}
.kpi .n{font-size:24px;color:%(caller)s}
.kpi .l{font-size:11px;color:%(muted)s}
table{width:100%%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid %(line)s}
th{color:%(muted)s;font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.nc{color:%(muted)s;font-style:italic}
.bar{fill:%(agent)s}
.foot{color:%(muted)s;font-size:11.5px;margin-top:6px}
a{color:%(agent)s}
.overflow{overflow-x:auto}
"""


def _page(title: str, description: str, body: str) -> str:
    esc = _report._esc
    css = _OBSERVE_CSS % _report._C
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title>"
        f"<meta name=\"description\" content=\"{esc(description)}\">"
        f"<style>{css}</style></head><body><div class=\"wrap\">{body}"
        "</div></body></html>\n"
    )


def _svg_pct_bars(rows: List[dict]) -> str:
    """A small inline SVG bar chart of each hop's p90 (ms). Bars scale to the
    largest measured p90; a hop with no measurement draws no bar and reads
    'not captured'. No external request, no script."""
    esc = _report._esc
    C = _report._C
    measured = [r for r in rows if r.get("p90") is not None]
    hi = max((r["p90"] for r in measured), default=1.0) or 1.0
    row_h, label_w, bar_w = 26, 200, 420
    w = label_w + bar_w + 90
    h = row_h * len(rows) + 8
    p = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
         f'aria-label="Per-hop p90 latency" font-family="ui-monospace,monospace">']
    for i, r in enumerate(rows):
        y = i * row_h + 6
        p.append(f'<text x="0" y="{y + 14}" fill="{C["muted"]}" font-size="12">'
                 f'{esc(r["label"])}</text>')
        if r.get("p90") is None:
            p.append(f'<text x="{label_w}" y="{y + 14}" fill="{C["muted"]}" '
                     f'font-size="11" font-style="italic">not captured</text>')
            continue
        bw = max(1.5, r["p90"] / hi * bar_w)
        p.append(f'<rect x="{label_w}" y="{y + 3}" width="{bw:.1f}" height="16" '
                 f'rx="3" fill="{C["agent"]}" />')
        p.append(f'<text x="{label_w + bw + 6:.1f}" y="{y + 15}" '
                 f'fill="{C["cream"]}" font-size="11">{r["p90"]:.1f} ms</text>')
    p.append("</svg>")
    return "".join(p)


def _pct_table_html(rows: List[dict], e2e: dict) -> str:
    esc = _report._esc

    def cell(x):
        return '<span class="nc">n/a</span>' if x is None else f"{x:.1f}"

    body = ["<tr><th>hop</th><th class=\"n\">p50</th><th class=\"n\">p90</th>"
            "<th class=\"n\">p99</th><th class=\"n\">n</th>"
            "<th class=\"n\">excluded</th></tr>"]
    for r in list(rows) + [{"label": "end to end", **e2e}]:
        body.append(
            f"<tr><td>{esc(r['label'])}</td>"
            f"<td class=\"n\">{cell(r['p50'])}</td>"
            f"<td class=\"n\">{cell(r['p90'])}</td>"
            f"<td class=\"n\">{cell(r['p99'])}</td>"
            f"<td class=\"n\">{r['n']}</td>"
            f"<td class=\"n\">{r['excluded_null']}</td></tr>"
        )
    return '<div class="overflow"><table>' + "".join(body) + "</table></div>"


def _tokens_table_html(rollup: dict) -> str:
    esc = _report._esc
    rows = ["<tr><th>model</th><th class=\"n\">spans</th>"]
    for c in TOKEN_CATEGORIES:
        rows[0] += f"<th class=\"n\">{c}</th>"
    rows[0] += "<th class=\"n\">est. USD</th></tr>"

    def tcell(block):
        if block["total"] is None:
            return '<td class="n nc">not captured</td>'
        return f'<td class="n">{block["total"]}</td>'

    for m in rollup["models"]:
        row = f"<tr><td>{esc(m['model'])}</td><td class=\"n\">{m['spans']}</td>"
        for c in TOKEN_CATEGORIES:
            row += tcell(m["tokens"][c])
        if m["priced"] is None:
            row += '<td class="n nc">n/a</td>'
        elif m["priced"] is False:
            row += '<td class="n nc">unpriced</td>'
        else:
            row += f'<td class="n">{m["estimated_usd"]:.6f}</td>'
        row += "</tr>"
        rows.append(row)
    return '<div class="overflow"><table>' + "".join(rows) + "</table></div>"


def build_report_html(result: dict) -> str:
    """One self-contained HTML page from a report result dict (the output of
    :func:`_report_result`). Inline CSS and SVG only; no external request and
    no wall clock."""
    esc = _report._esc
    pct = result["percentiles"]
    rollup = result["tokens"]

    kpis = [
        ("traces", pct["trace_count"]),
        ("spans", pct["span_count"]),
        ("token-bearing spans", rollup["llm_span_count"]),
    ]
    if rollup["prices"] is not None:
        usd = rollup["total_estimated_usd"]
        kpis.append(("estimated USD", "null" if usd is None else f"{usd:.4f}"))
    kpi_html = "".join(
        f'<div class="kpi"><div class="n">{esc(v)}</div>'
        f'<div class="l">{esc(k)}</div></div>' for k, v in kpis
    )

    worst_html = ""
    if result["worst_traces"]:
        def _row(w):
            e2e = w["end_to_end_ms"]
            e2e_text = "n/a" if e2e is None else f"{e2e:.1f}"
            return (
                f'<tr><td><a href="{esc(w["href"])}">{esc(w["name"])}</a></td>'
                f'<td class="n">{e2e_text} ms</td>'
                f'<td class="n">{w["span_count"]}</td></tr>'
            )
        items = "".join(_row(w) for w in result["worst_traces"])
        worst_html = (
            '<div class="card"><h2>Slowest traces</h2>'
            '<div class="overflow"><table>'
            '<tr><th>trace</th><th class="n">end to end</th>'
            '<th class="n">spans</th></tr>' + items + "</table></div></div>"
        )

    cost_card = ""
    if rollup["models"]:
        price_line = ""
        if rollup["prices"] is not None:
            usd = rollup["total_estimated_usd"]
            price_line = (
                f'<p class="foot">Total estimated cost: '
                f'{esc(rollup["prices"]["currency"])} '
                f'{"null" if usd is None else f"{usd:.6f}"} '
                f'(estimated from {esc(rollup["prices"]["table"])}). '
                "Tokens are read from the spans; the dollars are local "
                "arithmetic over your price table.</p>"
            )
        cost_card = (
            '<div class="card"><h2>Token usage per model</h2>'
            + _tokens_table_html(rollup) + price_line
            + '<p class="foot">A category no span reported reads "not '
            'captured", never 0. A model with no price row is unpriced.</p>'
            "</div>"
        )

    body = (
        f"<h1>{esc('hotato observe: ' + os.path.basename(os.path.normpath(result['dir'])))}</h1>"
        '<p class="sub">Every number on this page was derived on your machine '
        "from voice-trace spans you already have. No account, no upload.</p>"
        f'<div class="card"><div class="kpis">{kpi_html}</div></div>'
        '<div class="card"><h2>Per-hop p90 latency</h2>'
        f'<div class="overflow">{_svg_pct_bars(pct["hops"])}</div></div>'
        '<div class="card"><h2>Latency percentiles (ms)</h2>'
        f'{_pct_table_html(pct["hops"], pct["end_to_end"])}'
        f'<p class="foot">{esc(pct["method_note"])}</p></div>'
        + cost_card + worst_html
    )
    desc = (
        f"Self-contained hotato observe report over {pct['trace_count']} "
        f"trace(s): per-hop and end-to-end latency percentiles and per-model "
        "token totals, every value derived locally and offline."
    )
    return _page("hotato observe report", desc, body)


# --- report ---------------------------------------------------------------

def _pool_tokens(traces: List[dict], *, prices: Optional[str]) -> dict:
    """Cost rollup over the concatenated spans of every trace in the folder."""
    pooled = {"spans": []}
    for vt in traces:
        pooled["spans"].extend(vt.get("spans") or [])
    return cost_rollup(pooled, prices=prices)


def _report_result(dir_path: str, *, prices: Optional[str], out_path: str) -> dict:
    traces, _skipped = _load_traces(dir_path)
    pct = percentiles_over_dir(dir_path)
    rollup = _pool_tokens(traces, prices=prices)

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    ranked = []
    for vt in traces:
        ranked.append({
            "path": vt["_path"],
            "name": os.path.basename(vt["_path"]),
            "href": os.path.relpath(vt["_path"], out_dir),
            "end_to_end_ms": _trace_end2end_ms(vt),
            "span_count": len(vt.get("spans") or []),
        })
    ranked.sort(key=lambda w: (w["end_to_end_ms"] is not None,
                               w["end_to_end_ms"] or 0.0), reverse=True)
    worst = ranked[:5]

    return {
        "dir": dir_path,
        "out_path": out_path,
        "percentiles": pct,
        "tokens": rollup,
        "worst_traces": worst,
    }


def build_report(dir_path: str, *, out_path: str, prices: Optional[str] = None,
                 force: bool = False) -> dict:
    """Build the observe report over ``dir_path`` and write the self-contained
    HTML to ``out_path``. Raises ``ValueError`` (CLI exit 2) on a missing
    folder, no readable traces, an unreadable price table, or an existing
    ``out_path`` without ``force``."""
    if os.path.exists(out_path) and not force:
        raise ValueError(
            f"{out_path!r} already exists; pass --force to overwrite it, or "
            "choose a new --out"
        )
    result = _report_result(dir_path, prices=prices, out_path=out_path)
    html = build_report_html(result)
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:  # write-mode: FIFO-safe
        fh.write(html)
    result["bytes"] = len(html.encode("utf-8"))
    return result


def render_report_text(result: dict) -> str:
    pct = result["percentiles"]
    lines = [
        f"observe report: {result['out_path']}",
        f"  traces:  {pct['trace_count']}",
        f"  spans:   {pct['span_count']}",
        "  worst:   " + (
            ", ".join(w["name"] for w in result["worst_traces"]) or "(none)"
        ),
    ]
    return "\n".join(lines)


def report_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "observe-report", "schema_version": "1",
        "dir": result["dir"], "out_path": result["out_path"],
        "trace_count": result["percentiles"]["trace_count"],
        "span_count": result["percentiles"]["span_count"],
        "worst_traces": result["worst_traces"],
    }


# --- shared render helpers ------------------------------------------------

def _waterfall_text_lines(waterfall: dict) -> List[str]:
    lines = []
    for hop in waterfall["hops"]:
        if hop["ms"] is None:
            lines.append(f"{hop['label']:26s}: not captured ({hop['basis']})")
        else:
            lines.append(f"{hop['label']:26s}: {hop['ms']:.1f} ms")
    return lines
