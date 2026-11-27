"""``hotato pr comment``: one pull-request comment built from hotato's own
machine results.

Reads ONE machine result -- a ``hotato.suite-run`` (``hotato suite run``), a
``hotato.test-run`` (``hotato test run``), or a ``contract-verify``
(``hotato contract verify``) JSON document -- and renders the Markdown body of
a single sticky pull-request comment, which is also what the root Action
writes to the job summary:

* the overall verdict and the per-status counts;
* a table of every case that did not pass: what failed, which case, the
  measured evidence, and where its Failure Record and evidence card are;
* a red-to-green comparison against the base branch's result when one is
  supplied (fixed / still failing / newly failing, plus cases that became
  inconclusive);
* the evidence files (Failure Records from ``hotato record render --all``,
  evidence cards from ``hotato card``) and a link to the workflow run whose
  artifact holds them.

The module is a PURE, OFFLINE renderer on the standard library: it never
touches the network and the same inputs always render the same bytes (no
clock, no environment, no absolute path in the output). The one optional side
effect, :func:`render_cards`, writes SVG evidence cards into a directory the
caller names. Posting the comment is a separate step (the root Action's
``comment-pr`` input), the only part of the flow that reaches the GitHub API.

Status words follow the machine result and never blend axes: a check that is
missing the evidence it needs is INCONCLUSIVE, never PASS and never FAIL; a
contract whose channel mapping is unconfirmed is REFUSED; an edited bundle is
TAMPERED. The deterministic result owns the verdict; the model-judged rubric
lane is reported on its own line and never changes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import open_regular

__all__ = [
    "MARKER",
    "LEGACY_MARKERS",
    "KINDS",
    "load_result",
    "units_of",
    "verdict_of",
    "diff_units",
    "load_records_index",
    "render_cards",
    "render_pr_report",
]

# Hidden first line. The poster finds the ONE sticky comment by this marker and
# updates it in place; keep it stable and keep it first.
MARKER = "<!-- hotato-pr-report -->"

# Earlier sticky markers the poster also recognises, so a pull request that
# already carries the contract-verify comment gets that comment UPDATED to the
# new report instead of a second comment being added.
LEGACY_MARKERS = ("<!-- hotato-contract-verify -->",)

KINDS = ("hotato.suite-run", "hotato.test-run", "contract-verify")

_KIND_NOUN = {
    "hotato.suite-run": ("test", "tests"),
    "hotato.test-run": ("check", "checks"),
    "contract-verify": ("contract", "contracts"),
}

# Status words, and the three classes the comparison works in.
PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
REFUSED, NOT_SCORABLE, TAMPERED, ERROR = (
    "REFUSED", "NOT_SCORABLE", "TAMPERED", "ERROR")
_RED = frozenset({FAIL, TAMPERED, ERROR})
_AMBER = frozenset({INCONCLUSIVE, REFUSED, NOT_SCORABLE})
_STATUS_ORDER = (FAIL, TAMPERED, ERROR, REFUSED, NOT_SCORABLE, INCONCLUSIVE,
                 PASS)

# GitHub rejects an issue comment above 65,536 characters. The row cap keeps a
# large run readable; the byte budget is the hard backstop under the limit.
DEFAULT_MAX_ROWS = 25
_MAX_BODY_CHARS = 60000
_CELL_CAP = 160


def _class(status: str) -> str:
    if status == PASS:
        return "pass"
    if status in _RED:
        return "red"
    return "amber"


# ---------------------------------------------------------------------------
# Markdown safety
# ---------------------------------------------------------------------------

# Markdown punctuation a result-file value could use to open a link, an
# image, emphasis, or strikethrough. Each is backslash-escaped, which
# CommonMark and GitHub honour for any ASCII punctuation.
_MD_PUNCT = frozenset("\\[]()!*_~")


def _cell(value: Any, cap: int = _CELL_CAP) -> str:
    """One table cell or inline phrase read off a result file, rendered as
    inert text: whitespace collapsed, backticks dropped, ``&`` ``<`` ``>``
    entity-escaped, Markdown link, image and emphasis punctuation
    backslash-escaped, pipes escaped, and ``@``, ``://`` and ``www.``
    neutralised so a value can never break the table, inject markup, render a
    link or an image, autolink a URL, or mention a user from the comment."""
    text = " ".join(str("" if value is None else value).split())
    if len(text) > cap:
        text = text[: cap - 3].rstrip() + "..."
    text = (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("`", "'"))
    text = "".join("\\" + ch if ch in _MD_PUNCT else ch for ch in text)
    # ``|`` breaks a table row; ``@`` mentions a user or autolinks an email;
    # ``://`` and ``www.`` are where GitHub's bare-URL autolinks start.
    text = re.sub(r"(?i)\bwww\.", lambda m: m.group(0)[:3] + "&#46;", text)
    return (text.replace("|", "\\|").replace("@", "&#64;")
            .replace("://", "&#58;//"))


_SAFE_URL_RE = re.compile(
    r"^https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?"
    r"(/[A-Za-z0-9._~%/+=,;-]*)?(\?[A-Za-z0-9._~%/+=,;&-]*)?$")


def safe_url(value: Any) -> Optional[str]:
    """``value`` when it is a plain ``https://`` URL that is safe to place in
    a Markdown link target (no whitespace, quotes, brackets, parentheses or
    backslashes), else ``None``."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    return value if _SAFE_URL_RE.match(value) else None


def _code(value: Any, cap: int = 120) -> str:
    """An identifier as inline code. Inside a code span GitHub renders no
    mention and no markup, so only the backtick and the pipe need care."""
    text = " ".join(str("" if value is None else value).split())
    if len(text) > cap:
        text = text[: cap - 3] + "..."
    text = text.replace("`", "'").replace("|", "\\|")
    return f"`{text}`" if text else "`?`"


def _sec(value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{float(value):.2f} s"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_result(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse one machine result; returns ``(doc, error)`` and never raises.
    ``error`` names why the file is unusable (unreadable, not JSON, not an
    object, or not one of :data:`KINDS`)."""
    try:
        fh = open_regular(path, "r", encoding="utf-8")
    except OSError as exc:
        return None, f"unreadable ({exc.strerror or exc.__class__.__name__})"
    except ValueError:
        return None, "not a regular file"
    try:
        with fh:
            doc = json.load(fh)
    except OSError as exc:
        return None, f"unreadable ({exc.strerror or exc.__class__.__name__})"
    except ValueError:
        return None, "not valid JSON"
    if not isinstance(doc, dict):
        return None, "not a JSON object"
    kind = doc.get("kind")
    if kind not in KINDS:
        return None, (f"kind {kind!r} is not a suite run, a test run, or a"
                      " contract verification")
    return doc, None


# ---------------------------------------------------------------------------
# units: one row per case, the same key on the head and the base
# ---------------------------------------------------------------------------

def _unit(key: str, case: str, status: str, what: str, evidence: str,
          *, record_key: Optional[str] = None,
          card_source: Optional[str] = None) -> Dict[str, Any]:
    return {
        "key": key, "case": case, "status": status, "what": what,
        "evidence": evidence, "record_key": record_key or case,
        "card_source": card_source,
    }


def _suite_units(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for t in doc.get("tests") or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("test_id") or "?")
        raw = str(t.get("status") or "")
        dims = t.get("dimensions") or {}
        reasons = t.get("dim_reason") or {}
        failing = sorted(k for k, v in dims.items() if v == "FAIL") \
            if isinstance(dims, dict) else []
        inconclusive = sorted(k for k, v in dims.items()
                              if v == "INCONCLUSIVE") \
            if isinstance(dims, dict) else []
        if raw == "pass":
            status = PASS
        elif raw == "refuse":
            status = REFUSED
        elif raw == "fail":
            status = FAIL if failing or not inconclusive else INCONCLUSIVE
        else:
            status = ERROR
        if status == PASS:
            what = "all checks pass"
        elif failing:
            what = ", ".join(failing) + (" check failed" if len(failing) == 1
                                         else " checks failed")
        elif inconclusive:
            what = (", ".join(inconclusive)
                    + " check missing required evidence")
        elif status == REFUSED:
            what = "verdict withheld under the test's policy"
        else:
            what = f"test status {raw or 'missing'}"
        bits = []
        if isinstance(reasons, dict):
            for k in sorted(reasons):
                if reasons[k]:
                    bits.append(f"{k}: {reasons[k]}")
        rel = t.get("reliability") or {}
        if isinstance(rel, dict) and rel.get("n") not in (None, ""):
            bits.append(f"{rel.get('passes')}/{rel.get('n')} runs passed")
        out.append(_unit(tid, tid, status, what, "; ".join(bits)))
    return out


def _test_units(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    tid = str(doc.get("test_id") or "?")
    out = []
    any_fail = False
    for r in ((doc.get("assertions") or {}).get("results")) or []:
        if not isinstance(r, dict):
            continue
        aid = str(r.get("id") or "?")
        raw = str(r.get("status") or "").upper()
        status = raw if raw in (PASS, FAIL, INCONCLUSIVE) else ERROR
        any_fail = any_fail or status == FAIL
        dim = r.get("dimension") or "check"
        what = (f"{dim} check" if status == PASS
                else f"{dim} check {'missing required evidence' if status == INCONCLUSIVE else 'failed'}")
        bits = []
        if r.get("measured_ms") is not None:
            bits.append(f"measured {r['measured_ms']} ms")
        if r.get("reason"):
            bits.append(str(r["reason"]))
        out.append(_unit(f"{tid}:{aid}", f"{tid} / {aid}", status, what,
                         "; ".join(bits), record_key=tid,
                         card_source="self"))
    success = doc.get("success") or {}
    explained = any(u["status"] in (FAIL, INCONCLUSIVE) for u in out)
    if (isinstance(success, dict) and success.get("passed") is False
            and not explained and doc.get("exit_code") == 1):
        # A failed success.required condition the deterministic checks do not
        # already explain (for example a gated rubric failure).
        conds = success.get("conditions") or {}
        unmet = sorted(k for k, v in conds.items() if v is False) \
            if isinstance(conds, dict) else []
        out.append(_unit(f"{tid}:success.required",
                         f"{tid} / success.required", FAIL,
                         "a success.required condition failed",
                         ("unmet: " + ", ".join(unmet)) if unmet else "",
                         record_key=tid, card_source="self"))
    return out


def _contract_evidence(r: Dict[str, Any]) -> str:
    m = r.get("measurement") or {}
    if not isinstance(m, dict):
        m = {}
    stt, tov = _sec(m.get("seconds_to_yield")), _sec(m.get("talk_over_sec"))
    if m.get("did_yield") is True:
        head = f"agent yielded after {stt}" if stt else "agent yielded"
    elif m.get("did_yield") is False:
        head = "agent did not yield"
    else:
        head = "no yield measured"
    return head + (f"; talk-over {tov}" if tov else "")


def _contract_units(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for r in doc.get("results") or []:
        if not isinstance(r, dict):
            continue
        cid = str(r.get("id") or "?")
        expect = str(r.get("expect") or "?")
        bundle = r.get("dir")
        card = (os.path.join(str(bundle), "contract.json")
                if isinstance(bundle, str) and bundle else None)
        if r.get("authenticity") == "tampered":
            status = TAMPERED
            what = "bundle edited after creation"
            evidence = str(r.get("authenticity_reason") or "")
        elif not r.get("scorable", True):
            status = NOT_SCORABLE
            what = "recording not scorable"
            evidence = str(r.get("not_scorable_reason") or "")
        elif not r.get("verdict_eligible", True):
            status = REFUSED
            what = "channel mapping unconfirmed"
            evidence = str(r.get("verdict_ineligible_reason") or "")
        else:
            status = PASS if r.get("passed") else FAIL
            what = (f"turn-taking contract (expect {expect})"
                    if status == PASS
                    else f"turn-taking regression (expect {expect})")
            evidence = _contract_evidence(r)
        out.append(_unit(cid, cid, status, what, evidence, card_source=card))
        env = r.get("assertions")
        if isinstance(env, dict):
            results = env.get("results") or []
            fails = [a for a in results
                     if isinstance(a, dict)
                     and str(a.get("status", "")).upper() == FAIL]
            ok = env.get("exit_code") == 0
            first = fails[0] if fails else {}
            ev = (f"{first.get('id', '?')}: {first.get('reason') or 'FAIL'}"
                  if fails else "")
            # A SEPARATE axis, never blended into the timing row above.
            out.append(_unit(
                f"{cid}#assertions", f"{cid} / embedded assertions",
                PASS if ok else FAIL,
                "embedded assertions" + ("" if ok else " failed"), ev,
                record_key=cid, card_source=card))
    return out


_UNIT_FNS = {
    "hotato.suite-run": _suite_units,
    "hotato.test-run": _test_units,
    "contract-verify": _contract_units,
}


def units_of(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every case of one machine result in source order, each with a stable
    comparison ``key``, a display ``case``, a status word, what failed, and
    the measured evidence. Raises ``ValueError`` on an unknown kind."""
    fn = _UNIT_FNS.get(doc.get("kind"))
    if fn is None:
        raise ValueError(f"not a hotato machine result kind: {doc.get('kind')!r}")
    units = fn(doc)
    seen: Dict[str, int] = {}
    for u in units:
        # A duplicated key would make the comparison ambiguous; suffix repeats
        # by position so every key stays unique and stable.
        n = seen.get(u["key"], 0)
        seen[u["key"]] = n + 1
        if n:
            u["key"] = f"{u['key']}~{n + 1}"
    return units


def verdict_of(doc: Dict[str, Any], units: Sequence[Dict[str, Any]]) -> str:
    """``pass`` / ``fail`` / ``inconclusive`` / ``error`` from the result's own
    exit code and its cases (the same four words the Action's ``status``
    output uses). A missing check is never a PASS."""
    code = doc.get("exit_code")
    statuses = {u["status"] for u in units}
    red = bool(statuses & _RED)
    amber = bool(statuses & _AMBER)
    if code == 0:
        return "inconclusive" if amber else "pass"
    if code == 1:
        return "fail" if red or not amber else "inconclusive"
    if code == 2 and amber:
        return "inconclusive"
    return "error"


# ---------------------------------------------------------------------------
# red to green
# ---------------------------------------------------------------------------

def diff_units(head: Sequence[Dict[str, Any]],
               base: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare this run's cases with the base branch's, by case key.

    * ``newly_failing``: red here, and passing (or absent) on the base;
    * ``still_failing``: red here and red on the base;
    * ``fixed``: passing here, and red or inconclusive on the base;
    * ``now_inconclusive``: inconclusive here, and not inconclusive on the
      base (evidence went missing; never counted as a pass or a fail);
    * ``removed``: on the base, absent here.

    Each list but ``removed`` holds ``(head unit, base status or None)`` pairs
    in this run's order; ``removed`` holds the base units, in the base's
    order. Pure and deterministic."""
    by_key = {u["key"]: u for u in base}
    head_keys = {u["key"] for u in head}
    out: Dict[str, Any] = {"newly_failing": [], "still_failing": [],
                           "fixed": [], "now_inconclusive": [],
                           "removed": []}
    for u in head:
        b = by_key.get(u["key"])
        bs = b["status"] if b else None
        hc = _class(u["status"])
        bc = _class(bs) if bs else None
        if hc == "red":
            out["still_failing" if bc == "red" else "newly_failing"].append(
                (u, bs))
        elif hc == "pass":
            if bc in ("red", "amber"):
                out["fixed"].append((u, bs))
        elif bc != "amber":
            out["now_inconclusive"].append((u, bs))
    for b in base:
        if b["key"] not in head_keys:
            out["removed"].append(b)
    return out


# ---------------------------------------------------------------------------
# evidence files
# ---------------------------------------------------------------------------

def _posix(path: str) -> str:
    return path.replace(os.sep, "/") if os.sep != "/" else path


def _rel(path: str, root: Optional[str]) -> str:
    """``path`` relative to ``root`` (the uploaded artifact's root) when it
    lies beneath it, POSIX-separated; else the path unchanged."""
    if root:
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = path
        if not rel.startswith(".."):
            return _posix(rel)
    return _posix(path)


def load_records_index(path: str) -> Dict[str, str]:
    """Map each test id in a ``hotato record render --all`` index to its
    ``failure-record.md`` path (index directory + the entry's directory).
    Fail-open: an unreadable or malformed index yields ``{}``, never an
    error, because the records are presentation only."""
    try:
        with open_regular(path, "r", encoding="utf-8") as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(index, dict) or \
            index.get("kind") != "hotato.failure-record-index.v1":
        return {}
    base = os.path.dirname(path)
    out: Dict[str, str] = {}
    for entry in index.get("records") or []:
        if not isinstance(entry, dict):
            continue
        tid, directory = entry.get("test_id"), entry.get("directory")
        if not isinstance(tid, str) or not isinstance(directory, str):
            continue
        if not re.match(r"^[A-Za-z0-9._:-]{1,200}$", directory) or \
                directory in (".", ".."):
            continue
        out.setdefault(tid, os.path.join(base, directory, "failure-record.md"))
    return out


def _card_name(key: str, used: set) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-.")[:80] or "card"
    if slug != key or slug in used:
        slug = f"{slug}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"
    used.add(slug)
    return slug + ".svg"


def render_cards(doc: Dict[str, Any], cards_dir: str, *,
                 result_path: Optional[str] = None,
                 limit: int = DEFAULT_MAX_ROWS) -> Dict[str, str]:
    """Render one ``hotato card`` SVG per failing case that has a card input
    (a contract bundle's ``contract.json``; a test run's own result), into
    ``cards_dir``. Returns ``{unit key: card path}``. Fail-open per card: an
    input the card renderer refuses is skipped, never an error. The cards are
    share-safe (no identifiers), offline, and byte-stable."""
    from . import card as _card

    units = units_of(doc)
    made: Dict[str, str] = {}
    by_source: Dict[str, str] = {}
    used: set = set()
    count = 0
    for u in units:
        if _class(u["status"]) == "pass" or not u.get("card_source"):
            continue
        source = result_path if u["card_source"] == "self" else u["card_source"]
        if not source:
            continue
        if source in by_source:
            made[u["key"]] = by_source[source]
            continue
        if count >= limit:
            break
        try:
            svg = _card.make_card(source)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        name = _card_name(u["record_key"], used)
        os.makedirs(cards_dir, exist_ok=True)
        dest = os.path.join(cards_dir, name)
        with open(dest, "w", encoding="utf-8", newline="") as fh:
            fh.write(svg)
        by_source[source] = dest
        made[u["key"]] = dest
        count += 1
    return made


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _count_line(kind: str, units: Sequence[Dict[str, Any]]) -> str:
    one, many = _KIND_NOUN[kind]
    n = len(units)
    counts = {s: 0 for s in _STATUS_ORDER}
    for u in units:
        counts[u["status"]] = counts.get(u["status"], 0) + 1
    parts = [f"{counts[PASS]} pass"]
    for s in _STATUS_ORDER:
        if s != PASS and counts.get(s):
            parts.append(f"{counts[s]} {s.lower().replace('_', ' ')}")
    return f"{n} {one if n == 1 else many}: " + ", ".join(parts)


def _subject(doc: Dict[str, Any]) -> str:
    kind = doc["kind"]
    if kind == "hotato.suite-run":
        s = f"Suite {_code(doc.get('suite_id'))}"
        if doc.get("agent"):
            s += f", agent {_code(doc.get('agent'))}"
        return s
    if kind == "hotato.test-run":
        s = f"Conversation test {_code(doc.get('test_id'))}"
        if doc.get("agent"):
            s += f", agent {_code(doc.get('agent'))}"
        return s
    return f"Contract verification of {_code(doc.get('dir'))}"


def _advisory_line(doc: Dict[str, Any]) -> Optional[str]:
    kind = doc["kind"]
    totals = {"pass": 0, "fail": 0, "inconclusive": 0, "error": 0}
    gated = False
    seen = False
    if kind == "hotato.suite-run":
        for t in doc.get("tests") or []:
            rs = (t or {}).get("rubric_summary") or {}
            for k in totals:
                if rs.get(k):
                    seen = True
                    totals[k] += int(rs.get(k) or 0)
    elif kind == "hotato.test-run":
        env = doc.get("rubric") or {}
        gated = bool(env.get("gated"))
        for r in env.get("results") or []:
            st = str((r or {}).get("status", "")).lower()
            if st in totals:
                seen = True
                totals[st] += 1
    if not seen:
        return None
    gate = ("gates this run (opted in)" if gated
            else "advisory, does not change the verdict")
    return (f"Model-judged rubric lane ({gate}): {totals['pass']} pass,"
            f" {totals['fail']} fail, {totals['inconclusive']} inconclusive,"
            f" {totals['error']} error.")


def _files_cell(u: Dict[str, Any], records: Dict[str, str],
                cards: Dict[str, str], link: Optional[str]) -> str:
    bits = []
    for label, has in (("record", u["record_key"] in records),
                       ("card", u["key"] in cards)):
        if has:
            bits.append(f"[{label}]({link})" if link else label)
    return " · ".join(bits) if bits else "-"


def _file_list(units: Sequence[Dict[str, Any]], records: Dict[str, str],
               cards: Dict[str, str], root: Optional[str]) -> List[str]:
    lines: List[str] = []
    seen = set()
    for u in units:
        rec = records.get(u["record_key"])
        card = cards.get(u["key"])
        entry = (u["record_key"], rec, card)
        if (not rec and not card) or entry in seen:
            continue
        seen.add(entry)
        parts = []
        if rec:
            parts.append(f"Failure Record {_code(_rel(rec, root), 240)}")
        if card:
            parts.append(f"evidence card {_code(_rel(card, root), 240)}")
        lines.append(f"- {_code(u['record_key'])}: " + ", ".join(parts))
    return lines


def _status_cell(status: str) -> str:
    return f"**{status}**" if status in _RED else status


def _diff_section(head_units: Sequence[Dict[str, Any]],
                  base_doc: Optional[Dict[str, Any]],
                  base_note: Optional[str], base_label: Optional[str],
                  kind: str, max_rows: int) -> List[str]:
    title = "### Compared with the base branch"
    if base_label:
        title += f" ({_code(base_label, 80)})"
    lines = [title, ""]
    if base_doc is None:
        lines.append(base_note or "No base result was supplied, so there is"
                     " nothing to compare against.")
        lines.append("")
        return lines
    if base_doc.get("kind") != kind:
        lines.append(f"The base result is a {_code(base_doc.get('kind'))}"
                     f" and this run is a {_code(kind)}; they are not"
                     " compared.")
        lines.append("")
        return lines
    d = diff_units(head_units, units_of(base_doc))
    lines.append(
        f"Fixed: **{len(d['fixed'])}** · Newly failing:"
        f" **{len(d['newly_failing'])}** · Still failing:"
        f" **{len(d['still_failing'])}**"
        + (f" · Now inconclusive: **{len(d['now_inconclusive'])}**"
           if d["now_inconclusive"] else "")
    )
    lines.append("")
    rows: List[str] = []
    for label, group in (("newly failing", d["newly_failing"]),
                         ("still failing", d["still_failing"]),
                         ("fixed", d["fixed"]),
                         ("now inconclusive", d["now_inconclusive"])):
        for u, bs in group:
            rows.append(f"| {label} | {_code(u['case'])} |"
                        f" {bs or 'not on base'} | {_status_cell(u['status'])} |")
    if rows:
        lines.append("| Change | Case | Base | This PR |")
        lines.append("|---|---|---|---|")
        lines.extend(rows[:max_rows])
        if len(rows) > max_rows:
            lines.append("")
            lines.append(f"+{len(rows) - max_rows} more changes (see the"
                         " machine result JSON).")
    else:
        lines.append("No case changed status against the base.")
    if d["removed"]:
        lines.append("")
        lines.append(f"{len(d['removed'])} case(s) on the base are not in"
                     " this run.")
    lines.append("")
    return lines


def _contract_moment(doc: Dict[str, Any]) -> List[str]:
    """The caught moment for a contract run: the first non-passing contract's
    measured number and its ASCII barge-in timeline (the same block
    ``hotato contract verify --pr-comment`` renders)."""
    from . import contract as _contract

    results = [r for r in doc.get("results") or [] if isinstance(r, dict)]
    red = [r for r in results if r.get("authenticity") == "tampered"
           or (r.get("scorable", True) and r.get("verdict_eligible", True)
               and not r.get("passed", True))]
    amber = [r for r in results if not r.get("passed", True)]
    # Hero a caught regression first; a not-scorable or refused contract only
    # when nothing regressed.
    hero = (red or amber or [None])[0]
    if hero is None:
        return []
    if hero.get("authenticity") == "tampered":
        said = "the bundle was edited after creation"
    elif not hero.get("scorable", True):
        said = "not scorable"
    elif not hero.get("verdict_eligible", True):
        said = "channel mapping unconfirmed (refused)"
    else:
        said = _contract_evidence(hero)
    lines = ["### Caught moment", "",
             f"{_code(hero.get('id'))} (expect"
             f" {_cell(hero.get('expect', 'yield'))}): **{_cell(said)}**", ""]
    lines += _contract._ascii_barge_timeline(hero)
    lines += ["", _contract._STORED_EVIDENCE_CAVEAT, ""]
    return lines


def render_pr_report(
    doc: Dict[str, Any],
    *,
    base_doc: Optional[Dict[str, Any]] = None,
    base_note: Optional[str] = None,
    base_label: Optional[str] = None,
    records: Optional[Dict[str, str]] = None,
    cards: Optional[Dict[str, str]] = None,
    run_url: Optional[str] = None,
    artifact_url: Optional[str] = None,
    artifact_root: Optional[str] = None,
    reproduce: Optional[str] = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> str:
    """The Markdown body of the sticky pull-request comment (and the job
    summary). ``doc`` is one machine result (see :data:`KINDS`); ``base_doc``
    the base branch's result of the same kind, or ``None`` with ``base_note``
    saying why there is none. ``records`` maps a test id to its Failure
    Record path, ``cards`` a case key to its evidence card path; both are
    shown relative to ``artifact_root`` (the root of the uploaded results
    artifact). ``artifact_url`` (preferred) or ``run_url`` is the link the
    evidence files are reached through. Deterministic: the same arguments
    always return the same string."""
    kind = doc.get("kind")
    if kind not in KINDS:
        raise ValueError(f"not a hotato machine result kind: {kind!r}")
    max_rows = max(1, int(max_rows))
    records = records or {}
    cards = cards or {}
    # A link target that is not a plain https URL is dropped, never rendered.
    artifact_url, run_url = safe_url(artifact_url), safe_url(run_url)
    link = artifact_url or run_url
    units = units_of(doc)
    verdict = verdict_of(doc, units)
    failing = [u for u in units if _class(u["status"]) != "pass"]
    failing.sort(key=lambda u: _STATUS_ORDER.index(u["status"])
                 if u["status"] in _STATUS_ORDER else 0)

    md: List[str] = [MARKER, f"## hotato: {verdict.upper()}", ""]
    code = doc.get("exit_code")
    md.append(f"{_subject(doc)}. {_count_line(kind, units)}."
              f" Exit code {code if isinstance(code, int) else '?'}.")
    md.append("")

    if failing:
        md.append(f"### Not passing ({len(failing)})")
        md.append("")
        md.append("| Status | Case | What failed | Evidence | Files |")
        md.append("|---|---|---|---|---|")
        for u in failing[:max_rows]:
            md.append(
                f"| {_status_cell(u['status'])} | {_code(u['case'])} |"
                f" {_cell(u['what'])} | {_cell(u['evidence']) or '-'} |"
                f" {_files_cell(u, records, cards, link)} |")
        if len(failing) > max_rows:
            md.append("")
            md.append(f"+{len(failing) - max_rows} more not shown (the"
                      " machine result JSON lists every case).")
        md.append("")
    else:
        md.append("Every case passed.")
        md.append("")

    if kind == "contract-verify":
        md.extend(_contract_moment(doc))

    md.extend(_diff_section(units, base_doc, base_note, base_label, kind,
                            max_rows))

    files = _file_list(failing[:max_rows], records, cards, artifact_root)
    if files:
        md.append("### Evidence")
        md.append("")
        if artifact_url:
            md.append(f"In the [results artifact]({artifact_url}) of this"
                      " run:")
        elif run_url:
            md.append(f"In the results artifact of [this workflow"
                      f" run]({run_url}):")
        else:
            md.append("Relative to the results directory:")
        md.append("")
        md.extend(files)
        md.append("")
    elif link:
        md.append(f"Results: [{'results artifact' if artifact_url else 'workflow run'}]({link}).")
        md.append("")

    advisory = _advisory_line(doc)
    if advisory:
        md.append(advisory)
        md.append("")
    if reproduce:
        md.append("Reproduce locally:")
        md.append("")
        md.append("```bash")
        md.append(" ".join(str(reproduce).split()).replace("`", "'"))
        md.append("```")
        md.append("")
    md.append("<sub>Deterministic checks decide this verdict; it re-renders"
              " byte-for-byte from the same result files. This comment is"
              " updated in place on every run.</sub>")
    body = "\n".join(md) + "\n"
    if len(body) > _MAX_BODY_CHARS:
        cut = body[:_MAX_BODY_CHARS]
        cut = cut[: cut.rfind("\n") + 1]
        body = (cut + "\n_Truncated to stay under the pull-request comment"
                " size limit; the machine result JSON is complete._\n")
    return body
