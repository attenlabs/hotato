#!/usr/bin/env python3
"""Deterministic static builder for the Voice Failure Atlas (delta D5).

Reads ONLY the typed sources under ``atlas/{records,contracts,implementations}/``
and renders a static page graph, machine indexes, and discovery files. Follows
``examples/render_examples.py``'s determinism discipline: stdlib-only, no
wall-clock, no randomness, alphabetical ordering throughout, so two runs on
the same sources are byte-identical (CI renders twice and diffs; see
``tests/test_atlas.py``).

HONESTY INVARIANT: a typed source under ``atlas/records/`` never carries a
pre-baked capability verdict. This builder computes every capability
requirement by calling the real router
(``hotato.capability_routing.route_capability``) over the record's
``routing_fixture`` plus its ``paired_with`` siblings' ``routing_fixture``
events. A source file cannot assert a routing outcome the router itself did
not derive, and the backchannel-exclusion rule (an addressed backchannel
routes to ``turn_intent_discriminator``, never ``utterance_addressee_gate``)
is therefore enforced by code, not by trusting the source.

Usage:

    python scripts/build_atlas.py            # render into _atlas_site/
    python scripts/build_atlas.py OUT_DIR     # render into OUT_DIR
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "src"))

from hotato import capability_routing as cr  # noqa: E402

try:
    from hotato.report import _C as _C  # noqa: E402
except Exception:  # pragma: no cover - same fallback discipline as serve/render.py
    _C = {
        "bg": "#1b1714", "card": "#241f1a", "card2": "#2b241d", "line": "#3a3128",
        "cream": "#f1e8d7", "muted": "#b7ab97", "mono": "#f6eddd",
        "caller": "#ead9a6", "agent": "#7fb2c4", "ember": "#f0663a",
        "green": "#74c98a", "red": "#e0664f", "grid": "#463b30",
    }

ATLAS_SRC = os.path.join(REPO, "atlas")
DEFAULT_OUT = os.path.join(REPO, "_atlas_site")

# The stacks hotato's own CLI recognizes (--stack {generic,vapi,twilio,livekit,
# pipecat,retell}). Every stack gets an /integrations/{stack}/ page -- most
# will show an honest "0 records captured on this stack yet" state rather
# than only existing once a record happens to reference them, so the page
# graph is a complete, dead-link-free template from the first build.
KNOWN_STACKS = ("generic", "vapi", "twilio", "livekit", "pipecat", "retell")

_STATUS_COLOR = {"PASS": "green", "FAIL": "red", "INCONCLUSIVE": "ember",
                 "ERROR": "red", "NOT_RUN": "muted", "UNAVAILABLE": "muted"}

# Free text (title/summary/approach) across every typed source is checked
# against this blocklist: a Hotato verdict never names an implementation,
# product, or vendor. Word-ish boundaries avoid flagging unrelated substrings.
_NEUTRALITY_BLOCKLIST = [
    re.compile(r"\bsaa\b", re.IGNORECASE),
    re.compile(r"attention\s*labs", re.IGNORECASE),
    re.compile(r"attenlabs", re.IGNORECASE),
    re.compile(r"multivox", re.IGNORECASE),
    re.compile(r"speech\s+addressee\s+agent", re.IGNORECASE),
]

# Authenticity-protest words stripped from all copy this builder writes or
# renders; typed sources are linted for them too (see tests/test_atlas.py).
_AUTHENTICITY_PROTEST = [
    re.compile(r"\bactual(ly)?\b", re.IGNORECASE),
    re.compile(r"\bhonest(ly)?\b", re.IGNORECASE),
    re.compile(r"\bgenuine(ly)?\b", re.IGNORECASE),
    re.compile(r"\btruly\b", re.IGNORECASE),
    re.compile(r"no fabrication", re.IGNORECASE),
]

PATTERN_MIN_RECORDS = 3
PATTERN_MIN_CONFIGURATIONS = 2

CAPABILITY_SLUG = {
    "engagement_control": "engagement-control",
    "utterance_addressee_gate": "utterance-addressee-gate",
    "turn_intent_discriminator": "turn-intent-discriminator",
}


class AtlasBuildError(RuntimeError):
    """A typed source failed a structural, digest, or honesty check."""


# =========================================================================
# loading + integrity
# =========================================================================

def _esc(x: Any) -> str:
    return _html.escape("" if x is None else str(x))


def _canonical_digest(doc: Dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "content_digest"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canon).hexdigest()


def _load_dir(subdir: str, required_kind: str) -> List[Dict[str, Any]]:
    path = os.path.join(ATLAS_SRC, subdir)
    docs = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        full = os.path.join(path, name)
        with open(full, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("kind") != required_kind:
            raise AtlasBuildError(f"{full}: kind must be {required_kind!r}, got {doc.get('kind')!r}")
        digest = doc.get("content_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise AtlasBuildError(f"{full}: missing or malformed content_digest")
        recomputed = _canonical_digest(doc)
        if digest != recomputed:
            raise AtlasBuildError(
                f"{full}: content_digest mismatch (stored {digest}, recomputed {recomputed}) "
                "-- the source was edited without recomputing its digest"
            )
        docs.append(doc)
    return docs


def _lint_neutrality(label: str, text: Optional[str]) -> None:
    if not text:
        return
    for pat in _NEUTRALITY_BLOCKLIST:
        if pat.search(text):
            raise AtlasBuildError(f"{label}: neutrality violation, matched {pat.pattern!r} in {text!r}")


def _lint_authenticity(label: str, text: Optional[str]) -> None:
    if not text:
        return
    for pat in _AUTHENTICITY_PROTEST:
        if pat.search(text):
            raise AtlasBuildError(f"{label}: authenticity-protest wording, matched {pat.pattern!r} in {text!r}")


def _lint_text_fields(label: str, doc: Dict[str, Any], fields: List[str]) -> None:
    for f in fields:
        v = doc.get(f)
        _lint_neutrality(f"{label}.{f}", v)
        _lint_authenticity(f"{label}.{f}", v)


def load_sources() -> Dict[str, List[Dict[str, Any]]]:
    records = _load_dir("records", "hotato.atlas-record.v1")
    contracts = _load_dir("contracts", "hotato.atlas-contract.v1")
    implementations = _load_dir("implementations", "hotato.atlas-implementation.v1")

    for r in records:
        _lint_text_fields(f"record:{r['content_id']}", r, ["title", "summary"])
        _lint_text_fields(f"record:{r['content_id']}.release",
                          r["release"], ["consent_and_rights_attestation"])
    for c in contracts:
        _lint_text_fields(f"contract:{c['family']}", c, ["title", "summary"])
    for i in implementations:
        _lint_text_fields(f"implementation:{i['implementation_id']}", i, ["title", "approach"])
        for point in i.get("integration_points", []):
            _lint_neutrality(f"implementation:{i['implementation_id']}.integration_points", point)
            _lint_authenticity(f"implementation:{i['implementation_id']}.integration_points", point)

    return {"records": records, "contracts": contracts, "implementations": implementations}


# =========================================================================
# capability routing -- computed, never read from a typed source
# =========================================================================

def _routing_events_for(record: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    events = [record["routing_fixture"]]
    for sibling_id in sorted(record.get("paired_with") or []):
        sibling = by_id.get(sibling_id)
        if sibling is None:
            raise AtlasBuildError(
                f"record:{record['content_id']}: paired_with references unknown "
                f"content_id {sibling_id!r}"
            )
        events.append(sibling["routing_fixture"])
    return events


def compute_capability_verdicts(records: List[Dict[str, Any]]) -> Dict[str, Optional[Dict[str, Any]]]:
    """content_id -> the real hotato.capability-requirement.v1 verdict (or
    None), derived by calling the shipped router. Never fabricated, never
    read from the source."""
    by_id = {r["content_id"]: r for r in records}
    verdicts: Dict[str, Optional[Dict[str, Any]]] = {}
    for r in records:
        events = _routing_events_for(r, by_id)
        try:
            verdict = cr.route_capability(events, contract_uri=cr.DEFAULT_CONTRACT_URI)
        except cr.RoutingInputError as exc:
            raise AtlasBuildError(f"record:{r['content_id']}: routing input error: {exc}") from exc
        verdicts[r["content_id"]] = verdict
    return verdicts


def verify_implementation_evidence(
    implementations: List[Dict[str, Any]],
    records_by_id: Dict[str, Dict[str, Any]],
    verdicts: Dict[str, Optional[Dict[str, Any]]],
) -> None:
    """An implementation marked status=evidenced must name >=1 real record the
    router independently routed to the SAME capability. Never trust the claim."""
    for impl in implementations:
        if impl["status"] != "evidenced":
            continue
        capability = impl["capability"]
        matched = False
        for content_id in impl["verified_against"]:
            record = records_by_id.get(content_id)
            if record is None:
                raise AtlasBuildError(
                    f"implementation:{impl['implementation_id']}: verified_against "
                    f"references unknown record {content_id!r}"
                )
            verdict = verdicts.get(content_id)
            if verdict is not None and verdict["required_capability"] == capability:
                matched = True
        if not matched:
            raise AtlasBuildError(
                f"implementation:{impl['implementation_id']}: status=evidenced but no "
                f"record in verified_against actually routes to {capability!r} "
                "per the real router -- refusing to publish an unearned evidenced claim"
            )


# =========================================================================
# hard publication gate
# =========================================================================

# A drive-letter root ("C:", "c:/win", "Z:\\x"). Matched OS-independently so a
# Windows-shaped path is refused even when this builder runs on POSIX.
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:")


def _is_unsafe_fixture_path(path: str) -> bool:
    """Whether a cited fixture path could escape the repo, decided WITHOUT
    relying on the host ``os.sep``.

    A typed source is shared across contributors and operating systems, so the
    gate cannot trust ``os.path.isabs`` (host-native: on POSIX it treats
    ``C:\\x``, ``\\\\srv\\share`` and ``\\x`` as ordinary relative names) nor a
    ``/``-only split (which never sees a backslash ``..`` segment). Rejects:
    POSIX-absolute roots, Windows drive-letter roots, UNC roots, backslash
    absolute roots, and any ``..`` traversal segment in either separator style.
    """
    if not path:
        return True
    # Treat a backslash as a separator too: it is one on Windows and must never
    # be trusted as an ordinary filename character in a shared typed source.
    unified = path.replace("\\", "/")
    if os.path.isabs(path):          # host-native absolute (POSIX '/...')
        return True
    if unified.startswith("/"):       # POSIX-absolute or backslash root ('\\x')
        return True
    if unified.startswith("//"):      # UNC root ('\\\\srv\\share' or '//srv/share')
        return True
    if _DRIVE_ROOT.match(path):        # drive-letter root ('C:\\', 'C:/', 'C:x')
        return True
    if ".." in unified.split("/"):     # traversal in either separator style
        return True
    return False


def record_gate_reasons(record: Dict[str, Any]) -> List[str]:
    """Every reason this record FAILS the hard publication gate. Empty means
    eligible for indexing."""
    reasons = []
    release = record.get("release") or {}
    if release.get("release_permission") is not True:
        reasons.append("release_permission is not true")
    if not (release.get("consent_and_rights_attestation") or "").strip():
        reasons.append("consent_and_rights_attestation is empty")
    if not (release.get("license") or "").strip():
        reasons.append("license is empty")
    if release.get("share_safe_profile") != "share-safe-v1":
        reasons.append("release.share_safe_profile is not share-safe-v1")

    if record.get("origin") not in ("fixture", "synthetic", "benchmark", "cleared-captured"):
        reasons.append("origin is not one of fixture|synthetic|benchmark|cleared-captured")

    fr = record.get("failure_record") or {}
    privacy = fr.get("privacy") or {}
    if privacy.get("profile") != "share-safe-v1":
        reasons.append("failure_record.privacy.profile is not share-safe-v1")
    for flag in ("raw_audio_embedded", "transcript_body_embedded", "tool_payload_embedded",
                 "state_value_embedded", "credential_embedded", "absolute_path_embedded"):
        if privacy.get(flag) is not False:
            reasons.append(f"failure_record.privacy.{flag} is not false")

    if not (record.get("evidence_provenance") or {}).get("fixture_paths"):
        reasons.append("evidence_provenance.fixture_paths is empty")
    if not (fr.get("evidence") or []):
        reasons.append("failure_record.evidence is empty -- no claim traces to evidence")

    for path in (record.get("evidence_provenance") or {}).get("fixture_paths", []):
        if _is_unsafe_fixture_path(path):
            reasons.append(f"evidence_provenance.fixture_paths contains an unsafe path: {path!r}")

    # 'fixture' origin is a VERIFIABLE property, not a self-asserted label: every
    # cited fixture must resolve to a shipped file under examples/. This applies
    # the same "compute, never trust a stored claim" discipline used for routing.
    if record.get("origin") == "fixture":
        for path in (record.get("evidence_provenance") or {}).get("fixture_paths", []):
            if not (path.startswith("examples/") and os.path.isfile(os.path.join(REPO, path))):
                reasons.append(
                    f"origin=fixture but cited fixture does not resolve under examples/: {path!r}")

    return reasons


def is_single_case_synthetic(record: Dict[str, Any], records: List[Dict[str, Any]]) -> bool:
    """Synthetic single-case pages default to noindex."""
    if record.get("origin") != "synthetic":
        return False
    siblings = [r for r in records if r["pattern_class"] == record["pattern_class"]]
    return len(siblings) < 2


def pattern_class_qualifies(pattern_class: str, records: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """>=3 qualifying records from >=2 agent configurations, else INCONCLUSIVE."""
    members = [r for r in records if r["pattern_class"] == pattern_class and not record_gate_reasons(r)]
    configs = {m["routing_fixture"]["configuration_id"] for m in members}
    if len(members) >= PATTERN_MIN_RECORDS and len(configs) >= PATTERN_MIN_CONFIGURATIONS:
        return True, "PASS"
    return False, (
        f"INCONCLUSIVE: {len(members)} of {PATTERN_MIN_RECORDS} required qualifying "
        f"records, {len(configs)} of {PATTERN_MIN_CONFIGURATIONS} required agent "
        "configurations"
    )


def all_referenced_pattern_classes(records: List[Dict[str, Any]],
                                   contracts: List[Dict[str, Any]]) -> List[str]:
    """Every pattern class that must have a page: the ones records carry, PLUS
    any a contract's related_pattern_classes names -- so a contract can point
    at an honest, still-empty pattern page (open evidence gap) instead of a
    dead link."""
    classes = {r["pattern_class"] for r in records}
    for c in contracts:
        classes.update(c.get("related_pattern_classes") or [])
    return sorted(classes)


# =========================================================================
# tiny HTML scaffold (static, public; no token/workspace chrome from
# hotato.serve.render -- that chrome is per-workspace and does not apply to a
# public static site -- but the palette and status-chip semantics are reused)
# =========================================================================

def _status_chip(status: Optional[str]) -> str:
    if not status:
        return '<span class="dash">-</span>'
    color = _C.get(_STATUS_COLOR.get(status, "muted"), _C["muted"])
    return f'<span class="chip" style="background:{color}">{_esc(status)}</span>'

_ATLAS_CSS = ("""
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:%(bg)s;color:%(cream)s;
 font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
 font-size:15px;line-height:1.55}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.wrap{max-width:900px;margin:0 auto;padding:28px 20px 64px}
a{color:%(agent)s}
.card{background:%(card)s;border:1px solid %(line)s;border-radius:14px;
 padding:16px 18px;margin-bottom:16px}
.chip{color:#15110d;font-weight:800;font-size:12px;letter-spacing:0.05em;
 padding:3px 10px;border-radius:7px;display:inline-block}
.dash{color:%(muted)s}
.pill{background:%(card2)s;border:1px solid %(line)s;border-radius:999px;
 padding:2px 10px;font-size:11.5px;color:%(muted)s;display:inline-block}
nav.top{display:flex;flex-wrap:wrap;gap:14px;border-bottom:1px solid %(line)s;
 padding-bottom:14px;margin-bottom:20px;font-size:13.5px}
nav.top a{color:%(muted)s;text-decoration:none;font-weight:600}
nav.top a:hover{color:%(cream)s}
h1{font-size:21px;margin:0 0 6px}
h2{font-size:16px;margin:0 0 10px}
.lede{color:%(muted)s;font-size:13.5px;margin:0 0 18px}
table{border-collapse:collapse;width:100%%;font-size:13px}
th{text-align:left;color:%(muted)s;font-weight:600;font-size:11.5px;
 padding:6px 12px 6px 0;border-bottom:1px solid %(line)s}
td{padding:6px 12px 6px 0;border-bottom:1px solid %(card2)s;vertical-align:top}
.cldim{color:%(muted)s;font-size:12.5px}
.notice{background:%(card2)s;border:1px solid %(line)s;border-left:3px solid %(ember)s;
 border-radius:10px;padding:10px 13px;font-size:12.5px;margin:10px 0}
.foot{margin-top:30px;border-top:1px solid %(line)s;padding-top:16px;
 color:%(muted)s;font-size:12.5px}
""") % _C

_NAV = (
    ("/failures/", "Failures"),
    ("/contracts/engagement-control/v1/", "Contracts"),
    ("/implementations/utterance-addressee-gate/", "Implementations"),
    ("/failures/index.json", "index.json"),
    ("/implementations/index.json", "implementations.json"),
)


def page(title: str, body: str, *, noindex: bool = False) -> str:
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    nav = "".join(f'<a href="{href}">{_esc(label)}</a>' for href, label in _NAV)
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"{robots}"
        f"<title>{_esc(title)} · Hotato Voice Failure Atlas</title>"
        f"<style>{_ATLAS_CSS}</style></head><body><div class=\"wrap\">"
        f"<nav class=\"top\">{nav}</nav>"
        f"{body}"
        "<div class=\"foot\">Hotato is maintained by Attention Labs.</div>"
        "</div></body></html>"
    )


def _kv_row(label: str, value: str) -> str:
    return f'<tr><td class="cldim">{_esc(label)}</td><td class="mono">{_esc(value)}</td></tr>'


# =========================================================================
# a page-graph entry: everything the discovery files are derived from
# =========================================================================

class Page:
    __slots__ = ("path", "title", "summary", "indexed", "kind", "date", "html")

    def __init__(self, path, title, summary, indexed, kind, date, html_body):
        self.path = path
        self.title = title
        self.summary = summary
        self.indexed = indexed
        self.kind = kind
        self.date = date
        self.html = html_body


# =========================================================================
# record + pattern pages
# =========================================================================

def render_record_page(record: Dict[str, Any], verdict: Optional[Dict[str, Any]],
                        gate_reasons: List[str], noindex: bool) -> str:
    fr = record["failure_record"]
    dims = fr["dimensions"]
    ic = record["interface_conformance"]
    be = record["behavioral_evidence"]

    parts = [f'<h1>{_esc(record["title"])}</h1>',
             f'<p class="lede">{_esc(record["summary"])}</p>']

    if gate_reasons:
        parts.append('<div class="notice">This record does NOT meet the publication gate and is '
                     'not indexed. Reasons: ' + _esc("; ".join(gate_reasons)) + '.</div>')

    parts.append('<section class="card"><table><tbody>')
    parts.append(_kv_row("content id", record["content_id"]))
    parts.append(_kv_row("pattern class", record["pattern_class"]))
    parts.append(_kv_row("stack", record["stack"]))
    parts.append(_kv_row("origin", record["origin"]))
    parts.append(_kv_row("recorded date", record["recorded_date"]))
    parts.append(_kv_row("record id", fr["record_id"]))
    parts.append(_kv_row("content digest", record["content_digest"]))
    parts.append('</tbody></table></section>')

    parts.append('<section class="card"><h2>Interface conformance</h2>'
                 '<div class="cldim">Whether the input structurally conforms to what the engine can '
                 'score at all -- kept separate from the behavioral finding below.</div>'
                 '<table><tbody>')
    parts.append(_kv_row("status", ic["status"]))
    parts.append(_kv_row("scorable", str(ic["scorable"])))
    for flag in ("self_echo", "non_speech_ambient", "invalid_channel_map"):
        parts.append(_kv_row(flag, str(ic["input_health"][flag])))
    parts.append('</tbody></table></section>')

    parts.append('<section class="card"><h2>Behavioral evidence</h2>'
                 '<div class="cldim">The measured timing behavior, from a real '
                 '<span class="mono">hotato</span> CLI run against the bundled fixture.</div>'
                 '<table><tbody>')
    parts.append(_kv_row("expected behavior", be["expected_behavior"]))
    parts.append(_kv_row("observed behavior", be["observed_behavior"]))
    parts.append(_kv_row("did yield", str(be["did_yield"])))
    parts.append(_kv_row("seconds to yield", "n/a" if be["seconds_to_yield"] is None else be["seconds_to_yield"]))
    parts.append(_kv_row("talk-over seconds", "n/a" if be["talk_over_sec"] is None else be["talk_over_sec"]))
    parts.append('</tbody></table></section>')

    parts.append('<section class="card"><h2>Five dimensions (never blended)</h2><table>'
                 '<thead><tr><th>dimension</th><th>status</th><th>evaluated / passed / failed / inconclusive</th></tr></thead><tbody>')
    for dim in ("outcome", "policy", "conversation", "speech", "reliability"):
        d = dims[dim]
        if dim == "reliability":
            counts = f'{d["trials"]} trials / {d["passes"]} passed'
        else:
            counts = f'{d["evaluated"]} / {d["passed"]} / {d["failed"]} / {d["inconclusive"]}'
        parts.append(f'<tr><td class="mono">{_esc(dim)}</td><td>{_status_chip(d["status"])}</td>'
                     f'<td class="mono">{_esc(counts)}</td></tr>')
    parts.append('</tbody></table></section>')

    gate = fr["gate"]
    advisory = fr["advisory"]
    parts.append('<section class="card"><h2>Gate authority</h2>'
                 '<div class="cldim">The deterministic gate, shown apart from the model advisory.</div>'
                 '<table><tbody>')
    parts.append(_kv_row("deterministic gate", gate["status"]))
    parts.append(_kv_row("policy", gate["policy"]))
    parts.append(_kv_row("model advisory", advisory["status"]))
    parts.append(_kv_row("advisory gate enabled", str(advisory["gate_enabled"])))
    parts.append('</tbody></table></section>')

    parts.append('<section class="card"><h2>Capability requirement</h2>')
    if verdict is None:
        parts.append('<div class="cldim">No paired capability requirement is routed for this '
                     'record (either it has no opposite-risk pair, the pair is not both scorable '
                     'and input-health-clean, or the labels do not carry a trusted addressee/intent '
                     'axis). This is a generic engagement-control timing finding, not a capability '
                     'claim.</div>')
    else:
        cap_slug = CAPABILITY_SLUG[verdict["required_capability"]]
        parts.append('<table><tbody>')
        parts.append(_kv_row("required capability", verdict["required_capability"]))
        parts.append(_kv_row("trigger", verdict["trigger"]))
        parts.append('</tbody></table>')
        parts.append(f'<div class="cldim">Spec: <a href="/contracts/{_esc(cap_slug)}/v1/">'
                     f'/contracts/{_esc(cap_slug)}/v1/</a></div>')
    parts.append('</section>')

    parts.append('<section class="card"><h2>Evidence references</h2><table>'
                 '<thead><tr><th>evidence id</th><th>kind</th><th>digest</th></tr></thead><tbody>')
    for ev in fr["evidence"]:
        digest = ev["digest"]
        short = digest if len(digest) <= 27 else digest[:24] + "..."
        parts.append(f'<tr><td class="mono">{_esc(ev["evidence_id"])}</td>'
                     f'<td class="mono">{_esc(ev["kind"])}</td>'
                     f'<td class="mono">{_esc(short)}</td></tr>')
    parts.append('</tbody></table></section>')

    parts.append('<section class="card"><h2>Reproduce</h2>')
    for cmd in record["evidence_provenance"]["source_cli_commands"]:
        parts.append(f'<div class="mono cldim">$ {_esc(cmd)}</div>')
    parts.append('</section>')

    parts.append('<section class="card"><h2>CLI transcript</h2>'
                 '<div class="cldim">Verbatim output from the commands above, run against the '
                 'bundled fixture (docs/TRUST-GALLERY.md worked-example style).</div>')
    for entry in record.get("cli_transcript", []):
        parts.append(f'<div class="mono" style="margin-top:10px">$ {_esc(entry["command"])}</div>'
                     f'<pre class="mono" style="white-space:pre-wrap;overflow-x:auto;'
                     f'background:{_C["card2"]};border:1px solid {_C["line"]};border-radius:8px;'
                     f'padding:10px 12px;font-size:12px">{_esc(entry["output"])}</pre>')
    parts.append('</section>')

    return page(record["title"], "".join(parts), noindex=noindex)


def render_pattern_page(pattern_class: str, members: List[Dict[str, Any]],
                        verdicts: Dict[str, Optional[Dict[str, Any]]],
                        qualifies: bool, gate_line: str) -> str:
    parts = [f'<h1>Pattern: {_esc(pattern_class)}</h1>']
    parts.append(f'<div class="notice">{_esc(gate_line)}</div>')
    if not members:
        parts.append('<div class="cldim">No atlas record carries this pattern class yet. This '
                     'page exists so a contract can point at the open evidence gap instead of a '
                     'dead link -- see which contract references it below.</div>')
    else:
        parts.append('<section class="card"><table><thead><tr><th>record</th><th>stack</th>'
                     '<th>configuration</th><th>capability</th></tr></thead><tbody>')
        for m in sorted(members, key=lambda r: r["content_id"]):
            cap = verdicts.get(m["content_id"])
            cap_text = cap["required_capability"] if cap else "(none routed)"
            parts.append(
                f'<tr><td><a href="/failures/records/{_esc(m["content_id"])}/">{_esc(m["content_id"])}</a></td>'
                f'<td class="mono">{_esc(m["stack"])}</td>'
                f'<td class="mono">{_esc(m["routing_fixture"]["configuration_id"])}</td>'
                f'<td class="mono">{_esc(cap_text)}</td></tr>'
            )
        parts.append('</tbody></table></section>')
    return page(f"Pattern: {pattern_class}", "".join(parts), noindex=not qualifies)


def render_failures_index(records: List[Dict[str, Any]], patterns: List[str]) -> str:
    parts = ['<h1>Voice agent failures</h1>',
             '<p class="lede">Static, typed-source records of voice-agent turn-taking failures, '
             'each scored on five separate dimensions and traced to evidence from a bundled, '
             'share-safe fixture.</p>']
    parts.append('<section class="card"><h2>Records</h2><table>'
                 '<thead><tr><th>record</th><th>pattern class</th><th>origin</th></tr></thead><tbody>')
    for r in sorted(records, key=lambda x: x["content_id"]):
        parts.append(f'<tr><td><a href="/failures/records/{_esc(r["content_id"])}/">'
                     f'{_esc(r["content_id"])}</a></td>'
                     f'<td class="mono">{_esc(r["pattern_class"])}</td>'
                     f'<td class="mono">{_esc(r["origin"])}</td></tr>')
    parts.append('</tbody></table></section>')
    parts.append('<section class="card"><h2>Patterns</h2><table>'
                 '<thead><tr><th>pattern class</th></tr></thead><tbody>')
    for p in sorted(patterns):
        parts.append(f'<tr><td><a href="/failures/patterns/{_esc(p)}/">{_esc(p)}</a></td></tr>')
    parts.append('</tbody></table></section>')
    return page("Voice agent failures", "".join(parts))


# =========================================================================
# contract + implementation + integration pages
# =========================================================================

def render_contract_page(contract: Dict[str, Any]) -> str:
    parts = [f'<h1>{_esc(contract["title"])}</h1>',
             f'<p class="lede">{_esc(contract["summary"])}</p>']
    parts.append('<section class="card"><table><tbody>')
    parts.append(_kv_row("capability", contract["capability"]))
    parts.append(_kv_row("fix class", contract["fix_class"]))
    parts.append(_kv_row("backchannel exclusion", str(contract["backchannel_exclusion"])))
    parts.append(_kv_row("spec uri", contract["spec_uri"]))
    parts.append('</tbody></table></section>')
    parts.append('<section class="card"><h2>Acceptance tests</h2><ul>')
    for t in contract["acceptance_tests"]:
        parts.append(f'<li class="mono">{_esc(t)}</li>')
    parts.append('</ul></section>')
    if contract["excluded_causes"]:
        parts.append('<section class="card"><h2>Excluded causes</h2><ul>')
        for c in contract["excluded_causes"]:
            parts.append(f'<li class="mono">{_esc(c)}</li>')
        parts.append('</ul></section>')
    if contract["related_pattern_classes"]:
        parts.append('<section class="card"><h2>Related pattern classes</h2><ul>')
        for p in sorted(contract["related_pattern_classes"]):
            parts.append(f'<li><a href="/failures/patterns/{_esc(p)}/">{_esc(p)}</a></li>')
        parts.append('</ul></section>')
    return page(contract["title"], "".join(parts))


def render_implementation_detail(impl: Dict[str, Any]) -> str:
    parts = [f'<h1>{_esc(impl["title"])}</h1>',
             f'<p class="lede">{_esc(impl["approach"])}</p>']
    parts.append('<section class="card"><table><tbody>')
    parts.append(_kv_row("capability", impl["capability"]))
    parts.append(_kv_row("stack", impl["stack"]))
    parts.append(_kv_row("status", impl["status"]))
    parts.append('</tbody></table></section>')
    parts.append('<section class="card"><h2>Integration points</h2><ul>')
    for point in impl["integration_points"]:
        parts.append(f'<li>{_esc(point)}</li>')
    parts.append('</ul></section>')
    if impl["verified_against"]:
        parts.append('<section class="card"><h2>Evidenced against</h2><ul>')
        for content_id in sorted(impl["verified_against"]):
            parts.append(f'<li><a href="/failures/records/{_esc(content_id)}/">{_esc(content_id)}</a></li>')
        parts.append('</ul></section>')
    else:
        parts.append('<div class="notice">No atlas record is indexed as evidence for this recipe '
                     'yet; it documents the capability contract, not an observed case.</div>')
    return page(impl["title"], "".join(parts))


def render_implementation_landing(capability: str, impls: List[Dict[str, Any]]) -> str:
    parts = [f'<h1>Implementations: {_esc(capability)}</h1>']
    parts.append('<section class="card"><table><thead><tr><th>stack</th><th>status</th>'
                 '<th>title</th></tr></thead><tbody>')
    for i in sorted(impls, key=lambda x: x["stack"]):
        cap_slug = CAPABILITY_SLUG[i["capability"]]
        parts.append(f'<tr><td class="mono">{_esc(i["stack"])}</td>'
                     f'<td class="mono">{_esc(i["status"])}</td>'
                     f'<td><a href="/implementations/{_esc(cap_slug)}/{_esc(i["stack"])}/">'
                     f'{_esc(i["title"])}</a></td></tr>')
    parts.append('</tbody></table></section>')
    return page(f"Implementations: {capability}", "".join(parts))


def render_implementations_index(implementations: List[Dict[str, Any]]) -> str:
    caps = sorted({i["capability"] for i in implementations})
    parts = ['<h1>Implementation recipes</h1>',
             '<p class="lede">Neutral, per-stack recipes for the engagement-control capabilities '
             'above. A coding agent chooses an implementation; this Atlas does not.</p>']
    parts.append('<section class="card"><ul>')
    for cap in caps:
        slug = CAPABILITY_SLUG[cap]
        parts.append(f'<li><a href="/implementations/{_esc(slug)}/">{_esc(cap)}</a></li>')
    parts.append('</ul></section>')
    return page("Implementation recipes", "".join(parts))


def render_integration_page(stack: str, records: List[Dict[str, Any]],
                            implementations: List[Dict[str, Any]]) -> str:
    parts = [f'<h1>Integration: {_esc(stack)}</h1>']
    parts.append('<section class="card"><h2>Records</h2>')
    if not records:
        parts.append('<div class="cldim">0 records captured on this stack in this Atlas build. '
                     'Interface conformance and behavioural evidence are both UNAVAILABLE here, '
                     'never assumed.</div>')
    else:
        parts.append('<ul>')
        for r in sorted(records, key=lambda x: x["content_id"]):
            parts.append(f'<li><a href="/failures/records/{_esc(r["content_id"])}/">{_esc(r["content_id"])}</a></li>')
        parts.append('</ul>')
    parts.append('</section>')
    parts.append('<section class="card"><h2>Implementation recipes</h2>')
    if not implementations:
        parts.append('<div class="cldim">0 implementation recipes reference this stack yet.</div>')
    else:
        parts.append('<ul>')
        for i in sorted(implementations, key=lambda x: x["implementation_id"]):
            cap_slug = CAPABILITY_SLUG[i["capability"]]
            parts.append(f'<li><a href="/implementations/{_esc(cap_slug)}/{_esc(i["stack"])}/">'
                         f'{_esc(i["title"])}</a></li>')
        parts.append('</ul>')
    parts.append('</section>')
    return page(f"Integration: {stack}", "".join(parts), noindex=not records)


# =========================================================================
# build
# =========================================================================

def build(out_dir: str) -> Dict[str, Any]:
    sources = load_sources()
    records = sources["records"]
    contracts = sources["contracts"]
    implementations = sources["implementations"]
    records_by_id = {r["content_id"]: r for r in records}

    verdicts = compute_capability_verdicts(records)
    verify_implementation_evidence(implementations, records_by_id, verdicts)

    pages: List[Page] = []

    def write(path: str, html_body: str, *, title: str, summary: str,
             indexed: bool, kind: str, date: str) -> None:
        target = os.path.join(out_dir, path.strip("/"), "index.html") if path.endswith("/") \
            else os.path.join(out_dir, path.lstrip("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(html_body)
        pages.append(Page(path, title, summary, indexed, kind, date, html_body))

    # --- records -----------------------------------------------------
    for r in sorted(records, key=lambda x: x["content_id"]):
        reasons = record_gate_reasons(r)
        single_case = is_single_case_synthetic(r, records)
        indexed = not reasons and not single_case
        noindex = not indexed
        body = render_record_page(r, verdicts[r["content_id"]], reasons, noindex)
        write(f'/failures/records/{r["content_id"]}/', body,
              title=r["title"], summary=r["summary"], indexed=indexed,
              kind="record", date=r["recorded_date"])

    # --- patterns ------------------------------------------------------
    # Every pattern class a record carries, PLUS any a contract references
    # (so a contract can point at an honest, still-empty pattern page instead
    # of a dead link -- e.g. the SAA-eligible class with zero cleared fixtures).
    pattern_classes = all_referenced_pattern_classes(records, contracts)
    fallback_date = max((r["recorded_date"] for r in records), default="1970-01-01")
    for pc in pattern_classes:
        members = [r for r in records if r["pattern_class"] == pc]
        qualifies, gate_line = pattern_class_qualifies(pc, records)
        body = render_pattern_page(pc, members, verdicts, qualifies, gate_line)
        write(f'/failures/patterns/{pc}/', body,
              title=f"Pattern: {pc}", summary=gate_line, indexed=qualifies,
              kind="pattern",
              date=max((m["recorded_date"] for m in members), default=fallback_date))

    write('/failures/', render_failures_index(records, pattern_classes),
          title="Voice agent failures", summary="Index of failure records and patterns.",
          indexed=True, kind="section", date=max((r["recorded_date"] for r in records), default="1970-01-01"))

    # --- contracts -------------------------------------------------------
    for c in sorted(contracts, key=lambda x: x["family"]):
        body = render_contract_page(c)
        write(f'/contracts/{c["family"]}/{c["spec_version"]}/', body,
              title=c["title"], summary=c["summary"], indexed=True,
              kind="contract", date=c["recorded_date"])

    # --- implementations ---------------------------------------------
    for i in sorted(implementations, key=lambda x: x["implementation_id"]):
        cap_slug = CAPABILITY_SLUG[i["capability"]]
        body = render_implementation_detail(i)
        write(f'/implementations/{cap_slug}/{i["stack"]}/', body,
              title=i["title"], summary=i["approach"], indexed=True,
              kind="implementation", date=i["recorded_date"])

    for cap in sorted({i["capability"] for i in implementations}):
        slug = CAPABILITY_SLUG[cap]
        impls = [i for i in implementations if i["capability"] == cap]
        body = render_implementation_landing(cap, impls)
        write(f'/implementations/{slug}/', body,
              title=f"Implementations: {cap}", summary=f"Per-stack recipes for {cap}.",
              indexed=True, kind="section",
              date=max(i["recorded_date"] for i in impls))

    write('/implementations/', render_implementations_index(implementations),
          title="Implementation recipes", summary="Neutral per-stack recipes.",
          indexed=True, kind="section",
          date=max((i["recorded_date"] for i in implementations), default="1970-01-01"))

    # --- integrations (by stack) ----------------------------------------
    # Every stack hotato's CLI recognizes gets a page, not only the ones a
    # record happens to reference -- a stack with 0 records still resolves to
    # an honest empty state instead of a 404, so the page-graph template is
    # complete from the first build.
    stacks = sorted(set(KNOWN_STACKS) | {r["stack"] for r in records}
                    | {i["stack"] for i in implementations})
    for stack in stacks:
        stack_records = [r for r in records if r["stack"] == stack]
        stack_impls = [i for i in implementations if i["stack"] == stack]
        body = render_integration_page(stack, stack_records, stack_impls)
        write(f'/integrations/{stack}/', body,
              title=f"Integration: {stack}", summary=f"Records and recipes for the {stack} stack.",
              indexed=bool(stack_records), kind="integration",
              date=max([r["recorded_date"] for r in stack_records]
                       + [i["recorded_date"] for i in stack_impls], default="1970-01-01"))

    # --- benchmarks: the measured-evidence view of every record, one per
    #     content_id (not gated on origin -- "benchmark" here means "the raw
    #     measurement", matching docs/BENCHMARK.md's usage, not the narrower
    #     origin enum value) --------------------------------------------
    for r in sorted(records, key=lambda x: x["content_id"]):
        reasons = record_gate_reasons(r)
        indexed = not reasons and not is_single_case_synthetic(r, records)
        write(f'/benchmarks/{r["content_id"]}/', render_record_page(
                  r, verdicts[r["content_id"]], reasons, not indexed),
              title=f'Benchmark: {r["title"]}', summary=r["summary"], indexed=indexed,
              kind="benchmark", date=r["recorded_date"])

    _write_machine_indexes(out_dir, records, verdicts, implementations)
    _write_discovery(out_dir, pages)

    return {"pages": len(pages), "indexed": sum(1 for p in pages if p.indexed),
            "records": len(records), "contracts": len(contracts),
            "implementations": len(implementations)}


def _write_json(out_dir: str, path: str, obj: Any) -> None:
    target = os.path.join(out_dir, path.lstrip("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def _write_machine_indexes(out_dir: str, records: List[Dict[str, Any]],
                           verdicts: Dict[str, Optional[Dict[str, Any]]],
                           implementations: List[Dict[str, Any]]) -> None:
    failures_index = []
    for r in sorted(records, key=lambda x: x["content_id"]):
        reasons = record_gate_reasons(r)
        indexed = not reasons and not is_single_case_synthetic(r, records)
        verdict = verdicts[r["content_id"]]
        failures_index.append({
            "content_id": r["content_id"],
            "pattern_class": r["pattern_class"],
            "path": f'/failures/records/{r["content_id"]}/',
            "indexed": indexed,
            "origin": r["origin"],
            "stack": r["stack"],
            "required_capability": verdict["required_capability"] if verdict else None,
            "content_digest": r["content_digest"],
            "recorded_date": r["recorded_date"],
        })
    _write_json(out_dir, "/failures/index.json", {"schema": "hotato.atlas-failures-index.v1",
                                                   "records": failures_index})

    impl_index = [{
        "implementation_id": i["implementation_id"],
        "capability": i["capability"],
        "stack": i["stack"],
        "path": f'/implementations/{CAPABILITY_SLUG[i["capability"]]}/{i["stack"]}/',
        "status": i["status"],
        "content_digest": i["content_digest"],
        "recorded_date": i["recorded_date"],
    } for i in sorted(implementations, key=lambda x: x["implementation_id"])]
    _write_json(out_dir, "/implementations/index.json",
               {"schema": "hotato.atlas-implementations-index.v1", "implementations": impl_index})


def _write_discovery(out_dir: str, pages: List[Page]) -> None:
    indexed_pages = sorted((p for p in pages if p.indexed), key=lambda p: p.path)

    # llms.txt -- curated pointer index, mirrors the repo root's llms.txt style.
    lines = [
        "# Hotato Voice Failure Atlas",
        "",
        "> Static, typed-source records of voice-agent turn-taking failures. Every "
        "record cites the CLI evidence behind it and is scored on five separate "
        "dimensions; nothing is blended into one score. Server-rendered, no "
        "JavaScript, no tracking.",
        "",
    ]
    for p in indexed_pages:
        lines.append(f"- [{p.title}]({p.path}) -- {p.summary}")
    with open(os.path.join(out_dir, "llms.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # llms-full.txt -- full content dump of every indexed page.
    full = ["# Hotato Voice Failure Atlas -- full content", ""]
    for p in indexed_pages:
        full.append(f"## {p.title} ({p.path})")
        full.append(p.summary)
        full.append("")
    with open(os.path.join(out_dir, "llms-full.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(full) + "\n")

    # sitemap.xml -- lastmod from each page's source-controlled date, never
    # the build clock.
    urlset = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in indexed_pages:
        urlset.append(f"  <url><loc>{_esc(p.path)}</loc><lastmod>{_esc(p.date)}</lastmod></url>")
    urlset.append("</urlset>")
    with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(urlset) + "\n")

    # feed.xml -- Atom, sorted by (date desc, path asc) for determinism.
    entries = sorted(indexed_pages, key=lambda p: (p.date, p.path), reverse=True)
    updated = max((p.date for p in indexed_pages), default="1970-01-01")
    feed = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<feed xmlns="http://www.w3.org/2005/Atom">',
            "  <title>Hotato Voice Failure Atlas</title>",
            f"  <updated>{_esc(updated)}T00:00:00Z</updated>",
            '  <id>https://hotato.dev/atlas/feed.xml</id>']
    for p in entries:
        feed.append("  <entry>")
        feed.append(f"    <title>{_esc(p.title)}</title>")
        feed.append(f'    <link href="{_esc(p.path)}"/>')
        feed.append(f"    <id>https://hotato.dev/atlas{_esc(p.path)}</id>")
        feed.append(f"    <updated>{_esc(p.date)}T00:00:00Z</updated>")
        feed.append(f"    <summary>{_esc(p.summary)}</summary>")
        feed.append("  </entry>")
    feed.append("</feed>")
    with open(os.path.join(out_dir, "feed.xml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(feed) + "\n")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out_dir = argv[0] if argv else DEFAULT_OUT
    stats = build(out_dir)
    print(f"Built the Voice Failure Atlas into {out_dir}: "
          f"{stats['pages']} pages ({stats['indexed']} indexed), "
          f"{stats['records']} records, {stats['contracts']} contracts, "
          f"{stats['implementations']} implementations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
