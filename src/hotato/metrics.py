"""``hotato.metrics``: custom metric plugins, written as plain Python functions.

A team's own checks run next to hotato's built-in ones and are reported the
same way: in ``hotato assert run``, in a conversation test's two lanes, in the
HTML/Markdown report and in CI exit codes. A plugin is a function that reads
the call's data (transcript turns, ``hotato.voice_trace.v1`` spans, the scored
timing events: the same inputs the built-in assertion kinds and the rubric
lane read) and returns ``pass`` / ``fail`` / ``unknown`` with evidence.

Discovery, pytest style:

* every ``*.py`` file (not starting with ``_``) in a ``hotato_metrics/`` folder
  in the current directory, or in a folder/file passed with ``--metrics PATH``;
  every module-level function whose name starts with ``metric_`` is a metric
  (the ``metric_`` prefix is dropped to form its name), and the optional
  :func:`metric` decorator sets its name, inputs and labels explicitly;
* installed packages that declare an entry point in the ``hotato.metrics``
  group (the entry point names a module, scanned the same way, or one
  decorated function).

The CLI loads plugin code only when a document actually references a custom
metric (``kind: custom``) or when you list them with ``hotato assert metrics``:
loading runs the plugin files, exactly like pytest importing a test module, so
only point it at code you trust.

hotato's rules hold for custom metrics too, enforced here rather than left to
the plugin author:

* **Two lanes, never blended.** A metric is ``deterministic`` (the default) or
  ``judged`` (it calls a model, or anything else that can vary run to run). A
  deterministic metric runs in the ``assert.v1`` lane as ``kind: custom`` and
  is counted with the deterministic checks. A judged metric runs only in the
  rubric lane (``rubric.v1``, ``deterministic: false``), advisory unless the run
  opts into gating; referenced from the deterministic lane it is routed out as
  INCONCLUSIVE, never counted there.
* **Deterministic is checked, not assumed.** hotato calls a deterministic
  metric twice on independent copies of the same input. Two different answers
  is a usage error (exit 2) that names the metric, never a verdict.
* **A pass needs evidence, and the evidence must match the call.** Every cited
  turn, quote, span or timing field is looked up in the call's data. A pass
  with no evidence, or any result whose evidence does not match, is reported
  INCONCLUSIVE with the reason, never as the plugin's verdict.
* **Missing data is not guessed.** A metric declares the inputs it ``needs``;
  when one was not supplied the metric is not called and the result is
  INCONCLUSIVE (``--inconclusive-policy refuse`` turns that into exit 2). A
  plugin that fails to import, raises, or returns something malformed stops
  the run with exit 2 before any verdict is printed (in the rubric lane, a
  judged metric that raises is an ERROR, which refuses with exit 2 when the
  run is gated -- the same contract a model judge that could not run has).

The core stays stdlib-only: this module imports nothing outside the standard
library, and plugins import whatever they like.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "ENTRY_POINT_GROUP",
    "DEFAULT_DIR",
    "INPUTS",
    "LANES",
    "Call",
    "MetricResult",
    "MetricSpec",
    "MetricError",
    "metric",
    "passed",
    "failed",
    "unknown",
    "load_plugins",
    "registry",
    "get_metric",
    "clear_registry",
    "configure_autoload",
    "ensure_loaded",
    "run_metric",
    "list_entries",
]

ENTRY_POINT_GROUP = "hotato.metrics"
DEFAULT_DIR = "hotato_metrics"
FUNCTION_PREFIX = "metric_"
# The call data a metric may declare it needs. Mirrors assert_.Context: an input
# that was never supplied is None (absent), distinct from an empty list.
INPUTS = ("transcript", "spans", "timing")
LANES = ("deterministic", "judged")
_STATUSES = ("pass", "fail", "unknown")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_REASON_MAX = 500
_QUOTE_MAX = 300


class MetricError(ValueError):
    """A plugin could not be loaded or did not keep the plugin contract (bad
    return value, an exception, two different answers on the same input). A
    ``ValueError`` so every CLI surface maps it to its existing exit-2 usage
    path: hotato refuses rather than guessing a verdict."""


# =========================================================================
# What a plugin returns
# =========================================================================

@dataclass(frozen=True)
class MetricResult:
    """One metric verdict: ``status`` is ``pass`` / ``fail`` / ``unknown``,
    ``evidence`` is a list of citations into the call (see :class:`Call`'s
    ``cite_*`` helpers), ``reason`` is a short sentence, and ``measured`` is an
    optional mapping of numbers/strings the metric computed (reported as is)."""

    status: str
    evidence: Tuple[Dict[str, Any], ...] = ()
    reason: Optional[str] = None
    measured: Optional[Dict[str, Any]] = None


def passed(evidence: Sequence[Dict[str, Any]], reason: Optional[str] = None,
           measured: Optional[Dict[str, Any]] = None) -> MetricResult:
    """A pass. ``evidence`` is required: a pass without it is reported
    INCONCLUSIVE."""
    return MetricResult("pass", tuple(evidence or ()), reason, measured)


def failed(reason: str, evidence: Sequence[Dict[str, Any]] = (),
           measured: Optional[Dict[str, Any]] = None) -> MetricResult:
    """A fail, with a reason and (optionally) the evidence that shows it."""
    return MetricResult("fail", tuple(evidence or ()), reason, measured)


def unknown(reason: str, measured: Optional[Dict[str, Any]] = None) -> MetricResult:
    """The metric cannot tell from this call's data. Reported INCONCLUSIVE."""
    return MetricResult("unknown", (), reason, measured)


# =========================================================================
# What a plugin receives
# =========================================================================

class Call:
    """The call's data, as a metric sees it. Every attribute is a private deep
    copy, so a metric can never change what another check reads.

    * ``transcript``: list of ``{"role", "text", "start", "end"}`` turns, or
      ``None`` when no transcript was supplied.
    * ``spans``: list of ``hotato.voice_trace.v1`` span dicts, or ``None``.
    * ``timing``: the scored run's events (``envelope.v1`` ``events``), or
      ``None``.
    * ``params``: the ``params`` mapping from the assertion that referenced the
      metric (empty when none was given).
    """

    def __init__(self, transcript: Any = None, spans: Any = None,
                 timing: Any = None, params: Optional[Dict[str, Any]] = None) -> None:
        self.transcript = copy.deepcopy(transcript)
        self.spans = copy.deepcopy(spans)
        self.timing = copy.deepcopy(timing)
        self.params = copy.deepcopy(dict(params or {}))

    # -- reading helpers ---------------------------------------------------
    def turns(self, role: Optional[str] = None) -> List[Tuple[int, Dict[str, Any]]]:
        """``(index, turn)`` pairs, optionally only one ``role``. The index is
        what :meth:`cite_turn` takes."""
        return [(i, t) for i, t in enumerate(self.transcript or [])
                if role is None or t.get("role") == role]

    def spans_of(self, span_type: str, name: Optional[str] = None
                 ) -> List[Tuple[int, Dict[str, Any]]]:
        """``(index, span)`` pairs of one span ``type`` (optionally one
        ``name``), in trace order. The index is what :meth:`cite_span` takes."""
        return [(i, s) for i, s in enumerate(self.spans or [])
                if s.get("type") == span_type and (name is None or s.get("name") == name)]

    def events(self) -> List[Dict[str, Any]]:
        """The timing events as a list (a single event dict becomes a list of
        one; absent timing is an empty list)."""
        if self.timing is None:
            return []
        return list(self.timing) if isinstance(self.timing, list) else [self.timing]

    # -- citation helpers ----------------------------------------------------
    @staticmethod
    def cite_turn(index: int, quote: Optional[str] = None) -> Dict[str, Any]:
        """Cite transcript turn ``index``; ``quote`` (optional) must occur in
        that turn's text."""
        out: Dict[str, Any] = {"turn": index}
        if quote is not None:
            out["quote"] = quote
        return out

    @staticmethod
    def cite_span(index: int) -> Dict[str, Any]:
        """Cite the span at ``index`` (hotato's span id ``s_<index>``)."""
        return {"span_id": f"s_{index}"}

    @staticmethod
    def cite_timing(event: int, field_path: str, value: Any) -> Dict[str, Any]:
        """Cite timing event ``event``'s dotted ``field_path`` holding
        ``value``."""
        return {"event": event, "field": field_path, "value": value}


# =========================================================================
# The registry
# =========================================================================

@dataclass
class MetricSpec:
    """A registered metric: its function plus the labels hotato reports."""

    name: str
    func: Callable[..., Any]
    lane: str = "deterministic"
    needs: Tuple[str, ...] = ()
    description: str = ""
    dimension: Optional[str] = None
    model: Optional[str] = None
    version: int = 1
    source: str = ""
    source_sha256: Optional[str] = None
    takes_params: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def judged(self) -> bool:
        return self.lane == "judged"

    def entry(self) -> Dict[str, Any]:
        """The listing row ``hotato assert metrics`` prints."""
        out: Dict[str, Any] = {
            "name": self.name,
            "lane": self.lane,
            "needs": list(self.needs),
            "source": self.source,
            "source_sha256": self.source_sha256,
            "version": self.version,
        }
        if self.description:
            out["description"] = self.description
        if self.dimension:
            out["dimension"] = self.dimension
        if self.model:
            out["model"] = self.model
        return out


_REPORT_DIMENSIONS = ("outcome", "policy", "conversation", "speech", "reliability")
_METRIC_ATTR = "__hotato_metric__"

_REGISTRY: Dict[str, MetricSpec] = {}
_LOADED_SOURCES: List[str] = []
_AUTOLOAD: Optional[Dict[str, Any]] = None


def metric(name: Optional[str] = None, *, judged: bool = False,
           needs: Sequence[str] = (), description: Optional[str] = None,
           dimension: Optional[str] = None, model: Optional[str] = None,
           temperature: Optional[float] = None, version: int = 1
           ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a function as a hotato metric and set its labels.

    * ``name``: the id an assertion references (default: the function name with
      a leading ``metric_`` dropped).
    * ``judged``: ``True`` for a metric that calls a model or can otherwise vary
      between runs. Judged metrics run only in the rubric lane and must declare
      the pinned ``model`` id they call and ``temperature=0``.
    * ``needs``: which of ``transcript`` / ``spans`` / ``timing`` must be present;
      when one is absent the metric is not called and reports INCONCLUSIVE.
    * ``description``: one line shown in listings and reports (default: the
      first line of the docstring).
    * ``dimension``: an optional report dimension (``outcome``, ``policy``,
      ``conversation``, ``speech``, ``reliability``) used when the assertion
      does not set its own.
    * ``version``: bump when the metric's logic changes (reported as provenance).

    The function is returned unchanged, so it stays directly unit-testable."""
    def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, _METRIC_ATTR, {
            "name": name, "judged": judged, "needs": tuple(needs),
            "description": description, "dimension": dimension, "model": model,
            "temperature": temperature, "version": version,
        })
        return func
    return wrap


def _display_source(path: str) -> str:
    """A share-safe label for a plugin file: relative to the current directory
    when it sits under it, else just its file name (never an absolute path)."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        rel = os.path.basename(path)
    if rel.startswith(".."):
        rel = os.path.basename(path)
    return rel.replace(os.sep, "/")


def _file_sha256(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    from .errors import open_regular  # FIFO-safe read of a user-supplied path

    h = hashlib.sha256()
    with open_regular(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _accepts_params(func: Callable[..., Any], label: str) -> bool:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError) as exc:
        raise MetricError(f"custom metric {label}: cannot read its signature: {exc}") from exc
    positional = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    required = [p for p in positional if p.default is p.empty]
    if len(positional) == 1 and len(required) == 1:
        return False
    if len(positional) == 2 and len(required) >= 1:
        return True
    raise MetricError(
        f"custom metric {label}: must take (call) or (call, params), got "
        f"{len(positional)} positional parameter(s)"
    )


def _spec_from_function(func: Callable[..., Any], *, default_name: str,
                        source: str, source_sha256: Optional[str]) -> MetricSpec:
    meta = dict(getattr(func, _METRIC_ATTR, None) or {})
    name = meta.get("name") or default_name
    label = repr(name)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise MetricError(
            f"custom metric {label} in {source}: a name is 1-64 characters, "
            "starts with a letter, and uses letters, digits, '_', '.', '-'"
        )
    judged = bool(meta.get("judged", False))
    needs = tuple(meta.get("needs") or ())
    bad = [n for n in needs if n not in INPUTS]
    if bad:
        raise MetricError(
            f"custom metric {label} in {source}: 'needs' entries must be drawn "
            f"from {INPUTS}, got {bad}"
        )
    dimension = meta.get("dimension")
    if dimension is not None and dimension not in _REPORT_DIMENSIONS:
        raise MetricError(
            f"custom metric {label} in {source}: 'dimension' must be one of "
            f"{_REPORT_DIMENSIONS}, got {dimension!r}"
        )
    version = meta.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise MetricError(f"custom metric {label} in {source}: 'version' must be an integer >= 1")
    model = meta.get("model")
    if judged:
        if not isinstance(model, str) or not model.strip() or model.strip() == "latest" \
                or model.strip().endswith(":latest"):
            raise MetricError(
                f"custom metric {label} in {source}: a judged metric must declare "
                "the pinned model id it calls (model='...'), never 'latest'"
            )
        if meta.get("temperature") != 0:
            raise MetricError(
                f"custom metric {label} in {source}: a judged metric must declare "
                "temperature=0 (hotato's judge rule)"
            )
    elif model is not None:
        raise MetricError(
            f"custom metric {label} in {source}: 'model' is only for judged "
            "metrics; a metric that calls a model must set judged=True"
        )
    description = meta.get("description")
    if description is None:
        doc = inspect.getdoc(func) or ""
        description = doc.strip().splitlines()[0] if doc.strip() else ""
    return MetricSpec(
        name=name, func=func, lane="judged" if judged else "deterministic",
        needs=needs, description=str(description)[:200], dimension=dimension,
        model=model.strip() if isinstance(model, str) else None, version=version,
        source=source, source_sha256=source_sha256,
        takes_params=_accepts_params(func, label),
    )


def _register(spec: MetricSpec) -> None:
    prior = _REGISTRY.get(spec.name)
    if prior is not None and prior.func is not spec.func:
        raise MetricError(
            f"custom metric {spec.name!r} is defined twice ({prior.source} and "
            f"{spec.source}); metric names must be unique"
        )
    _REGISTRY[spec.name] = spec


def _scan_module(module: Any, source: str, source_sha256: Optional[str]) -> int:
    found = 0
    for attr, obj in sorted(vars(module).items()):
        if not inspect.isfunction(obj) or obj.__module__ != module.__name__:
            continue
        if not (attr.startswith(FUNCTION_PREFIX) or hasattr(obj, _METRIC_ATTR)):
            continue
        default = attr[len(FUNCTION_PREFIX):] if attr.startswith(FUNCTION_PREFIX) else attr
        _register(_spec_from_function(obj, default_name=default, source=source,
                                      source_sha256=source_sha256))
        found += 1
    return found


def _load_file(path: str) -> int:
    path = os.path.abspath(path)
    source = _display_source(path)
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    mod_name = f"_hotato_metrics_plugin_{digest}"
    if mod_name in sys.modules:
        return _scan_module(sys.modules[mod_name], source, _file_sha256(path))
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise MetricError(f"custom metric plugin {source}: not a loadable Python file")
    module = importlib.util.module_from_spec(spec)
    folder = os.path.dirname(path)
    added = folder not in sys.path
    if added:
        sys.path.insert(0, folder)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken plugin refuses the run (exit 2)
        sys.modules.pop(mod_name, None)
        raise MetricError(
            f"custom metric plugin {source} failed to import: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if added:
            try:
                sys.path.remove(folder)
            except ValueError:
                pass
    return _scan_module(module, source, _file_sha256(path))


def _plugin_files(path: str) -> List[str]:
    if os.path.isfile(path):
        if not path.endswith(".py"):
            raise MetricError(f"--metrics {path!r}: a plugin file must end in .py")
        return [path]
    if os.path.isdir(path):
        return [os.path.join(path, n) for n in sorted(os.listdir(path))
                if n.endswith(".py") and not n.startswith("_")
                and os.path.isfile(os.path.join(path, n))]
    raise MetricError(f"--metrics {path!r}: no such file or folder")


def _entry_points() -> List[Any]:
    from importlib import metadata

    eps = metadata.entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group=ENTRY_POINT_GROUP))
    return list(eps.get(ENTRY_POINT_GROUP, []))  # Python 3.9


def _load_entry_point(ep: Any) -> int:
    source = f"entry-point:{ep.name}"
    try:
        obj = ep.load()
    except Exception as exc:
        raise MetricError(
            f"custom metric entry point {ep.name!r} ({ep.value}) failed to load: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if inspect.ismodule(obj):
        return _scan_module(obj, source, _file_sha256(getattr(obj, "__file__", None)))
    if inspect.isfunction(obj):
        try:
            src_file = inspect.getsourcefile(obj)
        except TypeError:
            src_file = None
        _register(_spec_from_function(obj, default_name=ep.name, source=source,
                                      source_sha256=_file_sha256(src_file)))
        return 1
    raise MetricError(
        f"custom metric entry point {ep.name!r} must name a module or a function"
    )


def load_plugins(paths: Iterable[str] = (), *, default_dir: bool = True,
                 entry_points: bool = True, cwd: Optional[str] = None) -> int:
    """Load metric plugins into the registry and return how many metrics were
    found. ``paths`` are plugin files or folders; ``default_dir`` also loads
    ``./hotato_metrics/`` (relative to ``cwd``) when it exists; ``entry_points``
    also loads installed packages' ``hotato.metrics`` entry points. Raises
    :class:`MetricError` for a missing path, an import failure, a duplicate
    name or a malformed declaration."""
    found = 0
    targets = list(paths or ())
    if default_dir:
        d = os.path.join(cwd or os.getcwd(), DEFAULT_DIR)
        if os.path.isdir(d) and os.path.abspath(d) not in [os.path.abspath(t) for t in targets]:
            targets.append(d)
    for target in targets:
        for f in _plugin_files(target):
            found += _load_file(f)
        if target not in _LOADED_SOURCES:
            _LOADED_SOURCES.append(target)
    if entry_points:
        for ep in _entry_points():
            found += _load_entry_point(ep)
    return found


def registry() -> Dict[str, MetricSpec]:
    """The registered metrics, by name (a copy)."""
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Forget every registered metric and any pending autoload (for tests and
    long-lived processes that reload plugins)."""
    global _AUTOLOAD
    _REGISTRY.clear()
    _LOADED_SOURCES.clear()
    _AUTOLOAD = None


def configure_autoload(paths: Iterable[str] = (), *, default_dir: bool = True,
                       entry_points: bool = True, cwd: Optional[str] = None) -> None:
    """Arrange for :func:`load_plugins` to run the first time a document
    references a custom metric. The CLI calls this, so plugin code runs only
    when something actually asks for a custom metric. Missing ``paths`` are
    reported immediately (no plugin code runs for that check)."""
    global _AUTOLOAD
    paths = list(paths or ())
    for p in paths:
        if not os.path.exists(p):
            raise MetricError(f"--metrics {p!r}: no such file or folder")
    _AUTOLOAD = {"paths": paths, "default_dir": default_dir,
                 "entry_points": entry_points, "cwd": cwd}


def ensure_loaded() -> None:
    """Run a pending autoload (once)."""
    global _AUTOLOAD
    pending, _AUTOLOAD = _AUTOLOAD, None
    if pending is not None:
        load_plugins(pending["paths"], default_dir=pending["default_dir"],
                     entry_points=pending["entry_points"], cwd=pending["cwd"])


def get_metric(name: str) -> MetricSpec:
    """The registered metric called ``name``, running a pending autoload first.
    An unknown name is a :class:`MetricError` (exit 2) naming where hotato
    looked."""
    if name not in _REGISTRY:
        ensure_loaded()
    spec = _REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise MetricError(
            f"unknown custom metric {name!r} (registered: {known}). Put plugin "
            f"files in ./{DEFAULT_DIR}/, pass --metrics PATH, or install a "
            f"package with a {ENTRY_POINT_GROUP!r} entry point"
        )
    return spec


def list_entries() -> List[Dict[str, Any]]:
    """Every registered metric's listing row, sorted by name."""
    ensure_loaded()
    return [_REGISTRY[n].entry() for n in sorted(_REGISTRY)]


# =========================================================================
# Running one metric
# =========================================================================

def _norm_text(s: str) -> str:
    return " ".join(str(s).split()).lower()


def _resolve_path(obj: Any, path: str) -> Tuple[Any, bool]:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def _check_evidence(item: Any, call: Call) -> Optional[str]:
    """``None`` when one citation matches the call's data, else why not."""
    if not isinstance(item, dict) or not item:
        return "an evidence item must be a non-empty mapping"
    if "turn" in item:
        idx = item["turn"]
        turns = call.transcript
        if turns is None:
            return "cites a transcript turn but no transcript was supplied"
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(turns):
            return f"cites transcript turn {idx!r}, which does not exist"
        quote = item.get("quote")
        if quote is not None:
            if not isinstance(quote, str) or not quote.strip():
                return f"turn {idx}: a quote must be a non-empty string"
            if _norm_text(quote) not in _norm_text(turns[idx].get("text") or ""):
                return f"turn {idx}: the quoted text does not occur in that turn"
        return None
    if "span_id" in item:
        sid = item["span_id"]
        m = re.match(r"^s_(\d+)$", sid) if isinstance(sid, str) else None
        if call.spans is None:
            return "cites a span but no trace was supplied"
        if m is None or int(m.group(1)) >= len(call.spans):
            return f"cites span {sid!r}, which does not exist in the trace"
        return None
    if "event" in item:
        ev = item["event"]
        events = call.events()
        if call.timing is None:
            return "cites a timing event but no timing was supplied"
        if isinstance(ev, bool) or not isinstance(ev, int) or not 0 <= ev < len(events):
            return f"cites timing event {ev!r}, which does not exist"
        path = item.get("field")
        if not isinstance(path, str) or not path:
            return f"timing event {ev}: a 'field' path is required"
        val, found = _resolve_path(events[ev], path)
        if not found:
            return f"timing event {ev} has no field {path!r}"
        if "value" in item and val != item["value"]:
            return f"timing event {ev} field {path!r} does not hold the cited value"
        return None
    return "an evidence item must cite a 'turn', a 'span_id' or a timing 'event'"


def _public_evidence(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The checked citations as reported: only the citation keys, quotes
    clipped, so a report carries what was cited and nothing else."""
    out = []
    for it in items:
        if "turn" in it:
            row: Dict[str, Any] = {"turn": it["turn"]}
            if it.get("quote") is not None:
                row["quote"] = str(it["quote"])[:_QUOTE_MAX]
        elif "span_id" in it:
            row = {"span_id": it["span_id"]}
        else:
            row = {"event": it["event"], "field": it["field"]}
            if "value" in it:
                row["value"] = it["value"]
        out.append(row)
    return out


def _coerce(raw: Any, label: str) -> MetricResult:
    if isinstance(raw, MetricResult):
        res = raw
    elif isinstance(raw, dict):
        unknown_keys = set(raw) - {"status", "evidence", "reason", "measured"}
        if unknown_keys:
            raise MetricError(
                f"custom metric {label} returned unknown key(s) {sorted(unknown_keys)}; "
                "return passed()/failed()/unknown() or a dict with status, "
                "evidence, reason, measured"
            )
        ev = raw.get("evidence") or ()
        if not isinstance(ev, (list, tuple)):
            raise MetricError(f"custom metric {label}: 'evidence' must be a list")
        res = MetricResult(raw.get("status"), tuple(ev), raw.get("reason"),
                           raw.get("measured"))
    else:
        raise MetricError(
            f"custom metric {label} returned {type(raw).__name__}; return "
            "hotato.metrics.passed(...), failed(...) or unknown(...) (a bare "
            "bool carries no evidence)"
        )
    if res.status not in _STATUSES:
        raise MetricError(
            f"custom metric {label}: status must be one of {_STATUSES}, got {res.status!r}"
        )
    if res.reason is not None and not isinstance(res.reason, str):
        raise MetricError(f"custom metric {label}: 'reason' must be a string")
    if res.measured is not None:
        if not isinstance(res.measured, dict):
            raise MetricError(f"custom metric {label}: 'measured' must be a mapping")
        try:
            json.dumps(res.measured, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise MetricError(
                f"custom metric {label}: 'measured' must be plain JSON values: {exc}"
            ) from exc
    return res


def _canonical(res: MetricResult) -> str:
    return json.dumps(
        {"status": res.status, "evidence": list(res.evidence), "reason": res.reason,
         "measured": res.measured},
        sort_keys=True, default=repr,
    )


def _invoke(spec: MetricSpec, call: Call) -> Any:
    if spec.takes_params:
        return spec.func(call, call.params)
    return spec.func(call)


def run_metric(spec: MetricSpec, *, transcript: Any = None, spans: Any = None,
               timing: Any = None, params: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    """Run one metric against the call's data and return a checked outcome:
    ``{"status": PASS|FAIL|INCONCLUSIVE, "reason"?, "evidence", "measured"?}``.

    Missing ``needs`` -> INCONCLUSIVE without calling the metric. A
    deterministic metric is called twice on independent copies; different
    answers raise :class:`MetricError`. A pass with no evidence, or any
    result whose evidence does not match the call, becomes INCONCLUSIVE with
    the reason. An exception or malformed return raises :class:`MetricError`."""
    label = repr(spec.name)
    supplied = {"transcript": transcript, "spans": spans, "timing": timing}
    missing = [n for n in spec.needs if supplied[n] is None]
    if missing:
        noun = {"transcript": "transcript", "spans": "trace", "timing": "timing"}
        return {
            "status": "INCONCLUSIVE",
            "reason": (f"custom metric {spec.name} needs "
                       f"{' and '.join(noun[m] for m in missing)}, which was not "
                       "supplied; the metric was not run"),
            "evidence": [],
            "missing": missing,
        }

    def _once() -> Tuple[MetricResult, Call]:
        call = Call(transcript, spans, timing, params)
        try:
            raw = _invoke(spec, call)
        except MetricError:
            raise
        except Exception as exc:
            raise MetricError(
                f"custom metric {label} raised {type(exc).__name__}: {exc}"
            ) from exc
        return _coerce(raw, label), call

    res, call = _once()
    if not spec.judged:
        again, _ = _once()
        if _canonical(again) != _canonical(res):
            raise MetricError(
                f"custom metric {label} returned different results on two calls "
                "over the same input, so it is not deterministic; make it "
                "deterministic or declare it judged=True (rubric lane)"
            )

    status = {"pass": "PASS", "fail": "FAIL", "unknown": "INCONCLUSIVE"}[res.status]
    reason = (res.reason or "")[:_REASON_MAX] or None
    problems = [p for p in (_check_evidence(it, call) for it in res.evidence) if p]
    out: Dict[str, Any] = {"status": status, "evidence": []}
    if problems:
        out["status"] = "INCONCLUSIVE"
        out["reason"] = (f"custom metric {spec.name} returned {res.status} but its "
                         f"evidence does not match the call: {problems[0]}")
        out["claimed_status"] = status
    elif status == "PASS" and not res.evidence:
        out["status"] = "INCONCLUSIVE"
        out["reason"] = (f"custom metric {spec.name} returned pass without evidence; "
                         "a pass needs at least one citation")
        out["claimed_status"] = "PASS"
    else:
        out["evidence"] = _public_evidence(res.evidence)
        if reason:
            out["reason"] = reason
        elif status == "INCONCLUSIVE":
            out["reason"] = f"custom metric {spec.name} could not tell from this call"
    if res.measured is not None:
        out["measured"] = res.measured
    return out
