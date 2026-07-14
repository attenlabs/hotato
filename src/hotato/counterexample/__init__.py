"""Proof-preserving counterexample compilation for deterministic Hotato tests.

The public API is intentionally small.  Every surface calls the same offline
compiler and verifier; no adapter, model, network client, or subprocess is
loaded on this path.
"""

from .bundle import (
    compile_counterexample,
    export_counterexample,
    inspect_counterexample,
    predicate_counterexample,
    reproduce_counterexample,
    verify_counterexample,
)
from .model import CounterexampleRefusal

__all__ = [
    "CounterexampleRefusal",
    "compile_counterexample",
    "verify_counterexample",
    "reproduce_counterexample",
    "inspect_counterexample",
    "export_counterexample",
    "predicate_counterexample",
]
