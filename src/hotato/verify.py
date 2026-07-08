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
        statement = (
            f"{n} of {m} fixtures that used to fail now pass"
            + (f", and {k} of {l} hold fixtures still pass" if l else "")
            + ". This improvement COINCIDES with your change; hotato measures "
            "timing and does not attribute cause."
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
    lines.append("  hotato reports coincidence, not causation.")
    return "\n".join(lines)
