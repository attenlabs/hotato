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

import itertools
import json
import math
import re
from typing import Any, Dict, List

from .assert_ import parse_assertions_yaml
from .errors import (
    check_kind_version as _check_kind_version,
)
from .errors import (
    open_regular as _open_regular,
)
from .errors import (
    reject_overall_score as _reject_overall_score_impl,
)

__all__ = [
    "KIND",
    "VERSION",
    "DEFAULT_SPEAKING_RATE",
    "EXAMPLE_FILENAME",
    "validate_scenario_doc",
    "parse_scenario",
    "load_scenario_file",
    "build_starter",
    "example_scenario_path",
    "variable_references",
    "substitute_variables",
    "variable_combinations",
    "node_say_lines",
    "enumerate_branch_paths",
]

KIND = "hotato.scenario"
VERSION = 1

DEFAULT_SPEAKING_RATE = 1.0

# The minimal scenario shipped INSIDE the package (installed with the wheel under
# ``hotato/data/simulate/``) so ``hotato simulate --example`` runs from a bare
# ``pip install`` with no example file on disk. Its bytes are produced by
# :func:`build_starter` -- one source of truth, pinned by a test.
EXAMPLE_FILENAME = "quickstart.scenario.json"
EXAMPLE_SCENARIO_ID = "simulate-quickstart"

# A ``{name}`` template reference inside a caller ``say`` line: an identifier in
# braces. Only a valid identifier matches, so literal braces around anything else
# (e.g. code in a transcript) are left untouched -- a scenario with no template
# refs and no ``variables`` block is byte-identical to before this feature.
_VARIABLE_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def variable_references(text: str) -> "set[str]":
    """The set of ``{name}`` template variable names referenced in ``text``. A
    plain string with no ``{identifier}`` yields the empty set."""
    if not isinstance(text, str):
        return set()
    return set(_VARIABLE_REF_RE.findall(text))


def substitute_variables(text: str, bindings: Dict[str, Any]) -> str:
    """Substitute every ``{name}`` reference in ``text`` with ``bindings[name]``
    (stringified). A reference with no binding is left VERBATIM (validation has
    already refused an unbound reference before any expansion runs). Uses a regex
    replace, never ``str.format``, so stray braces elsewhere never raise or get
    mangled -- the substitution is scoped to declared identifiers only."""
    def _repl(m: "re.Match") -> str:
        name = m.group(1)
        return str(bindings[name]) if name in bindings else m.group(0)

    return _VARIABLE_REF_RE.sub(_repl, text)


def variable_combinations(variables: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The deterministic cross product of a ``variables`` block: one binding dict
    per combination of values, iterating variable names in SORTED order and each
    name's values in declared order. An empty/absent block yields a single empty
    binding ``[{}]`` -- so a scenario without variables expands to exactly one
    (unchanged) cell along this axis."""
    if not variables:
        return [{}]
    names = sorted(variables)
    value_lists = [list(variables[n]) for n in names]
    return [
        {n: combo[i] for i, n in enumerate(names)}
        for combo in itertools.product(*value_lists)
    ]


def node_say_lines(node: Dict[str, Any]) -> List[str]:
    """The ordered caller lines a branch node speaks. A node's ``say`` is either
    a single string (one line) or a list of strings (several)."""
    say = node.get("say")
    if isinstance(say, str):
        return [say]
    return list(say or [])


def enumerate_branch_paths(branches: Dict[str, Any]) -> List[List[str]]:
    """Enumerate EVERY root-to-leaf path through a ``branches`` graph, as a list
    of node-name lists in deterministic depth-first, declared-``next`` order. A
    leaf is a node with no ``next``.

    REFUSES (``ValueError``, the caller's exit-2 path) an edge to an UNKNOWN node
    (a ``next`` naming a node not defined under ``branches.nodes``) and any CYCLE
    (a ``next`` pointing back to an ancestor on the current path) -- root-to-leaf
    paths must be finite. A shared child reached by two distinct parents (a
    diamond) is NOT a cycle: it yields one path per route, which is the point."""
    root = branches["root"]
    nodes = branches["nodes"]
    paths: List[List[str]] = []

    def _walk(name: str, trail: List[str], ancestors: "frozenset[str]") -> None:
        if name not in nodes:
            raise ValueError(
                f"branches references unknown node {name!r}; define it under "
                "branches.nodes or fix the 'next'/'root' that names it"
            )
        if name in ancestors:
            loop = " -> ".join(trail + [name])
            raise ValueError(
                f"branches contains a cycle ({loop}); every root-to-leaf path "
                "must be finite, so a node may not appear twice on one path"
            )
        trail = trail + [name]
        nxt = nodes[name].get("next") or []
        if not nxt:
            paths.append(trail)
            return
        for child in nxt:
            _walk(child, trail, ancestors | {name})

    _walk(root, [], frozenset())
    return paths


def _reject_overall_score(obj: Any, where: str) -> None:
    """Reject an ``overall_score`` key wherever the honesty invariant forbids
    one. The schema forbids it structurally too; this is the same guard on the
    code path, so a hand-built dict can never slip a score past
    :func:`validate_scenario_doc`. A scenario scores NOTHING -- it is an input.
    The reject MECHANISM is shared (:func:`hotato.errors.reject_overall_score`);
    this scenario-specific wording stays local (load-bearing per the invariant)."""
    _reject_overall_score_impl(
        obj,
        f"{where}: 'overall_score' is forbidden -- a scenario is a "
        "deterministic input, it never scores anything",
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


def _validate_agent_mock(am: Any) -> None:
    """Validate the OPTIONAL ``agent_mock`` block: the deterministic MOCK agent
    a scenario may declare so an OFFLINE simulation can exercise the outcome/
    policy authorities end-to-end (Phase-2 "tool mocks + deterministic state
    sandbox", 1.3 item 9). It is ADDITIVE and gated -- a scenario without it
    renders byte-identically. A mock is never a real agent: the produced
    conversation stays ``origin=simulated`` and the mock tool/state evidence is
    labelled as the simulator's, never a live agent's.

    * ``tools`` -- an ordered list of ``{name, arguments?, result?|error?,
      latency_ms?}`` the mock agent "invokes"; :mod:`hotato.simulate` renders
      each as a ``tool_call`` span (Authority 1) the ``tool_result``/
      ``tool_error`` assertions read.
    * ``handoff`` -- ``{to}``; rendered as a ``handoff`` span.
    * ``termination`` -- ``{reason?, by?}``; rendered as a ``termination`` span.
    * ``state`` -- a ``{resource: rows}`` post-call sandbox (Authority 2) a
      :class:`hotato.state_adapter.MockStateAdapter` serves to ``state`` /
      ``state_change`` assertions.

    This block declares AGENT-side evidence, never caller turns -- the caller
    script stays the only place caller words live (the "did not solve the task
    for the agent" invariant is unaffected: there is no real agent here)."""
    if am is None:
        return
    if not isinstance(am, dict):
        raise ValueError("'agent_mock' must be a mapping")
    _reject_overall_score(am, "agent_mock")
    tools = am.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise ValueError("agent_mock.tools must be a list")
        for j, t in enumerate(tools):
            if not isinstance(t, dict):
                raise ValueError(f"agent_mock.tools[{j}] must be a mapping")
            name = t.get("name")
            if not name or not isinstance(name, str):
                raise ValueError(f"agent_mock.tools[{j}] is missing a string 'name'")
            if "arguments" in t and not isinstance(t["arguments"], dict):
                raise ValueError(f"agent_mock.tools[{j}].arguments must be a mapping")
            if "result" in t and not isinstance(t["result"], dict):
                raise ValueError(f"agent_mock.tools[{j}].result must be a mapping")
            lat = t.get("latency_ms")
            if lat is not None and (isinstance(lat, bool)
                                    or not isinstance(lat, (int, float))
                                    or not math.isfinite(lat) or lat < 0):
                raise ValueError(
                    f"agent_mock.tools[{j}].latency_ms must be a number >= 0")
            at = t.get("at_ms")
            if at is not None and (
                isinstance(at, bool)
                or not isinstance(at, (int, float))
                or not math.isfinite(at)
                or at < 0
            ):
                raise ValueError(
                    f"agent_mock.tools[{j}].at_ms must be a finite number >= 0"
                )
    handoff = am.get("handoff")
    if handoff is not None:
        if not isinstance(handoff, dict) or not handoff.get("to"):
            raise ValueError("agent_mock.handoff must be a mapping with a 'to'")
    term = am.get("termination")
    if term is not None and not isinstance(term, dict):
        raise ValueError("agent_mock.termination must be a mapping")
    state = am.get("state")
    if state is not None and not isinstance(state, dict):
        raise ValueError(
            "agent_mock.state must be a {resource: rows} post-call sandbox")


def _validate_variables(variables: Any) -> None:
    """Validate the OPTIONAL ``variables`` block: a mapping of name -> a non-empty
    list of scalar values (strings or numbers). Each declared variable is a matrix
    axis (:func:`variable_combinations`); its values are substituted into ``{name}``
    template references in the caller's ``say`` lines. It is ADDITIVE and gated --
    a scenario without it expands byte-identically. The unbound-reference check
    (a ``{name}`` with no declared variable) lives in :func:`validate_scenario_doc`
    where both the caller script and any branch nodes are in view."""
    if variables is None:
        return
    if not isinstance(variables, dict) or not variables:
        raise ValueError(
            "'variables' must be a non-empty mapping of name -> list of values"
        )
    for name, values in variables.items():
        if not isinstance(name, str) or not _VARIABLE_NAME_RE.match(name):
            raise ValueError(
                f"variables key {name!r} must be an identifier "
                "([A-Za-z_][A-Za-z0-9_]*) so it can be a '{name}' template"
            )
        if not isinstance(values, list) or not values:
            raise ValueError(
                f"variables[{name!r}] must be a non-empty list of values"
            )
        for v in values:
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise ValueError(
                    f"variables[{name!r}] values must be strings or numbers, "
                    f"got {v!r}"
                )


def _validate_node_say(name: str, say: Any) -> None:
    if isinstance(say, str):
        if not say:
            raise ValueError(
                f"branches.nodes[{name!r}].say must be a non-empty caller line"
            )
        return
    if isinstance(say, list) and say and all(
        isinstance(x, str) and x for x in say
    ):
        return
    raise ValueError(
        f"branches.nodes[{name!r}].say must be a non-empty caller line string "
        "or a non-empty list of such strings"
    )


def _validate_branches(branches: Any) -> None:
    """Validate the OPTIONAL ``branches`` block: a decision tree/DAG of named
    nodes the caller walks. ``root`` names the entry node; ``nodes`` maps a name
    to ``{say, next?}`` where ``say`` is the node's caller line(s) and ``next`` is
    the list of successor node names (a leaf has none). Every root-to-leaf path
    is a deterministic expansion cell (:func:`enumerate_branch_paths`); its lines
    are appended to the base caller script for that run. It is ADDITIVE and gated
    -- a scenario without it expands byte-identically.

    REFUSES (``ValueError``, exit 2): a missing/blank ``root``; an empty/malformed
    ``nodes`` map; a bad node ``say``/``next``; a ``root`` that is not a defined
    node; an edge to an UNKNOWN node; and any CYCLE (all three graph faults are
    surfaced by walking the graph here, up front, before any expansion)."""
    if branches is None:
        return
    if not isinstance(branches, dict):
        raise ValueError(
            "'branches' must be a mapping with a 'root' node name and a 'nodes' map"
        )
    _reject_overall_score(branches, "branches")
    root = branches.get("root")
    if not root or not isinstance(root, str):
        raise ValueError(
            "branches.root is required and must name the entry node (a string)"
        )
    nodes = branches.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError(
            "branches.nodes is required and must be a non-empty mapping of node "
            "name -> {say, next?}"
        )
    for nname, node in nodes.items():
        if not isinstance(node, dict):
            raise ValueError(f"branches.nodes[{nname!r}] must be a mapping")
        _reject_overall_score(node, f"branches.nodes[{nname!r}]")
        _validate_node_say(nname, node.get("say"))
        nxt = node.get("next")
        if nxt is not None and (
            not isinstance(nxt, list)
            or not all(isinstance(x, str) and x for x in nxt)
        ):
            raise ValueError(
                f"branches.nodes[{nname!r}].next must be a list of successor "
                "node-name strings"
            )
    if root not in nodes:
        raise ValueError(
            f"branches.root {root!r} is not a defined node; add it under "
            "branches.nodes"
        )
    # Walk the whole graph now so an unknown-node edge or a cycle is refused up
    # front (exit 2), never at render time.
    enumerate_branch_paths(branches)


def _collect_variable_refs(doc: Dict[str, Any]) -> "set[str]":
    """Every ``{name}`` template reference across the base caller script AND any
    branch-node lines -- the full set a scenario must declare in ``variables``."""
    refs: "set[str]" = set()
    for turn in (doc.get("caller") or {}).get("script") or []:
        if isinstance(turn, dict):
            refs |= variable_references(turn.get("say", ""))
    branches = doc.get("branches")
    if isinstance(branches, dict):
        for node in (branches.get("nodes") or {}).values():
            if isinstance(node, dict):
                for line in node_say_lines(node):
                    refs |= variable_references(line)
    return refs


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

    # An actionable kind mismatch BEFORE the shared const check: the two "scenario"
    # concepts collide by name, and the bare ``'kind' must be 'hotato.scenario'``
    # leaves a new user stranded. Name what they DO have and the exact command
    # that produces a scenario ``simulate`` accepts.
    kind = doc.get("kind")
    if kind != KIND:
        if kind == "hotato.conversation-test":
            what = (
                " -- that is a conversation-test file (what `hotato scenario "
                "init` writes; `hotato test run` consumes it), NOT a simulate "
                "scenario"
            )
        else:
            what = ""
        raise ValueError(
            f"'kind' must be {KIND!r} (a hotato.scenario.v1 doc), got "
            f"{kind!r}{what}. Get a valid scenario with `hotato simulate --init "
            "demo.scenario.json` (then `hotato simulate demo.scenario.json`), "
            "or run the bundled one with `hotato simulate --example`. See "
            "docs/SIMULATE.md."
        )

    _check_kind_version(doc, kind=KIND, version=VERSION, subject="scenario")

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
    _validate_agent_mock(doc.get("agent_mock"))
    _validate_variables(doc.get("variables"))
    _validate_branches(doc.get("branches"))

    # Every ``{name}`` template referenced by a caller line (script or branch
    # node) must be declared in ``variables``; an unbound reference is refused up
    # front (exit 2), never left to substitute to a literal at render time.
    declared_vars = set(doc.get("variables") or {})
    unbound = _collect_variable_refs(doc) - declared_vars
    if unbound:
        raise ValueError(
            "caller script references undeclared variable(s) "
            f"{sorted(unbound)}; declare each under 'variables' as name -> list "
            "of values (an unbound '{name}' template)"
        )

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


def build_starter(scenario_id: str = "demo") -> str:
    """Return a MINIMAL valid ``hotato.scenario.v1`` document as JSON text that
    ``hotato simulate`` accepts as-is -- the onboarding scenario ``hotato
    simulate --init`` writes and the bundled ``--example`` ships.

    It round-trips through :func:`parse_scenario` + :func:`validate_scenario_doc`
    and renders to a faithful (non ``SIMULATOR_INVALID``) ``origin=simulated``
    conversation. Deliberately tiny: a two-turn scripted caller, backchannels
    off (so it is byte-identical at every seed), no variation matrix, no
    ``agent_mock``. It is a starter you EDIT for your own agent, never a claim
    these are the right caller turns for your call."""
    doc = {
        "kind": KIND,
        "version": VERSION,
        "id": scenario_id or "demo",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "facts": {"order_id": "A-1001"},
        "caller": {
            "script": [
                {"say": "Hi, my order A-1001 arrived damaged and I would like "
                        "a refund."},
                {"say": "Yes, please refund it to my card."},
            ],
            "behavior": {"backchannels": {"probability": 0.0}},
        },
        "environment": {"locale": "en-US", "route": "phone"},
        "seed": 0,
    }
    # sort_keys keeps the bytes stable so the packaged --example file and this
    # builder can be pinned equal by a test.
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def example_scenario_path() -> str:
    """Absolute path to the minimal scenario bundled INSIDE the package (shipped
    in the wheel under ``hotato/data/simulate/``), so ``hotato simulate
    --example`` runs from a bare ``pip install`` with no example file on disk."""
    from importlib import resources  # deferred: import cost at interpreter start

    return str(
        resources.files("hotato").joinpath("data", "simulate", EXAMPLE_FILENAME)
    )
