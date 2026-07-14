"""Hotato Fleet: a private, self-hosted control plane over the evidence kernel.

Local mode is zero-dependency (SQLite + a content-addressed artifact directory,
both stdlib). It registers voice agents with NO product-level cap, discovers
candidate failures, holds a human review queue, and runs manifest-bound clone
experiments -- always recommending, never auto-deploying, in this release.

Distributed self-hosted mode (Postgres + object store + worker pools) shares the
same domain API and is added behind an optional extra; nothing here requires a
hosted account.
"""
from .registry import DEFAULT_HOME, Registry
from .store import ArtifactStore

__all__ = ["ArtifactStore", "Registry", "DEFAULT_HOME"]
