"""Closed `hotato.reducers.v1` transform algebra for scripted scenarios."""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple, Union

from .model import MAX_MINIMALITY_UNITS, PRESERVED, CounterexampleRefusal
from .search import SearchState, ddmin_indices

PathPart = Union[str, int]
Path = Tuple[PathPart, ...]


def _get(root: Any, path: Sequence[PathPart]) -> Any:
    current = root
    for part in path:
        current = current[part]
    return current


def _set(root: Any, path: Sequence[PathPart], value: Any) -> None:
    if not path:
        raise ValueError("cannot replace the scenario root")
    parent = _get(root, path[:-1])
    parent[path[-1]] = value


def _delete(root: Any, path: Sequence[PathPart]) -> None:
    parent = _get(root, path[:-1])
    part = path[-1]
    if isinstance(parent, list):
        parent.pop(int(part))
    else:
        del parent[part]


def _has(root: Any, path: Sequence[PathPart]) -> bool:
    try:
        _get(root, path)
        return True
    except (KeyError, IndexError, TypeError):
        return False


def _path_text(path: Sequence[PathPart]) -> str:
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + part
    return out


def _reduce_list(
    current: Dict[str, Any], path: Path, state: SearchState, phase: str,
    *, min_items: int = 0,
) -> Dict[str, Any]:
    if not _has(current, path):
        return current
    original = copy.deepcopy(_get(current, path))
    if not isinstance(original, list) or len(original) <= min_items:
        return current

    def build(base: Dict[str, Any], remaining: List[int]) -> Dict[str, Any]:
        candidate = copy.deepcopy(base)
        _set(candidate, path, [copy.deepcopy(original[index]) for index in remaining])
        return candidate

    return ddmin_indices(
        current,
        size=len(original),
        build=build,
        state=state,
        phase=phase,
        min_items=min_items,
    )


def _reduce_dict_keys(
    current: Dict[str, Any], path: Path, state: SearchState, phase: str,
    *, protected: Iterable[str] = (),
) -> Dict[str, Any]:
    if not _has(current, path):
        return current
    source = _get(current, path)
    if not isinstance(source, dict):
        return current
    protected_set = set(protected)
    keys = [key for key in sorted(source) if key not in protected_set]
    fixed = {key: copy.deepcopy(source[key]) for key in sorted(source) if key in protected_set}
    if not keys:
        return current

    def build(base: Dict[str, Any], remaining: List[int]) -> Dict[str, Any]:
        candidate = copy.deepcopy(base)
        value = dict(fixed)
        for index in remaining:
            key = keys[index]
            value[key] = copy.deepcopy(source[key])
        _set(candidate, path, value)
        return candidate

    return ddmin_indices(
        current, size=len(keys), build=build, state=state, phase=phase,
    )


def _try_delete_key(
    current: Dict[str, Any], path: Path, state: SearchState, phase: str
) -> Dict[str, Any]:
    if not _has(current, path):
        return current
    candidate = copy.deepcopy(current)
    _delete(candidate, path)
    operation = {"kind": "remove-field", "phase": phase, "path": _path_text(path)}
    return state.try_accept(current, candidate, operation)[0]


def _state_hierarchy(
    current: Dict[str, Any], state: SearchState, frozen: Set[str]
) -> Dict[str, Any]:
    if "state" in frozen or not _has(current, ("agent_mock", "state")):
        return current
    current = _reduce_dict_keys(
        current, ("agent_mock", "state"), state, "state-resources"
    )
    if not _has(current, ("agent_mock", "state")):
        return current
    resources = _get(current, ("agent_mock", "state"))
    for resource in sorted(list(resources)):
        path = ("agent_mock", "state", resource)
        value = _get(current, path)
        if isinstance(value, list):
            current = _reduce_list(current, path, state, f"state-rows:{resource}")
        elif isinstance(value, dict) and ("before" in value or "after" in value):
            current = _reduce_dict_keys(
                current, path, state, f"state-snapshots:{resource}"
            )
            if not _has(current, path):
                continue
            snapshots = _get(current, path)
            for snap in sorted(list(snapshots)):
                snap_path = path + (snap,)
                rows = _get(current, snap_path)
                if isinstance(rows, list):
                    current = _reduce_list(
                        current, snap_path, state, f"state-rows:{resource}:{snap}"
                    )
    return current


def _tool_payload_hierarchy(
    current: Dict[str, Any], state: SearchState
) -> Dict[str, Any]:
    """Reduce free-form tool arguments/results without rewriting values."""
    if not _has(current, ("agent_mock", "tools")):
        return current
    tools = _get(current, ("agent_mock", "tools"))
    if not isinstance(tools, list):
        return current
    for index in range(len(tools)):
        for field in ("arguments", "result"):
            path = ("agent_mock", "tools", index, field)
            if not _has(current, path):
                continue
            value = _get(current, path)
            phase = f"tool-payload:{index}:{field}"
            if isinstance(value, dict):
                current = _reduce_dict_keys(current, path, state, phase)
            elif isinstance(value, list):
                current = _reduce_list(current, path, state, phase)
    return current


def hierarchical_reduce(
    scenario: Dict[str, Any], state: SearchState, frozen: Set[str]
) -> Dict[str, Any]:
    """Fixed phase order; no filesystem, clock, randomness, or worker ordering."""
    current = copy.deepcopy(scenario)

    current = _try_delete_key(current, ("variation_matrix",), state, "variation-matrix")
    current = _reduce_dict_keys(current, ("facts",), state, "facts")
    current = _try_delete_key(current, ("facts",), state, "empty-facts") if not current.get("facts") else current
    current = _reduce_dict_keys(current, ("environment",), state, "environment")
    current = _try_delete_key(current, ("environment",), state, "empty-environment") if not current.get("environment") else current

    if _has(current, ("caller", "behavior", "interruptions")):
        current = _reduce_list(
            current, ("caller", "behavior", "interruptions"), state,
            "interruptions",
        )
    if _has(current, ("caller", "behavior")):
        current = _reduce_dict_keys(
            current, ("caller", "behavior"), state, "caller-behavior",
            protected=("interruptions",),
        )
        behavior = _get(current, ("caller", "behavior"))
        if isinstance(behavior, dict) and not behavior:
            current = _try_delete_key(current, ("caller", "behavior"), state, "empty-behavior")

    if "script" not in frozen:
        current = _reduce_list(
            current, ("caller", "script"), state, "caller-turns", min_items=1,
        )

    if _has(current, ("agent_mock", "tools")) and "tools" not in frozen:
        current = _reduce_list(
            current, ("agent_mock", "tools"), state, "tool-spans"
        )
        if not _get(current, ("agent_mock", "tools")):
            current = _try_delete_key(current, ("agent_mock", "tools"), state, "empty-tools")
        else:
            current = _tool_payload_hierarchy(current, state)

    if "handoff" not in frozen:
        current = _try_delete_key(current, ("agent_mock", "handoff"), state, "handoff")
    if "termination" not in frozen:
        current = _try_delete_key(current, ("agent_mock", "termination"), state, "termination")
    current = _state_hierarchy(current, state, frozen)

    if _has(current, ("agent_mock",)) and not _get(current, ("agent_mock",)):
        current = _try_delete_key(current, ("agent_mock",), state, "empty-agent-mock")
    return current


def _nested_leaf_units(value: Any, path: Path) -> List[Path]:
    units: List[Path] = []
    if isinstance(value, dict):
        for key in sorted(value):
            child_path = path + (key,)
            units.append(child_path)
            child = value[key]
            if isinstance(child, (dict, list)):
                units.extend(_nested_leaf_units(child, child_path))
    elif isinstance(value, list):
        for index in range(len(value)):
            child_path = path + (index,)
            units.append(child_path)
            child = value[index]
            if isinstance(child, (dict, list)):
                units.extend(_nested_leaf_units(child, child_path))
    return units


def enumerate_units(scenario: Dict[str, Any], frozen: Set[str]) -> List[Dict[str, Any]]:
    """Every remaining removable unit in the closed algebra, stable and explicit."""
    units: List[Dict[str, Any]] = []

    def add(path: Path, component: str, *, min_items: int = 0) -> None:
        units.append({"path": path, "component": component, "min_items": min_items})

    def add_list_items_or_empty_field(
        path: Path,
        value: List[Any],
        *,
        item_component: str,
        empty_component: str,
    ) -> None:
        """Name list members, or the optional field once no members remain.

        A list-backed reducer owns its members while the list is non-empty.
        After the final member is removed, the optional empty container is a
        field unit in the surrounding reducer group.  Keeping that transition
        explicit prevents an empty declaration from falling out of the closed
        algebra while avoiding a whole-list unit that would silently remove
        multiple declared members.
        """
        if value:
            for index in range(len(value) - 1, -1, -1):
                add(path + (index,), item_component)
        else:
            add(path, empty_component)

    # The scenario validator is intentionally additive.  Reducers v1 therefore
    # names every optional field that may survive the coarse phases; otherwise
    # an unknown/additive metadata field could remain while the capsule claimed
    # a completed deletion pass.  Required structural fields stay protected.
    for key in sorted(set(scenario).difference({
        "kind", "version", "id", "goal", "caller", "variation_matrix",
        "facts", "environment", "agent_mock",
    })):
        add((key,), "top-level-optional")
    goal = scenario.get("goal")
    if isinstance(goal, dict):
        for key in sorted(set(goal).difference({"type", "target"})):
            add(("goal", key), "goal-optional")
    caller = scenario.get("caller")
    if isinstance(caller, dict):
        for key in sorted(set(caller).difference({"script", "behavior"})):
            add(("caller", key), "caller-optional")

    for name in ("variation_matrix", "facts", "environment"):
        if name in scenario:
            value = scenario[name]
            if isinstance(value, dict):
                for key in sorted(value):
                    add((name, key), name)
            add((name,), name)

    behavior = (scenario.get("caller") or {}).get("behavior")
    if isinstance(behavior, dict):
        for key in sorted(behavior):
            if key == "interruptions" and isinstance(behavior[key], list):
                add_list_items_or_empty_field(
                    ("caller", "behavior", key),
                    behavior[key],
                    item_component="interruptions",
                    empty_component="behavior",
                )
            else:
                add(("caller", "behavior", key), "behavior")
        add(("caller", "behavior"), "behavior")

    script = (scenario.get("caller") or {}).get("script") or []
    if "script" not in frozen and len(script) > 1:
        for index in range(len(script) - 1, -1, -1):
            add(("caller", "script", index), "script", min_items=1)
    if "script" not in frozen:
        for index, turn in enumerate(script):
            if isinstance(turn, dict):
                for key in sorted(set(turn).difference({"say"})):
                    add(("caller", "script", index, key), "script-field")

    agent_mock = scenario.get("agent_mock")
    if isinstance(agent_mock, dict):
        tools = agent_mock.get("tools") or []
        if "tools" not in frozen:
            for index in range(len(tools) - 1, -1, -1):
                add(("agent_mock", "tools", index), "tools")
            for index, tool in enumerate(tools):
                if isinstance(tool, dict):
                    for key in sorted(set(tool).difference({"name"})):
                        path = ("agent_mock", "tools", index, key)
                        add(path, "tool-field")
                        value = tool[key]
                        if key in {"arguments", "result"} and isinstance(
                            value, (dict, list)
                        ):
                            units.extend(
                                {
                                    "path": nested_path,
                                    "component": "tool-field",
                                    "min_items": 0,
                                }
                                for nested_path in _nested_leaf_units(value, path)
                            )
            if "tools" in agent_mock:
                add(("agent_mock", "tools"), "tools")
        if "handoff" not in frozen and "handoff" in agent_mock:
            handoff = agent_mock.get("handoff")
            if isinstance(handoff, dict):
                for key in sorted(set(handoff).difference({"to"})):
                    add(("agent_mock", "handoff", key), "handoff-field")
            add(("agent_mock", "handoff"), "handoff")
        if "termination" not in frozen and "termination" in agent_mock:
            termination = agent_mock.get("termination")
            if isinstance(termination, dict):
                for key in sorted(termination):
                    add(("agent_mock", "termination", key), "termination-field")
            add(("agent_mock", "termination"), "termination")
        if "state" not in frozen and "state" in agent_mock:
            units.extend({"path": path, "component": "state", "min_items": 0}
                         for path in _nested_leaf_units(agent_mock["state"], ("agent_mock", "state")))
            add(("agent_mock", "state"), "state")
        for key in sorted(set(agent_mock).difference({
            "tools", "handoff", "termination", "state",
        })):
            add(("agent_mock", key), "agent-mock-optional")
        add(("agent_mock",), "agent_mock")

    # Parents before children can make later paths disappear.  Final checking
    # restarts after every accepted deletion, so every attempted path is valid.
    # De-duplicate paths named by both a semantic phase and the additive-field
    # sweep.  The first declaration carries the more specific component label.
    unique: List[Dict[str, Any]] = []
    seen = set()
    for unit in sorted(units, key=lambda row: (len(row["path"]), _path_text(row["path"]))):
        if unit["path"] in seen:
            continue
        seen.add(unit["path"])
        unique.append(unit)
    return unique


def _bounded_units(
    scenario: Dict[str, Any], frozen: Set[str]
) -> List[Dict[str, Any]]:
    """Enumerate the proof work, refusing before any candidate deep copy."""
    units = enumerate_units(scenario, frozen)
    if len(units) > MAX_MINIMALITY_UNITS:
        raise CounterexampleRefusal(
            "minimality_work_limit",
            f"counterexample has {len(units)} remaining deletion units; "
            f"the proof limit is {MAX_MINIMALITY_UNITS}",
        )
    return units


def _can_delete(scenario: Dict[str, Any], unit: Dict[str, Any]) -> bool:
    path = unit["path"]
    if not _has(scenario, path):
        return False
    if unit["component"] == "script":
        return len(_get(scenario, path[:-1])) > int(unit.get("min_items", 1))
    return True


def final_single_unit_pass(
    scenario: Dict[str, Any], state: SearchState, frozen: Set[str]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
    """Greedy restart to a fixed point, then record every rejected remaining unit."""
    current = copy.deepcopy(scenario)
    while not state.exhausted:
        accepted_one = False
        for unit in _bounded_units(current, frozen):
            if not _can_delete(current, unit):
                continue
            candidate = copy.deepcopy(current)
            _delete(candidate, unit["path"])
            operation = {
                "kind": "remove-single-unit",
                "phase": "one-minimality",
                "path": _path_text(unit["path"]),
                "component": unit["component"],
            }
            new_current, accepted = state.try_accept(current, candidate, operation)
            if accepted:
                current = new_current
                accepted_one = True
                break
        if not accepted_one:
            break

    checks: List[Dict[str, Any]] = []
    if state.exhausted:
        return current, checks, False
    for unit in _bounded_units(current, frozen):
        if not _can_delete(current, unit):
            continue
        candidate = copy.deepcopy(current)
        _delete(candidate, unit["path"])
        operation = {
            "kind": "verify-single-unit",
            "phase": "minimality-proof",
            "path": _path_text(unit["path"]),
            "component": unit["component"],
        }
        result = state.evaluate(candidate, operation)
        checks.append({
            "path": operation["path"],
            "component": operation["component"],
            "outcome": result.get("status"),
            "code": result.get("code"),
            "candidate_digest": result.get("candidate_digest"),
        })
        if result.get("status") == PRESERVED:
            # A preserved deletion means the fixed point changed (possible only
            # through a cache/implementation fault); accept and restart.
            current, _accepted = state.try_accept(current, candidate, {
                "kind": "remove-single-unit",
                "phase": "minimality-proof-restart",
                "path": operation["path"],
                "component": operation["component"],
            })
            return final_single_unit_pass(current, state, frozen)
        if state.exhausted:
            return current, checks, False
    return current, checks, True


def verify_single_units(
    scenario: Dict[str, Any], state: SearchState, frozen: Set[str]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Independently re-run every deletion represented by the final algebra."""
    checks: List[Dict[str, Any]] = []
    preserved: List[str] = []
    for unit in _bounded_units(scenario, frozen):
        if not _can_delete(scenario, unit):
            continue
        candidate = copy.deepcopy(scenario)
        _delete(candidate, unit["path"])
        operation = {
            "kind": "verify-single-unit",
            "phase": "independent-verification",
            "path": _path_text(unit["path"]),
            "component": unit["component"],
        }
        result = state.evaluate(candidate, operation)
        row = {
            "path": operation["path"],
            "component": operation["component"],
            "outcome": result.get("status"),
            "code": result.get("code"),
            "candidate_digest": result.get("candidate_digest"),
        }
        checks.append(row)
        if result.get("status") == PRESERVED:
            preserved.append(operation["path"])
    return checks, preserved
