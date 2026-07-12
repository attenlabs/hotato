"""``hotato.scenario.v1``: parse + validate a deterministic simulation scenario.

A scenario file (schema ``schema/scenario.v1.json``) declares the deterministic
INPUT side of a simulation (Phase-2 design 2.1): the ground-truth the caller
holds, the caller's scripted turn sequence + behaviour, the environment, and a
variation matrix. It is not a verdict and it never scores anything -- the
scripted-caller renderer (:mod:`hotato.simulate`) turns it into a labelled
``origin=simulated`` conversation, and only the SEPARATE Phase-1 assert layer
scores that. This module is the honesty wall made structural for the scenario
file:

* A scenario never carries an ``overall_score`` -- rejected structurally here
  and in the JSON Schema, matching ``conversation_test`` / ``assert.v1``.
* The caller declares ONLY its own turns. A script turn's ``say`` is the caller
  speaking; there is no field for the agent's words, so a scenario can never put
  words in the agent's mouth (the "did not solve the task for the agent"
  invariant, enforced at the schema level rather than hoped for at render time).
* Malformed input raises ``ValueError`` immediately -- validation runs before
  any use, so a bad file never drives a partial simulation -- exactly the
  contract :func:`hotato.conversation_test.validate_conversation_test_doc` sets
  (the caller's usage-error / exit-2 path, see :mod:`hotato.errors`).

Reproducibility here is scoped to "a seeded replay is byte-identical", never
"the model is deterministic": there is no model in this slice. The ``seed`` and
``variation_matrix`` feed :mod:`hotato.simulate`'s deterministic per-run seeding.
"""

from __future__ import annotations

from typing import Any, Dict

from .assert_ import parse_assertions_yaml
from .errors import open_regular as _open_regular

__all__ = [
    "KIND",
    "VERSION",
    "DEFAULT_SPEAKING_RATE",
    "validate_scenario_doc",
    "parse_scenario",
    "load_scenario_file",
]

KIND = "hotato.scenario"
VERSION = 1

DEFAULT_SPEAKING_RATE = 1.0


def _reject_overall_score(obj: Any, where: str) -> None:
    """Reject an ``overall_score`` key wherever the honesty invariant forbids
    one. The schema forbids it structurally too; this is the same guard on the
    code path, so a hand-built dict can never slip a score past
    :func:`validate_scenario_doc`. A scenario scores NOTHING -- it is an input."""
    if isinstance(obj, dict) and "overall_score" in obj:
        raise ValueError(
            f"{where}: 'overall_score' is forbidden -- a scenario is a "
            "deterministic input, it never scores anything"
        )


def _validate_goal(goal: Any) -> None:
    if not isinstance(goal, dict):
        raise ValueError("'goal' is required and must be a mapping {type, target}")
    for field in ("type", "target"):
        val = goal.get(field)
        if not val or not isinstance(val, str):
            raise ValueError(f"goal.{field} is required and must be a non-empty string")


def _validate_turn(idx: int, turn: Any) -> None:
    """One scripted caller turn: a mapping with a non-empty ``say`` string and,
    at most, one label trigger (``when_agent_asks`` or ``after``). The absence of
    any agent-side field is the structural guarantee that a scenario can only
    ever declare the CALLER's words."""
    if not isinstance(turn, dict):
        raise ValueError(f"caller.script[{idx}] must be a mapping")
    say = turn.get("say")
    if not say or not isinstance(say, str):
        raise ValueError(
            f"caller.script[{idx}] is missing a non-empty string 'say' (the "
            "caller's spoken turn)"
        )
    triggers = [k for k in ("when_agent_asks", "after") if k in turn]
    if len(triggers) > 1:
        raise ValueError(
            f"caller.script[{idx}] has both {triggers[0]!r} and {triggers[1]!r}; "
            "a turn may carry at most one label trigger"
        )
    for k in triggers:
        if not turn[k] or not isinstance(turn[k], str):
            raise ValueError(
                f"caller.script[{idx}].{k} must be a non-empty label string"
            )


def _validate_behavior(behavior: Any) -> None:
    if behavior is None:
        return
    if not isinstance(behavior, dict):
        raise ValueError("caller.behavior must be a mapping")
    rate = behavior.get("speaking_rate", DEFAULT_SPEAKING_RATE)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
        raise ValueError(
            f"caller.behavior.speaking_rate must be a positive number, got {rate!r}"
        )
    interruptions = behavior.get("interruptions")
    if interruptions is not None:
        if not isinstance(interruptions, list):
            raise ValueError("caller.behavior.interruptions must be a list")
        for j, itr in enumerate(interruptions):
            if not isinstance(itr, dict):
                raise ValueError(f"caller.behavior.interruptions[{j}] must be a mapping")
            trig = itr.get("trigger")
            if not trig or not isinstance(trig, str):
                raise ValueError(
                    f"caller.behavior.interruptions[{j}] is missing a string 'trigger'"
                )
            off = itr.get("offset_ms")
            if isinstance(off, bool) or not isinstance(off, int) or off < 0:
                raise ValueError(
                    f"caller.behavior.interruptions[{j}].offset_ms must be an "
                    f"integer >= 0, got {off!r}"
                )
    bc = behavior.get("backchannels")
    if bc is not None:
        if not isinstance(bc, dict):
            raise ValueError("caller.behavior.backchannels must be a mapping")
        prob = bc.get("probability", 0.0)
        if isinstance(prob, bool) or not isinstance(prob, (int, float)) or not (
            0.0 <= prob <= 1.0
        ):
            raise ValueError(
                f"caller.behavior.backchannels.probability must be a number in "
                f"[0, 1], got {prob!r}"
            )


def _validate_caller(caller: Any) -> None:
    if not isinstance(caller, dict):
        raise ValueError("'caller' is required and must be a mapping")
    script = caller.get("script")
    if not isinstance(script, list) or not script:
        raise ValueError("caller.script is required and must be a non-empty list of turns")
    for idx, turn in enumerate(script):
        _validate_turn(idx, turn)
    _validate_behavior(caller.get("behavior"))


def _validate_variation_matrix(vm: Any) -> None:
    if vm is None:
        return
    if not isinstance(vm, dict):
        raise ValueError("'variation_matrix' must be a mapping")
    _reject_overall_score(vm, "variation_matrix")
    for name, itemtype in (
        ("locale", str), ("noise", str), ("behavior", str),
    ):
        vals = vm.get(name)
        if vals is not None:
            if not isinstance(vals, list) or not all(
                isinstance(v, str) for v in vals
            ):
                raise ValueError(
                    f"variation_matrix.{name} must be a list of strings"
                )
    rates = vm.get("speaking_rate")
    if rates is not None:
        if not isinstance(rates, list) or not all(
            (not isinstance(v, bool)) and isinstance(v, (int, float)) and v > 0
            for v in rates
        ):
            raise ValueError(
                "variation_matrix.speaking_rate must be a list of positive numbers"
            )
    reps = vm.get("repetitions", 1)
    if isinstance(reps, bool) or not isinstance(reps, int) or reps < 1:
        raise ValueError(
            f"variation_matrix.repetitions must be an integer >= 1, got {reps!r}"
        )


def validate_scenario_doc(doc: Any) -> Dict[str, Any]:
    """Validate a parsed scenario document and return a NORMALIZED copy with
    defaults applied (``facts`` -> ``{}``, ``caller.behavior.speaking_rate`` ->
    ``1.0`` when absent, ``seed`` -> ``0``). Raises ``ValueError`` on anything
    malformed: not a mapping; a wrong ``kind``/``version`` const; a missing
    ``id``/``goal``/``caller``; a bad goal/turn/behavior; a malformed
    ``variation_matrix``; a non-integer ``seed``; or a forbidden
    ``overall_score``.

    Nothing here renders or scores -- this is pure structural validation of the
    scenario file, run before any use, mirroring
    :func:`hotato.conversation_test.validate_conversation_test_doc`."""
    if not isinstance(doc, dict):
        raise ValueError(
            "scenario document must be a mapping with 'kind', 'version', 'id', "
            "'goal', and 'caller'"
        )
    _reject_overall_score(doc, "scenario document")

    if doc.get("kind") != KIND:
        raise ValueError(f"'kind' must be {KIND!r}, got {doc.get('kind')!r}")
    if doc.get("version") != VERSION:
        raise ValueError(
            f"unsupported scenario version {doc.get('version')!r}; this build "
            f"supports version {VERSION}"
        )

    sid = doc.get("id")
    if not sid or not isinstance(sid, str):
        raise ValueError("scenario is missing a string 'id'")

    _validate_goal(doc.get("goal"))

    facts = doc.get("facts", {})
    if not isinstance(facts, dict):
        raise ValueError("'facts' must be a mapping of the caller's ground-truth")

    _validate_caller(doc.get("caller"))

    env = doc.get("environment")
    if env is not None and not isinstance(env, dict):
        raise ValueError("'environment' must be a mapping")

    _validate_variation_matrix(doc.get("variation_matrix"))

    seed = doc.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError(f"'seed' must be an integer >= 0, got {seed!r}")

    # Normalized copy with defaults applied (the raw doc is never mutated).
    norm = dict(doc)
    norm["facts"] = dict(facts)
    norm["seed"] = seed
    caller = dict(norm["caller"])
    behavior = dict(caller.get("behavior") or {})
    behavior.setdefault("speaking_rate", DEFAULT_SPEAKING_RATE)
    caller["behavior"] = behavior
    norm["caller"] = caller
    return norm


def parse_scenario(text: str) -> Any:
    """Parse a scenario document from text. Reuses the dependency-free
    YAML-subset / JSON parser :func:`hotato.assert_.parse_assertions_yaml`, so
    scenario files stay zero-install (JSON, or the same small YAML subset the
    assertion / conversation-test files use). Raises ``ValueError`` on a
    malformed document. This only parses; call :func:`validate_scenario_doc` to
    validate."""
    return parse_assertions_yaml(text)


def load_scenario_file(path: str) -> Dict[str, Any]:
    """Load, parse, and validate a scenario file, returning the normalized doc.
    A FIFO/named-pipe path raises immediately (via
    :func:`hotato.errors.open_regular`) instead of blocking forever; a malformed
    document raises ``ValueError`` (the caller's exit-2 path)."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return validate_scenario_doc(parse_scenario(text))
