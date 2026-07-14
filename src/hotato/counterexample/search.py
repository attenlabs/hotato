"""Deterministic three-way hierarchical delta debugging."""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Sequence, Tuple, Union

from .model import PRESERVED, UNRESOLVED, digest_obj, prefixed_digest

PathPart = Union[str, int]
Deletion = Dict[str, Any]


def _path_sort_key(path: Sequence[PathPart]) -> Tuple[Any, ...]:
    """Apply child deletions first and list indices from highest to lowest."""
    encoded: List[Tuple[int, Any]] = []
    for part in path[:-1]:
        encoded.append((0, part) if isinstance(part, str) else (1, -part))
    last = path[-1]
    encoded.append((0, last) if isinstance(last, str) else (1, -last))
    return (-len(path), tuple(encoded))


def _path_text(path: Sequence[PathPart]) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (("." if out else "") + part)
    return out


def deletion_transform(parent: Any, child: Any) -> Dict[str, Any]:
    """Derive a replayable, deletion-only transform from ``parent`` to ``child``.

    Reducers v1 are intentionally closed: an accepted candidate may remove keys
    or list members, but it may never replace a scalar, reorder a list, or add
    content.  Recording this transform lets an independent verifier reconstruct
    every accepted candidate from the frozen source instead of trusting hashes.
    """
    removals: List[Deletion] = []

    def walk(before: Any, after: Any, path: Tuple[PathPart, ...]) -> None:
        if isinstance(before, dict) and isinstance(after, dict):
            added = set(after).difference(before)
            if added:
                raise ValueError(f"deletion transform added keys at {path!r}")
            for key in sorted(set(before).intersection(after)):
                walk(before[key], after[key], path + (key,))
            for key in sorted(set(before).difference(after)):
                removals.append({
                    "op": "remove",
                    "path": list(path + (key,)),
                    "removed_digest": prefixed_digest(before[key]),
                })
            return
        if isinstance(before, list) and isinstance(after, list):
            if len(before) == len(after):
                for index, (before_item, after_item) in enumerate(zip(before, after)):
                    walk(before_item, after_item, path + (index,))
                return
            if len(after) > len(before):
                raise ValueError(f"deletion transform grew a list at {path!r}")
            matched: List[int] = []
            cursor = 0
            for item in after:
                while cursor < len(before) and before[cursor] != item:
                    cursor += 1
                if cursor >= len(before):
                    raise ValueError(f"child is not a parent subsequence at {path!r}")
                matched.append(cursor)
                cursor += 1
            keep = set(matched)
            for index in range(len(before) - 1, -1, -1):
                if index not in keep:
                    removals.append({
                        "op": "remove",
                        "path": list(path + (index,)),
                        "removed_digest": prefixed_digest(before[index]),
                    })
            return
        if before != after or type(before) is not type(after):
            raise ValueError(f"reducers v1 attempted a non-deletion change at {path!r}")

    walk(parent, child, ())
    removals.sort(key=lambda row: _path_sort_key(row["path"]))
    if not removals and parent != child:
        raise ValueError("candidate changed without a deletion")
    return {"kind": "hotato.delete-only.v1", "operations": removals}


def apply_deletion_transform(parent: Any, transform: Dict[str, Any]) -> Any:
    """Replay and validate one closed reducer transform."""
    if not isinstance(transform, dict) or set(transform) != {"kind", "operations"}:
        raise ValueError("malformed deletion transform")
    if transform.get("kind") != "hotato.delete-only.v1":
        raise ValueError("unknown deletion transform kind")
    operations = transform.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("deletion transform must contain at least one operation")
    value = copy.deepcopy(parent)
    seen = set()
    for row in operations:
        if not isinstance(row, dict) or set(row) != {"op", "path", "removed_digest"}:
            raise ValueError("malformed deletion operation")
        path = row.get("path")
        if row.get("op") != "remove" or not isinstance(path, list) or not path:
            raise ValueError("malformed deletion operation path")
        path_key = tuple(path)
        if path_key in seen:
            raise ValueError("duplicate deletion operation path")
        seen.add(path_key)
        current = value
        for part in path[:-1]:
            if isinstance(current, dict) and isinstance(part, str) and part in current:
                current = current[part]
            elif isinstance(current, list) and isinstance(part, int) and not isinstance(part, bool) and 0 <= part < len(current):
                current = current[part]
            else:
                raise ValueError("deletion operation path does not exist")
        leaf = path[-1]
        if isinstance(current, dict) and isinstance(leaf, str) and leaf in current:
            removed = current[leaf]
            if prefixed_digest(removed) != row.get("removed_digest"):
                raise ValueError("deleted value digest mismatch")
            del current[leaf]
        elif isinstance(current, list) and isinstance(leaf, int) and not isinstance(leaf, bool) and 0 <= leaf < len(current):
            removed = current[leaf]
            if prefixed_digest(removed) != row.get("removed_digest"):
                raise ValueError("deleted value digest mismatch")
            current.pop(leaf)
        else:
            raise ValueError("deletion operation path does not exist")
    return value


class SearchState:
    """Budget, cache, journal, and accepted-chain state for one compilation."""

    def __init__(self, budget: int, evaluator: Callable[[Dict[str, Any]], Dict[str, Any]]):
        self.budget = budget
        self.evaluator = evaluator
        self.evaluations = 0
        self.cache_hits = 0
        self.accepted = 0
        self.exhausted = False
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.journal: List[Dict[str, Any]] = []
        self.accepted_steps: List[Dict[str, Any]] = []

    def evaluate(self, scenario: Dict[str, Any], operation: Dict[str, Any]) -> Dict[str, Any]:
        candidate_digest = digest_obj(scenario)
        cached = self.cache.get(candidate_digest)
        if cached is not None:
            self.cache_hits += 1
            result = dict(cached)
            result["cached"] = True
        elif self.evaluations >= self.budget:
            self.exhausted = True
            result = {
                "status": UNRESOLVED,
                "code": "budget_exhausted",
                "candidate_digest": candidate_digest,
                "cached": False,
            }
        else:
            self.evaluations += 1
            result = dict(self.evaluator(scenario))
            result["candidate_digest"] = candidate_digest
            result["cached"] = False
            self.cache[candidate_digest] = dict(result)
        row = {
            "attempt": len(self.journal) + 1,
            "operation": operation,
            "candidate_digest": candidate_digest,
            "status": result.get("status", UNRESOLVED),
            "code": result.get("code"),
            "cached": bool(result.get("cached")),
        }
        if result.get("failure_atom_digest"):
            row["failure_atom_digest"] = result["failure_atom_digest"]
        self.journal.append(row)
        return result

    def try_accept(
        self,
        current: Dict[str, Any],
        candidate: Dict[str, Any],
        operation: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool]:
        parent_digest = digest_obj(current)
        transform = deletion_transform(current, candidate)
        if not transform["operations"]:
            raise RuntimeError("candidate did not change the scenario")
        # ddmin is used over both lists and sorted dictionary keys. Bind its
        # descriptive operation to the exact replayed paths rather than to
        # internal search indices, whose meaning cannot be reconstructed from
        # a later certificate alone.
        if operation.get("kind") == "remove-path-set":
            operation = dict(operation)
            operation["paths"] = sorted(
                _path_text(row["path"]) for row in transform["operations"]
            )
        result = self.evaluate(candidate, operation)
        if result.get("status") != PRESERVED:
            return current, False
        child_digest = digest_obj(candidate)
        self.accepted += 1
        step = {
            "step": self.accepted,
            "parent_digest": parent_digest,
            "child_digest": child_digest,
            "operation": operation,
            "transform": transform,
            "oracle_result_digest": result.get("result_digest"),
            "failure_atom_digest": result.get("failure_atom_digest"),
        }
        self.accepted_steps.append(step)
        return candidate, True


def _partitions(items: Sequence[int], n: int) -> List[List[int]]:
    """Stable, near-even contiguous partitions."""
    length = len(items)
    n = max(1, min(n, length))
    out: List[List[int]] = []
    start = 0
    for i in range(n):
        end = start + (length - start + (n - i) - 1) // (n - i)
        out.append(list(items[start:end]))
        start = end
    return [part for part in out if part]


def ddmin_indices(
    current: Dict[str, Any],
    *,
    size: int,
    build: Callable[[Dict[str, Any], List[int]], Dict[str, Any]],
    state: SearchState,
    phase: str,
    min_items: int = 0,
) -> Dict[str, Any]:
    """Remove groups with classic `ddmin`, preserving original index order."""
    kept = list(range(size))
    granularity = 2
    while len(kept) > min_items and not state.exhausted:
        changed = False
        for chunk in _partitions(kept, granularity):
            remove = set(chunk)
            remaining = [idx for idx in kept if idx not in remove]
            if len(remaining) < min_items:
                continue
            candidate = build(current, remaining)
            operation = {
                "kind": "remove-path-set",
                "phase": phase,
                # Normalized to the transform's exact paths by try_accept.
                "paths": ["pending"],
            }
            new_current, accepted = state.try_accept(current, candidate, operation)
            if accepted:
                current = new_current
                kept = remaining
                granularity = max(2, granularity - 1)
                changed = True
                break
        if changed:
            continue
        if granularity >= len(kept):
            break
        granularity = min(len(kept), granularity * 2)
    return current


def copy_candidate(value: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(value)
