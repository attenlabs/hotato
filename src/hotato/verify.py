"""``hotato verify``: battery-scale before/after proof that a fix actually held.

After you apply a config change (produced by ``hotato patch``) and RE-CAPTURE
the previously-failing fixtures, ``hotato verify`` scores the old and the new
envelope sets against each other and reports what really moved across the whole
battery -- not one cherry-picked clip:

  "N of M fixtures that used to fail now pass, and K of L hold fixtures still
   pass."

It REUSES the exact primitives the rest of Hotato is built on:

* the compare TAXONOMY (``compare.classify_pair``) for each event pair, so a
  fixture is marked with the same machine-stable word ``hotato compare`` uses:
  fixed, regressed, improved, worse, unchanged, still_pass, not_scorable;
* aggregate's pooled-distribution definitions (``hotato._stats.dist_summary``)
  for the before/after talk-over and time-to-yield shift.

Honesty rules, enforced here:

* verify reports COINCIDENCE, never causation: it says a change "coincides with"
  an improvement and never that it "caused" it. Hotato measures timing; it does
  not run a controlled experiment.
* it REFUSES a battery-scale claim when there are too few fixtures on the
  regression axis to characterize (``--min-n``): the per-fixture facts are still
  reported, but the headline proof is withheld and said so.
* an unjudgeable side is ``not_scorable`` (from the shared taxonomy), never an
  invented verdict; a fixture present on only one side is reported as unpaired,
  never silently dropped into the rollup.
* nothing here fabricates a number: every count and distribution is pooled from
  the envelopes' real measurements.

Inputs are hotato run envelopes: pass a single envelope JSON per side (a whole
battery run), or a directory of envelope JSONs, for ``--before`` and ``--after``.
Fixtures pair by ``event_id`` (falling back to ``scenario_id``).
"""

from __future__ import annotations

import json
import os
from typing import Optional, Tuple

from . import compare as _compare
from ._stats import dist_summary
from .aggregate import is_envelope

SCHEMA_ID = "hotato.verify.v1"

DEFAULT_MIN_N = 3


def _event_key(event: dict) -> Optional[str]:
    # A malformed / hand-edited side may carry a non-object entry in events[] (an
    # int, a string, null). It is not a fixture and has no key: return None so the
    # loader skips it cleanly instead of raising AttributeError on ``.get``.
    if not isinstance(event, dict):
        return None
    return event.get("event_id") or event.get("scenario_id")


def _load_side(path: str, label: str) -> Tuple[list, list]:
    """Return ``(envelopes, events)`` for one side. ``path`` is a single
    envelope JSON or a directory of them. Every event is tagged with its source
    file. Raises ValueError (exit 2) for unusable input."""
    envelopes = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if not name.endswith(".json"):
                continue
            fp = os.path.join(path, name)
            try:
                with open(fp, encoding="utf-8") as fh:
                    obj = json.load(fh)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"--{label} directory has an unreadable JSON {name!r}: {exc}"
                ) from exc
            if is_envelope(obj):
                envelopes.append((name, obj))
    elif os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError(f"--{label} {path!r} is not readable JSON: {exc}") from exc
        if not is_envelope(obj):
            raise ValueError(
                f"--{label} {path!r} is not a hotato run envelope (save one with "
                "hotato run --scenarios DIR --audio DIR --format json > run.json)"
            )
        envelopes.append((os.path.basename(path), obj))
    else:
        raise ValueError(f"--{label} {path!r} is not a file or directory")

    if not envelopes:
        raise ValueError(
            f"--{label} {path!r} had no hotato run envelopes to verify"
        )

    events = []
    seen: dict = {}
    for fname, env in envelopes:
        for ev in env.get("events", []):
            # A non-object events[] entry (int/str/null) is not a fixture: skip it
            # rather than crash on the malformed side.
            if not isinstance(ev, dict):
                continue
            key = _event_key(ev)
            if key is None:
                continue
            # A fixture key must be a hashable scalar (str or number). A hand-edited
            # side can carry a list/object event_id, which is unhashable and would
            # raise ``TypeError: unhashable type`` at ``key in seen``. Reject it as
            # a clean, named usage error (exit 2) instead.
            if not isinstance(key, (str, int, float)) or isinstance(key, bool):
                raise ValueError(
                    f"--{label} has a fixture in {fname!r} whose event_id / "
                    f"scenario_id is a {type(key).__name__}, not a string or "
                    "number; each fixture needs a scalar id so before/after "
                    "pairing is unambiguous."
                )
            if key in seen:
                raise ValueError(
                    f"--{label} has fixture {key!r} more than once "
                    f"({seen[key]} and {fname}); each fixture must be unique per "
                    "side so the before/after pairing is unambiguous"
                )
            seen[key] = fname
            tagged = dict(ev)
            tagged["_source_file"] = fname
            events.append(tagged)
    return envelopes, events


def _scorable(event: dict) -> bool:
    # An event is judgeable only if it is not flagged not-scorable AND actually
    # carries a verdict object WITH a `passed` field. A malformed / incomplete /
    # older-schema side (no verdict, or a verdict missing `passed`) is
    # not_scorable, never a crash -- matching compare.classify_pair's guard.
    v = event.get("verdict")
    return (event.get("scorable") is not False
            and isinstance(v, dict) and "passed" in v)


def _passed(event: dict) -> bool:
    return _scorable(event) and bool(event["verdict"]["passed"])


def _pooled(events: list) -> dict:
    tov, tty = [], []
    for e in events:
        if not _scorable(e):
            continue
        v = e.get("verdict") or {}
        # Only pool real numeric measurements: a malformed / hand-edited side can
        # carry a non-numeric talk_over_sec / seconds_to_yield (a string, a list),
        # which must never reach dist_summary's sort/round.
        tov_val = v.get("talk_over_sec")
        if isinstance(tov_val, (int, float)) and not isinstance(tov_val, bool):
            tov.append(tov_val)
        tty_val = v.get("seconds_to_yield")
        if isinstance(tty_val, (int, float)) and not isinstance(tty_val, bool):
            tty.append(tty_val)
    return {
        "talk_over_sec": dist_summary(tov),
        "seconds_to_yield": dist_summary(tty),
    }


def verify_sides(
    before_path: str,
    after_path: str,
    *,
    min_n: int = DEFAULT_MIN_N,
) -> dict:
    """Score the before set against the after set and return the proof dict.
    Pure and deterministic; raises ValueError (exit 2) for unusable input."""
    _b_envs, before_events = _load_side(before_path, "before")
    _a_envs, after_events = _load_side(after_path, "after")

    before_by = {_event_key(e): e for e in before_events}
    after_by = {_event_key(e): e for e in after_events}

    paired_keys = sorted(set(before_by) & set(after_by))
    only_before = sorted(set(before_by) - set(after_by))
    only_after = sorted(set(after_by) - set(before_by))
    if not paired_keys:
        raise ValueError(
            "no fixtures pair between --before and --after (matched on event_id, "
            "then scenario_id). Re-capture the SAME fixtures you ran before."
        )

    per_fixture = []
    counts = {w: 0 for w in _compare.RESULTS}
    used_to_fail = []
    now_pass = []
    hold_guards = []
    hold_guards_pass = []
    regressions = []

    for key in paired_keys:
        b, a = before_by[key], after_by[key]
        expect_yield = bool(b.get("expected_yield"))
        result = _compare.classify_pair(expect_yield, b, a)
        counts[result] += 1
        bv, av = b.get("verdict") or {}, a.get("verdict") or {}
        per_fixture.append({
            "fixture": key,
            "expect": "yield" if expect_yield else "hold",
            "result": result,
            "before": {
                "scorable": _scorable(b),
                "passed": _passed(b),
                "talk_over_sec": bv.get("talk_over_sec"),
                "seconds_to_yield": bv.get("seconds_to_yield"),
            },
            "after": {
                "scorable": _scorable(a),
                "passed": _passed(a),
                "talk_over_sec": av.get("talk_over_sec"),
                "seconds_to_yield": av.get("seconds_to_yield"),
            },
        })
        b_failed = _scorable(b) and not _passed(b)
        if b_failed:
            used_to_fail.append(key)
            if _passed(a):
                now_pass.append(key)
        if not expect_yield and _passed(b):
            hold_guards.append(key)
            if _passed(a):
                hold_guards_pass.append(key)
        if result in ("regressed", "worse"):
            regressions.append(key)

    m = len(used_to_fail)
    n = len(now_pass)
    l = len(hold_guards)
    k = len(hold_guards_pass)
    claim_supported = m >= min_n

    if claim_supported:
        head = (
            f"{n} of {m} fixtures that used to fail now pass"
            + (f", and {k} of {l} hold fixtures still pass" if l else "")
        )
        # Only call the outcome an "improvement" when something actually newly
        # passes AND nothing regressed. A zero-improvement or strictly-worse
        # battery must NOT be described as "This improvement"; say what happened.
        coincidence = (
            " hotato measures timing and does not attribute cause."
        )
        if n == 0 and regressions:
            statement = (
                head + f". This battery REGRESSED on {len(regressions)} "
                "fixture(s) and no fixture that used to fail now passes; this "
                "is not an improvement." + coincidence
            )
        elif n == 0:
            statement = (
                head + ". No fixture that used to fail now passes; this change "
                "did not improve the battery." + coincidence
            )
        elif regressions:
            statement = (
                head + f", but {len(regressions)} fixture(s) REGRESSED. This "
                "mixed result COINCIDES with your change;" + coincidence
            )
        else:
            statement = (
                head + ". This improvement COINCIDES with your change;"
                + coincidence
            )
    else:
        statement = (
            f"only {m} fixture(s) used to fail, below --min-n {min_n}: too few to "
            "state a battery-scale proof. The per-fixture results below still "
            "hold; this COINCIDES with your change but is too small a sample to "
            "characterize. No causal claim is made."
        )

    return {
        "tool": "hotato",
        "kind": "verify",
        "schema_version": "1",
        "offline": True,
        "min_n": min_n,
        "paired": len(paired_keys),
        "results": counts,
        "regression_axis": {
            "used_to_fail": m,
            "now_pass": n,
            "still_fail": m - n,
        },
        "hold_axis": {
            "hold_guards": l,
            "still_pass": k,
            "regressed": l - k,
        },
        "regressions": regressions,
        "claim": {
            "supported": claim_supported,
            "statement": statement,
            "relationship": "coincides_with",
        },
        "distribution": {
            "before": _pooled(before_events),
            "after": _pooled(after_events),
        },
        "unpaired": {"only_before": only_before, "only_after": only_after},
        "per_fixture": per_fixture,
    }


def render_text(v: dict) -> str:
    r = v["results"]
    ra = v["regression_axis"]
    ha = v["hold_axis"]
    lines = [
        f"hotato verify: {v['paired']} fixtures paired (before -> after)",
        f"  {ra['now_pass']} of {ra['used_to_fail']} that used to fail now pass; "
        f"{ha['still_pass']} of {ha['hold_guards']} hold fixtures still pass",
        "  results: "
        + ", ".join(f"{w}={r[w]}" for w in _compare.RESULTS if r[w]),
    ]
    if v["regressions"]:
        lines.append("  REGRESSIONS: " + ", ".join(v["regressions"]))
    claim = v["claim"]
    tag = "CLAIM" if claim["supported"] else "REFUSED (low n)"
    lines.append(f"  {tag}: {claim['statement']}")
    for name, side in (("before", v["distribution"]["before"]),
                       ("after", v["distribution"]["after"])):
        tov = side["talk_over_sec"]
        tov_s = "no measurements" if not tov else f"p95 {tov['p95']:.2f}s (n={tov['n']})"
        lines.append(f"  talk-over {name}: {tov_s}")
    up = v["unpaired"]
    if up["only_before"]:
        lines.append("  only in before (unpaired): " + ", ".join(up["only_before"]))
    if up["only_after"]:
        lines.append("  only in after (unpaired): " + ", ".join(up["only_after"]))
    pol = v.get("policy")
    if pol:
        tag = "PASSED" if pol["passed"] else "FAILED"
        src = pol.get("source")
        lines.append(f"  POLICY {tag}" + (f" ({src})" if src else "")
                     + ": guardrails are hard fails; targets are success criteria")
        for g in pol["guardrails"]:
            mark = "ok" if g["ok"] else "VIOLATED"
            lines.append(f"    guardrail {g['name']}: {mark} ({g['detail']})")
        for t in pol["targets"]:
            mark = "met" if t["met"] else "NOT met"
            lines.append(f"    target {t['metric']}: {mark} ({t['detail']})")
    lines.append("  hotato reports coincidence, not causation.")
    return "\n".join(lines)


# --- self-contained HTML proof artifact -----------------------------------
#
# ``hotato verify --out verify.html`` renders the same before/after proof as a
# single offline HTML file (zero external assets). It REUSES report.py's house
# style verbatim, and it reads ONLY the numbers verify_sides already measured;
# nothing here re-scores or invents a value. The page is the flagship "did the
# fix hold" artifact, and it is honest by construction:
#
# * the headline PASSED/FAILED is tied to the SAME bar verify already enforces
#   (a battery-scale claim needs --min-n previously-failing fixtures AND at
#   least one now passing AND no regression); the low-n refusal never earns a
#   PASSED stamp;
# * the TARGET section shows the failure it set out to move (talk-over p95 and
#   the failing-fixture count, before -> after);
# * the OPPOSITE-RISK section shows what a naive threshold bandaid would break
#   (hold / backchannel fixtures and the false-yield count, before -> after),
#   so a "fix" that just makes the agent yield to everything is caught;
# * the conclusion says COINCIDENCE, never causation, and states plainly what
#   the artifact does not prove.

_EXTRA_CSS = """
.chip.verdict{font-size:13.5px;letter-spacing:0.06em}
.vgood{color:%(muted)s} .vbad{color:%(muted)s}
.cmpcap{color:%(muted)s;font-size:12.5px;margin:2px 0 10px}
td.delta.good{color:%(green)s} td.delta.bad{color:%(red)s}
td.delta.flat{color:%(muted)s}
.gtag{font-weight:700;font-size:11px;letter-spacing:0.04em;padding:2px 8px;
 border-radius:6px;color:#15110d}
.concl{margin-top:18px;background:%(card2)s;border:1px solid %(line)s;
 border-left:3px solid %(ember)s;border-radius:10px;padding:13px 16px;
 font-size:14px;line-height:1.55}
.concl b{color:%(ember)s}
.notprove{color:%(muted)s;font-size:12.5px;margin-top:9px}
"""


def _p95(dist: Optional[dict]):
    return dist.get("p95") if isinstance(dist, dict) else None


def _fmt_s(x) -> str:
    return "no measurement" if x is None else f"{x:.2f}s"


def verdict_model(v: dict) -> dict:
    """Derive the PASSED/FAILED verdict and the target / opposite-risk numbers
    the HTML report shows, entirely from the numbers ``verify_sides`` already
    measured. Pure and deterministic; invents nothing.

    PASSED is deliberately tied to the SAME honesty bar verify enforces on the
    text/JSON claim: the battery-scale claim must be supported (>= --min-n
    previously-failing fixtures), at least one such fixture must now pass, AND
    nothing may have regressed. A low-n battery, a zero-improvement battery, or
    any regression is FAILED -- it never earns the flagship PASSED stamp.
    """
    ra, ha = v["regression_axis"], v["hold_axis"]
    now_pass, used_to_fail = ra["now_pass"], ra["used_to_fail"]
    regressed_any = bool(v["regressions"])
    supported = bool(v["claim"]["supported"])
    passed = supported and now_pass > 0 and not regressed_any

    pf = v.get("per_fixture", [])
    before_failed = sum(
        1 for r in pf if r["before"]["scorable"] and not r["before"]["passed"])
    after_failed = sum(
        1 for r in pf if r["after"]["scorable"] and not r["after"]["passed"])
    false_yield_before = sum(
        1 for r in pf if r["expect"] == "hold"
        and r["before"]["scorable"] and not r["before"]["passed"])
    false_yield_after = sum(
        1 for r in pf if r["expect"] == "hold"
        and r["after"]["scorable"] and not r["after"]["passed"])

    b_p95 = _p95(v["distribution"]["before"]["talk_over_sec"])
    a_p95 = _p95(v["distribution"]["after"]["talk_over_sec"])

    if passed:
        conclusion = ("Timing improved on this battery. "
                      "Hotato reports coincidence, not causation.")
    elif regressed_any:
        conclusion = (
            f"This battery regressed on {len(v['regressions'])} fixture(s) and "
            "did not clear the fix check. "
            "Hotato reports coincidence, not causation.")
    elif not supported:
        conclusion = (
            f"Only {used_to_fail} fixture(s) used to fail, below the --min-n "
            f"{v['min_n']} needed to prove a fix at battery scale. "
            "Hotato reports coincidence, not causation.")
    else:
        conclusion = ("No fixture that used to fail now passes on this battery. "
                      "Hotato reports coincidence, not causation.")

    return {
        "passed": passed,
        "verdict": "PASSED" if passed else "FAILED",
        "supported": supported,
        "now_pass": now_pass,
        "used_to_fail": used_to_fail,
        "before_failed": before_failed,
        "after_failed": after_failed,
        "talk_over_p95_before": b_p95,
        "talk_over_p95_after": a_p95,
        "hold_guards": ha["hold_guards"],
        "hold_still_pass": ha["still_pass"],
        "new_false_yields": ha["regressed"],
        "false_yield_before": false_yield_before,
        "false_yield_after": false_yield_after,
        "not_scorable": v["results"].get("not_scorable", 0),
        "regressions": list(v["regressions"]),
        "conclusion": conclusion,
    }


def _delta_cell(before, after, *, lower_is_better: bool):
    """A '(improved)/(worse)/(no change)' delta cell for a before/after metric,
    green when it moved the good way. Returns (text, css_class). Missing
    measurements degrade to a flat, honest 'n/a', never a fabricated arrow."""
    if before is None or after is None:
        return "n/a", "flat"
    d = round(after - before, 3)
    if abs(d) < 0.0005:
        return "no change", "flat"
    good = (d < 0) if lower_is_better else (d > 0)
    word = "improved" if good else "worse"
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.2f} ({word})", ("good" if good else "bad")


def _cmp_table(esc, rows) -> str:
    """A before -> after comparison table. Each row is
    (label, before_str, after_str, delta_text, delta_class)."""
    body = []
    for label, b, a, dtext, dclass in rows:
        body.append(
            f'<tr><td>{esc(label)}</td>'
            f'<td class="mono">{esc(b)}</td>'
            f'<td class="mono">{esc(a)}</td>'
            f'<td class="delta {dclass}">{esc(dtext)}</td></tr>'
        )
    return (
        '<table class="basetab"><thead><tr>'
        '<th>measure</th><th>before</th><th>after</th><th>change</th>'
        '</tr></thead><tbody>' + "".join(body) + '</tbody></table>'
    )


def _policy_section(esc, C, pol: dict) -> str:
    """The policy card: every guardrail and target with an ok/violated or
    met/unmet tag, reusing the gtag chip. Reads only the evaluated policy."""
    def tag(ok, yes, no):
        c = C["green"] if ok else C["red"]
        return (f'<span class="gtag" style="background:{c}">'
                f'{yes if ok else no}</span>')

    grows = []
    for g in pol["guardrails"]:
        grows.append(
            f'<tr><td class="mono">{esc(g["name"])}</td>'
            f'<td class="mono">{esc(str(g["spec"]))}</td>'
            f'<td class="mono">{esc(str(g["observed"]))}</td>'
            f'<td>{tag(g["ok"], "ok", "violated")}</td></tr>')
    gtable = (
        '<table class="basetab"><thead><tr><th>guardrail</th><th>limit</th>'
        '<th>observed</th><th>status</th></tr></thead><tbody>'
        + "".join(grows) + '</tbody></table>') if grows else ""

    def fval(t, which):
        x = t[which]
        if x is None:
            return "n/a"
        return (f"{x:.2f}s"
                if POLICY_TARGET_METRICS[t["metric"]]["seconds"] else str(x))

    trows = []
    for t in pol["targets"]:
        trows.append(
            f'<tr><td class="mono">{esc(t["metric"])}</td>'
            f'<td class="mono">{esc(str(t["spec"]))}</td>'
            f'<td class="mono">{esc(fval(t, "before"))} -&gt; '
            f'{esc(fval(t, "after"))}</td>'
            f'<td>{tag(t["met"], "met", "unmet")}</td></tr>')
    ttable = (
        '<table class="basetab"><thead><tr><th>target</th><th>goal</th>'
        '<th>before -&gt; after</th><th>status</th></tr></thead><tbody>'
        + "".join(trows) + '</tbody></table>') if trows else ""

    verdict = "PASSED" if pol["passed"] else "FAILED"
    src = pol.get("source")
    cap = (
        'Checked against ' + (f'<span class="mono">{esc(src)}</span>' if src
                              else 'the supplied policy')
        + '. Guardrails are hard fail conditions; targets are the success '
        'criteria. The change passes only if every guardrail holds AND every '
        'target is met, so an improvement on one axis cannot hide a regression '
        'on the other.')
    return (
        '<section class="card"><div class="ctitle">Policy check: '
        f'{verdict}</div><div class="cmpcap">{cap}</div>'
        + gtable + ttable + '</section>')


def render_html(v: dict) -> str:
    """Render the verify proof as ONE self-contained, offline HTML file, reusing
    report.py's house style. Reads only the measured numbers in ``v``."""
    from . import report as _report

    esc = _report._esc
    C = _report._C
    m = verdict_model(v)
    pol = v.get("policy")
    # When a policy gates the run, the headline verdict IS the policy verdict
    # (every guardrail held AND every target met); otherwise it is verify's own
    # min-n / regression bar.
    passed = pol["passed"] if pol else m["passed"]
    verdict_word = "PASSED" if passed else "FAILED"
    chip_label = "Policy check" if pol else "Fix verification"
    chip_c = C["green"] if passed else C["red"]

    css = _report._CSS + (_EXTRA_CSS % C)

    head = (
        '<header class="top"><div class="logo"></div><div>'
        '<h1 class="h1">hotato verify</h1>'
        '<div class="tagline">Battery-scale before/after proof that a fix '
        'held.</div>'
        '<div class="subtle">Every value below is a real measurement pooled '
        'from the before and after run envelopes. Nothing is re-scored here.</div>'
        '<div class="metarow">'
        f'<span class="pill">paired <b>{v["paired"]}</b></span>'
        f'<span class="pill">min-n <b>{v["min_n"]}</b></span>'
        '<span class="pill">offline <b>yes</b></span>'
        '</div></div></header>'
    )

    summary = (
        '<div class="summary">'
        f'<div><div class="bignum">{m["now_pass"]} of {m["used_to_fail"]}</div>'
        f'<div class="subtle" style="color:{C["muted"]}">fixtures that used to '
        'fail now pass</div></div>'
        f'<div class="chip verdict" style="background:{chip_c}">'
        f'{chip_label}: {verdict_word}</div>'
        '</div>'
    )

    # TARGET: the failure the fix set out to move.
    target_rows = [
        ("talk-over p95",
         _fmt_s(m["talk_over_p95_before"]), _fmt_s(m["talk_over_p95_after"]),
         *_delta_cell(m["talk_over_p95_before"], m["talk_over_p95_after"],
                      lower_is_better=True)),
        ("failing fixtures",
         str(m["before_failed"]), str(m["after_failed"]),
         *_delta_cell(m["before_failed"], m["after_failed"],
                      lower_is_better=True)),
    ]
    target = (
        '<section class="card"><div class="ctitle">Target failure '
        'improvement</div>'
        '<div class="cmpcap">The failure this change set out to move: the '
        'pooled talk-over p95 across the battery, and how many fixtures still '
        f'fail. {m["now_pass"]} of {m["used_to_fail"]} previously-failing '
        'fixtures now pass.</div>'
        + _cmp_table(esc, target_rows) + '</section>'
    )

    # OPPOSITE RISK: what a naive threshold bandaid would silently break.
    fy_text, fy_class = _delta_cell(
        m["false_yield_before"], m["false_yield_after"], lower_is_better=True)
    nfy = m["new_false_yields"]
    ns = m["not_scorable"]
    guard_fy = ('<span class="gtag" style="background:' + C["green"] + '">ok</span>'
                if nfy == 0 else
                '<span class="gtag" style="background:' + C["red"] + '">violated</span>')
    guard_ns = ('<span class="gtag" style="background:' + C["green"] + '">ok</span>'
                if ns == 0 else
                '<span class="gtag" style="background:' + C["red"] + '">violated</span>')
    opp_rows = [
        ("hold / backchannel fixtures still passing",
         f'{m["hold_still_pass"]} of {m["hold_guards"]}',
         f'{m["hold_still_pass"]} of {m["hold_guards"]}',
         "unchanged" if nfy == 0 else f"{nfy} now fail", "flat" if nfy == 0 else "bad"),
        ("false yields (hold fixtures that yielded)",
         str(m["false_yield_before"]), str(m["false_yield_after"]),
         fy_text, fy_class),
    ]
    opp = (
        '<section class="card"><div class="ctitle">Opposite-risk check</div>'
        '<div class="cmpcap">The check a threshold bandaid would fail: a fix '
        'that just makes the agent yield to everything trades talk-over for '
        'false yields on hold and backchannel fixtures. Those must not have '
        'regressed.</div>'
        + _cmp_table(esc, opp_rows)
        + '<div class="stats">'
        + f'<div class="stat"><span class="k">new false yields introduced</span>'
        + f'<span class="v">{nfy} &nbsp; {guard_fy}</span></div>'
        + f'<div class="stat"><span class="k">not-scorable pairs</span>'
        + f'<span class="v">{ns} &nbsp; {guard_ns}</span></div>'
        + '</div></section>'
    )

    # Per-fixture facts, never dropped.
    frows = []
    for r in v["per_fixture"]:
        b_tov = r["before"]["talk_over_sec"]
        a_tov = r["after"]["talk_over_sec"]
        b_s = _fmt_s(b_tov) if isinstance(b_tov, (int, float)) else "n/a"
        a_s = _fmt_s(a_tov) if isinstance(a_tov, (int, float)) else "n/a"
        frows.append(
            f'<tr><td class="mono">{esc(r["fixture"])}</td>'
            f'<td>{esc(r["expect"])}</td>'
            f'<td>{esc(r["result"])}</td>'
            f'<td class="mono">{esc(b_s)} -&gt; {esc(a_s)}</td></tr>'
        )
    per_fixture = (
        '<section class="card"><div class="ctitle">Per-fixture results</div>'
        '<div class="cmpcap">Every paired fixture and the machine-stable '
        'compare word for it (fixed, regressed, improved, worse, unchanged, '
        'still_pass, not_scorable), with its talk-over before and after.</div>'
        '<table class="basetab"><thead><tr><th>fixture</th><th>expect</th>'
        '<th>result</th><th>talk-over</th></tr></thead><tbody>'
        + "".join(frows) + '</tbody></table>'
    )

    # Unpaired fixtures are reported, never silently dropped.
    up = v["unpaired"]
    unpaired = ""
    if up["only_before"] or up["only_after"]:
        parts = []
        if up["only_before"]:
            parts.append('<div class="cmpcap">only in before (unpaired): '
                         f'{esc(", ".join(up["only_before"]))}</div>')
        if up["only_after"]:
            parts.append('<div class="cmpcap">only in after (unpaired): '
                         f'{esc(", ".join(up["only_after"]))}</div>')
        unpaired = (
            '<section class="card"><div class="ctitle">Unpaired fixtures</div>'
            '<div class="cmpcap">Present on only one side, so they carry no '
            'before/after pair. Reported here, never folded into the rollup.'
            '</div>' + "".join(parts) + '</section>'
        )

    policy_html = _policy_section(esc, C, pol) if pol else ""

    if pol:
        verb = "passes" if passed else "does not pass"
        why = ("every guardrail held and every target was met. " if passed
               else "at least one guardrail was violated or a target was not "
               "met. ")
        conclusion = (f"Against your policy this change {verb}: " + why
                      + "Hotato reports coincidence, not causation.")
    else:
        conclusion = m["conclusion"]

    concl = (
        '<div class="concl">'
        f'<b>{esc(conclusion)}</b>'
        f'<div class="notprove">Claim: {esc(v["claim"]["statement"])}</div>'
        '<div class="notprove">What this does not prove: Hotato measures '
        'timing only. It does not run a controlled experiment, does not '
        'attribute cause, and does not judge whether a turn was semantically '
        'correct. The before/after coincides with your change; that is all it '
        'says.</div></div>'
    )

    body = (
        f'<div class="wrap">{head}<main>{summary}'
        f'{policy_html}{target}{opp}{per_fixture}{unpaired}{concl}</main></div>'
    )

    title = f"hotato verify: {chip_label.lower()} {verdict_word}"
    desc = (
        f"Self-contained hotato verify proof: {m['now_pass']} of "
        f"{m['used_to_fail']} previously-failing fixtures now pass across a "
        "battery, with the target talk-over shift and the opposite-risk "
        "false-yield check, before and after. Offline; Hotato reports "
        "coincidence, not causation."
    )
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title>"
        f"<meta name=\"description\" content=\"{esc(desc)}\">"
        f"<style>{css}</style></head><body>{body}</body></html>\n"
    )


# --- hotato.verify.yaml policy --------------------------------------------
#
# ``hotato verify --policy hotato.verify.yaml`` turns the measured before/after
# rollup into a PASS/FAIL gate against a small, declared policy. The policy has
# two parts, and BOTH must hold for verify to pass:
#
# * ``target.improve`` -- the success criteria: the failures the fix set out to
#   move (e.g. ``talk_over_sec_p95: -0.5`` = the pooled talk-over p95 must drop
#   by at least 0.5s; ``failed_count: decrease`` = fewer fixtures may fail).
# * ``guardrails`` -- HARD fail conditions that catch the opposite risk a naive
#   threshold bandaid trades into: ``max_new_false_yields`` and
#   ``max_not_scorable`` cap regressions on the hold / not-scorable axes, and
#   ``require_hold_fixture`` / ``require_yield_fixture`` refuse to certify a
#   battery that does not even TEST the opposite axis.
#
# This is the anti-bandaid mechanism: verify passes only when every guardrail
# holds AND every target is met, so you cannot pass by improving one axis while
# regressing (or never testing) the other. A patch that cuts talk-over by making
# the agent yield to everything meets the talk-over target but trips
# ``max_new_false_yields`` on the hold fixtures, and the whole check fails.
#
# The policy file is parsed with the STANDARD LIBRARY only: Hotato's core
# carries no third-party runtime dependency (PyYAML included), so ``verify``
# stays zero-install and its proof stays reproducible regardless of what happens
# to be importable. The supported subset is exactly what the shipped
# ``examples/verify-policy/hotato.verify.yaml`` uses (space-indented block
# mappings, up to two levels deep, scalar leaves, ``#`` comments); anything
# outside it is a clean exit-2 error, never a silent misread.

# Each target metric maps to a before/after pair verify already measured. All of
# Hotato's failure metrics are lower-is-better (less talk-over, fewer failures).
POLICY_TARGET_METRICS = {
    "talk_over_sec_p95": {"label": "talk-over p95", "seconds": True},
    "seconds_to_yield_p95": {"label": "time-to-yield p95", "seconds": True},
    "failed_count": {"label": "failing fixtures", "seconds": False},
    "false_yield_count": {"label": "false yields", "seconds": False},
}

# Guardrails split by prefix: ``max_*`` cap an observed count; ``require_*``
# demand a fixture class be present so the opposite axis is actually tested.
POLICY_MAX_GUARDRAILS = ("max_new_false_yields", "max_not_scorable")
POLICY_REQUIRE_GUARDRAILS = ("require_hold_fixture", "require_yield_fixture")
POLICY_GUARDRAILS = POLICY_MAX_GUARDRAILS + POLICY_REQUIRE_GUARDRAILS

# The keyword forms a target spec may take instead of a numeric delta.
POLICY_TARGET_KEYWORDS = ("decrease", "increase", "no_worse", "no_better",
                          "unchanged")


def _policy_strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment (one that begins the line or follows
    whitespace), leaving text inside quotes untouched. A ``#`` glued to a value
    (``key: a#b``) is kept, matching YAML."""
    out = []
    quote = None
    prev_ws = True
    for ch in line:
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev_ws = False
            continue
        if ch == "#" and prev_ws:
            break
        out.append(ch)
        prev_ws = ch in (" ", "\t")
    return "".join(out)


def _policy_scalar(raw: str):
    """Coerce a bare YAML scalar to a Python value over the documented subset
    (bool, null, int, float, quoted or bare string), agreeing with PyYAML on the
    tokens the schema actually uses."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_verify_policy(text: str) -> dict:
    """Parse the ``hotato.verify.yaml`` subset into a nested dict using only the
    standard library. Supports space-indented block mappings with scalar leaves;
    a tab indent, a list, or deeper-than-mapping nesting is a ValueError."""
    root: dict = {}
    # (indent, mapping) for each currently-open parent, outermost first.
    stack = [(-1, root)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        lead = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in lead:
            raise ValueError(
                f"line {lineno}: tab indentation is not allowed; use spaces")
        line = _policy_strip_comment(raw)
        if not line.strip():
            continue
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        body = stripped.rstrip()
        if body.lstrip().startswith("- "):
            raise ValueError(
                f"line {lineno}: lists are not part of the policy schema")
        if ":" not in body:
            raise ValueError(
                f"line {lineno}: expected 'key: value' or 'key:', got {body!r}")
        key, _sep, rawval = body.partition(":")
        key = key.strip()
        if not key:
            raise ValueError(f"line {lineno}: a key is empty")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            raise ValueError(f"line {lineno}: indentation does not nest cleanly")
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            raise ValueError(
                f"line {lineno}: {key!r} is nested under a scalar value")
        if key in parent:
            raise ValueError(f"line {lineno}: duplicate key {key!r}")
        val = rawval.strip()
        if val == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _policy_scalar(val)
    return root


def _normalize_policy(raw, source: Optional[str]) -> dict:
    """Validate a parsed policy against the schema and return the canonical
    ``{source, target:{improve:{...}}, guardrails:{...}}`` form. Raises
    ValueError (exit 2) for any unknown key or wrong-typed value, so a typo in
    the policy never silently passes."""
    if not isinstance(raw, dict):
        raise ValueError("policy must be a mapping of target / guardrails")
    unknown_top = set(raw) - {"target", "guardrails"}
    if unknown_top:
        raise ValueError(
            "unknown top-level key(s) " + ", ".join(sorted(unknown_top))
            + "; the policy has only 'target' and 'guardrails'")

    improve: dict = {}
    target = raw.get("target")
    if target is not None:
        if not isinstance(target, dict):
            raise ValueError("'target' must be a mapping with an 'improve' block")
        unknown = set(target) - {"improve"}
        if unknown:
            raise ValueError(
                "unknown key(s) under 'target': " + ", ".join(sorted(unknown)))
        block = target.get("improve") or {}
        if not isinstance(block, dict):
            raise ValueError("'target.improve' must be a mapping of metric: goal")
        for metric, spec in block.items():
            if metric not in POLICY_TARGET_METRICS:
                raise ValueError(
                    f"unknown target metric {metric!r}; allowed: "
                    + ", ".join(sorted(POLICY_TARGET_METRICS)))
            if isinstance(spec, bool) or not isinstance(spec, (int, float, str)):
                raise ValueError(
                    f"target {metric!r} must be a signed number (a required "
                    "delta, e.g. -0.5) or a keyword "
                    f"({', '.join(POLICY_TARGET_KEYWORDS)})")
            if isinstance(spec, str) and spec.lower() not in POLICY_TARGET_KEYWORDS:
                raise ValueError(
                    f"target {metric!r} keyword {spec!r} is not one of "
                    + ", ".join(POLICY_TARGET_KEYWORDS))
            improve[metric] = spec.lower() if isinstance(spec, str) else spec

    guardrails: dict = {}
    graw = raw.get("guardrails")
    if graw is not None:
        if not isinstance(graw, dict):
            raise ValueError("'guardrails' must be a mapping")
        for name, spec in graw.items():
            if name not in POLICY_GUARDRAILS:
                raise ValueError(
                    f"unknown guardrail {name!r}; allowed: "
                    + ", ".join(POLICY_GUARDRAILS))
            if name in POLICY_MAX_GUARDRAILS:
                if isinstance(spec, bool) or not isinstance(spec, int) or spec < 0:
                    raise ValueError(
                        f"guardrail {name!r} must be a non-negative integer cap")
            else:  # require_*
                if not isinstance(spec, bool):
                    raise ValueError(
                        f"guardrail {name!r} must be true or false")
            guardrails[name] = spec

    if not improve and not guardrails:
        raise ValueError(
            "policy is empty: declare at least one target.improve metric or a "
            "guardrail")
    return {"source": source,
            "target": {"improve": improve},
            "guardrails": guardrails}


def load_policy(path: str) -> dict:
    """Read and validate a ``hotato.verify.yaml`` policy file. Raises ValueError
    (exit 2) for an unreadable or invalid policy."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError(f"--policy {path!r} is not readable: {exc}") from exc
    try:
        raw = _parse_verify_policy(text)
    except ValueError as exc:
        raise ValueError(f"--policy {path!r} is not valid: {exc}") from exc
    return _normalize_policy(raw, path)


def _eval_target(metric: str, spec, before, after) -> Tuple[bool, Optional[float], str]:
    """Evaluate one target.improve criterion. Returns ``(met, delta, detail)``.
    A missing measurement on either side is honestly NOT met, never a guess."""
    seconds = POLICY_TARGET_METRICS[metric]["seconds"]
    unit = "s" if seconds else ""

    def fmt(x):
        if x is None:
            return "n/a"
        return (f"{x:.2f}{unit}" if seconds else f"{x}")

    if before is None or after is None:
        return False, None, (
            f"no measurement to compare ({fmt(before)} -> {fmt(after)})")
    delta = round(after - before, 4)
    ds = f"{delta:+.2f}{unit}" if seconds else f"{delta:+g}"
    if isinstance(spec, str):
        kw = spec.lower()
        if kw == "decrease":
            met, need = after < before, "decrease"
        elif kw == "increase":
            met, need = after > before, "increase"
        elif kw == "no_worse":
            met, need = after <= before, "not get worse"
        elif kw == "no_better":
            met, need = after >= before, "not get better"
        else:  # unchanged
            met, need = abs(delta) < 1e-9, "stay unchanged"
        return met, delta, (
            f"must {need}: {fmt(before)} -> {fmt(after)} (delta {ds})")
    # numeric spec: a required signed delta (lower-is-better, so <= spec)
    met = delta <= spec + 1e-9
    goal = f"{spec:+.2f}{unit}" if seconds else f"{spec:+g}"
    return met, delta, (
        f"delta must be <= {goal}: {fmt(before)} -> {fmt(after)} (delta {ds})")


def _eval_guardrail(name: str, spec, observed: int) -> Tuple[bool, str]:
    """Evaluate one guardrail. Returns ``(ok, detail)``."""
    if name in POLICY_MAX_GUARDRAILS:
        return observed <= spec, f"{observed} observed, cap is {spec}"
    # require_* : the class must be present when true
    klass = "hold" if "hold" in name else "yield"
    if not spec:
        return True, f"not required ({observed} {klass} fixture(s) present)"
    ok = observed >= 1
    return ok, (
        f"{observed} {klass} fixture(s) in the battery" if ok else
        f"no {klass} fixture in the battery; the opposite-risk axis is untested")


def evaluate_policy(v: dict, policy: dict) -> dict:
    """Score a ``verify_sides`` result against a normalized policy. Reads only
    numbers verify already measured; invents nothing. Returns the policy
    evaluation dict attached to the proof under ``["policy"]``.

    ``passed`` is true only when EVERY guardrail holds AND EVERY target is met,
    so a fix cannot pass by moving one axis while regressing (or never testing)
    the other -- the anti-bandaid gate."""
    m = verdict_model(v)
    b_tty = _p95(v["distribution"]["before"]["seconds_to_yield"])
    a_tty = _p95(v["distribution"]["after"]["seconds_to_yield"])
    metric_ba = {
        "talk_over_sec_p95": (m["talk_over_p95_before"], m["talk_over_p95_after"]),
        "seconds_to_yield_p95": (b_tty, a_tty),
        "failed_count": (m["before_failed"], m["after_failed"]),
        "false_yield_count": (m["false_yield_before"], m["false_yield_after"]),
    }
    per = v.get("per_fixture", [])
    observed = {
        "max_new_false_yields": m["new_false_yields"],
        "max_not_scorable": m["not_scorable"],
        "require_hold_fixture": sum(1 for r in per if r["expect"] == "hold"),
        "require_yield_fixture": sum(1 for r in per if r["expect"] == "yield"),
    }

    targets = []
    for metric, spec in policy["target"]["improve"].items():
        before, after = metric_ba[metric]
        met, delta, detail = _eval_target(metric, spec, before, after)
        targets.append({
            "metric": metric,
            "label": POLICY_TARGET_METRICS[metric]["label"],
            "spec": spec, "before": before, "after": after,
            "delta": delta, "met": met, "detail": detail,
        })

    guardrails = []
    for name, spec in policy["guardrails"].items():
        ok, detail = _eval_guardrail(name, spec, observed[name])
        guardrails.append({
            "name": name, "spec": spec, "observed": observed[name],
            "ok": ok, "detail": detail,
        })

    targets_met = all(t["met"] for t in targets)
    guardrails_ok = all(g["ok"] for g in guardrails)
    return {
        "source": policy.get("source"),
        "passed": targets_met and guardrails_ok,
        "targets_met": targets_met,
        "guardrails_ok": guardrails_ok,
        "targets": targets,
        "guardrails": guardrails,
    }
