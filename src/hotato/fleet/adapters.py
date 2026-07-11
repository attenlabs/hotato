"""Stack adapter capability contract for Fleet experiments.

Each adapter DECLARES the capabilities it supports rather than the orchestrator
assuming feature parity (plan §8). A capability an adapter lacks is simply
unavailable; the experiment engine checks before it acts.

Capabilities:
  inspect_config, pull_recordings, dual_channel_capture, clone_agent,
  apply_variant, run_scenario, capture_result, snapshot_config, canary_route,
  rollback, delete_clone

Live provider adapters (Vapi, Retell) implement the offline capabilities
(config normalization + hashing) and REFUSE networked capabilities until
credentials are supplied -- production mutation is never silent. A MockAdapter
implements the whole loop locally (using synthetic recapture) so the
clone -> apply -> scenario -> capture -> recompute path is exercisable and
tested without any live account.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional, Set

from .. import manifest as _manifest
from .. import synth as _synth

CAPABILITIES = (
    "inspect_config", "pull_recordings", "dual_channel_capture", "clone_agent",
    "apply_variant", "run_scenario", "capture_result", "snapshot_config",
    "canary_route", "rollback", "delete_clone",
)


class CapabilityError(RuntimeError):
    """Raised when a capability is used that the adapter does not declare, or
    that requires credentials not present."""


def _validated_source_id(source_id, stack: str) -> str:
    """Guard a platform id before it is interpolated into a provider URL, using
    the SAME rule the CLI apply path enforces (:data:`apply._ID_RE`). A source id
    carrying a '/' or a URL fragment could otherwise smuggle an extra path
    segment into the GET the clone/inspect path issues; this refuses a non-plain
    id rather than building a tampered URL."""
    from .. import apply as _apply
    sid = str(source_id)
    if not _apply._ID_RE.match(sid):
        raise ValueError(
            f"the source id {sid!r} is not a valid {stack} platform id "
            "(allowed: letters, digits, '.', '-', '_'); refusing to build a "
            "clone URL from it.")
    return sid


class Adapter:
    """Base adapter. Subclasses set ``stack`` and override ``capabilities()``."""
    stack = "generic"
    version = "1"

    def capabilities(self) -> Set[str]:
        return set()

    def supports(self, cap: str) -> bool:
        return cap in self.capabilities()

    def _require(self, cap: str):
        if not self.supports(cap):
            raise CapabilityError(
                f"{self.stack} adapter v{self.version} does not support {cap!r}")

    # --- offline capabilities (default implementations) ----------------
    def snapshot_config(self, config: dict) -> str:
        """Hash the exact effective turn-taking configuration -> a deployment
        identity. Available to every adapter (no network)."""
        self._require("snapshot_config")
        return hashlib.sha256(
            _manifest.canonical_json(config).encode("utf-8")).hexdigest()

    # --- networked capabilities (must be overridden) -------------------
    def inspect_config(self, ref):
        self._require("inspect_config"); raise NotImplementedError

    def clone_agent(self, ref, *, name):
        self._require("clone_agent"); raise NotImplementedError

    def apply_variant(self, clone_ref, variant):
        self._require("apply_variant"); raise NotImplementedError

    def run_scenario(self, clone_ref, scenario):
        self._require("run_scenario"); raise NotImplementedError

    def capture_result(self, clone_ref, scenario):
        self._require("capture_result"); raise NotImplementedError

    def rollback(self, ref, revision):
        self._require("rollback"); raise NotImplementedError

    def delete_clone(self, clone_ref):
        self._require("delete_clone"); raise NotImplementedError


class _CredentialGatedAdapter(Adapter):
    """A live provider adapter: offline config capabilities work; every
    networked capability refuses without credentials so production state is
    never mutated silently."""
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    def capabilities(self) -> Set[str]:
        # declared regardless of creds; networked ones refuse at call time.
        # rollback/delete_clone are deliberately NOT declared: live rollback and
        # delete are not wired for a hosted provider yet, so supports() reports
        # them unavailable rather than advertising a capability the class cannot
        # honor (they inherit Adapter.rollback/delete_clone, which raise). The
        # MockAdapter, which implements the whole loop, keeps them.
        return {"inspect_config", "pull_recordings", "dual_channel_capture",
                "clone_agent", "apply_variant", "run_scenario", "capture_result",
                "snapshot_config"}

    def _need_key(self, cap):
        if not self.api_key:
            raise CapabilityError(
                f"{self.stack} {cap} requires credentials (connect a stack and "
                f"supply an API key); this build never mutates production silently")

    # clone_agent + apply_variant delegate to the SAME clone-only HTTP primitive
    # `hotato apply` uses (GET the source, apply a merge-patch, POST a NEW staging
    # assistant). Production is never mutated; only a fresh clone is created. The
    # apply happens at clone time (you cannot clone-empty-then-apply), so
    # clone_agent stages the source+name and apply_variant performs the create.

    def inspect_config(self, ref):
        self._require("inspect_config"); self._need_key("inspect_config")
        from .. import apply as _apply
        endpoint = _apply._CLONE_ENDPOINTS[self.stack]
        source_id = _validated_source_id(ref, self.stack)
        read_url = endpoint["read_url_template"].format(id=source_id)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        return _apply._http_json("GET", read_url, headers=headers, body=None,
                                 timeout=30)

    def clone_agent(self, ref, *, name):
        self._require("clone_agent"); self._need_key("clone_agent")
        # stage; the network create happens in apply_variant (clone-with-patch)
        return {"stack": self.stack, "source_id": ref, "name": name,
                "pending": True}

    def apply_variant(self, clone_ref, variant):
        self._require("apply_variant"); self._need_key("apply_variant")
        from .. import apply as _apply
        source_id = clone_ref["source_id"] if isinstance(clone_ref, dict) else clone_ref
        source_id = _validated_source_id(source_id, self.stack)
        name = (clone_ref.get("name") if isinstance(clone_ref, dict) else None) or "hotato-staging"
        merge_patch = variant.get("config_delta", variant) or {}
        return _apply.create_clone(stack=self.stack, source_id=source_id, name=name,
                                   merge_patch=merge_patch, api_key=self.api_key)

    def run_scenario(self, clone_ref, scenario):
        self._require("run_scenario"); self._need_key("run_scenario")
        raise NotImplementedError(
            f"{self.stack} scripted scenario execution requires a connected capture "
            "runner; use hotato capture against the clone, then hotato fix trial")

    def capture_result(self, clone_ref, scenario):
        self._require("capture_result"); self._need_key("capture_result")
        raise NotImplementedError(
            f"{self.stack} result capture requires a connected capture runner; use "
            "hotato pull / hotato capture against the clone")


class VapiAdapter(_CredentialGatedAdapter):
    stack = "vapi"


class RetellAdapter(_CredentialGatedAdapter):
    stack = "retell"


class LiveKitAdapter(Adapter):
    """Source-config target: local test runner + config recipe, no hosted clone."""
    stack = "livekit"

    def capabilities(self) -> Set[str]:
        return {"inspect_config", "snapshot_config", "run_scenario", "capture_result"}


class PipecatAdapter(LiveKitAdapter):
    stack = "pipecat"


class MockAdapter(Adapter):
    """A fully local adapter implementing the WHOLE loop for tests: cloning is a
    dict copy, applying a variant tweaks the config, running a scenario
    synthesizes an 'after' recording from the scenario's before audio (a fix
    that makes the agent yield). Lets the experiment path be exercised end to end
    with zero live account."""
    stack = "mock"

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        os.makedirs(work_dir, exist_ok=True)
        self._clones: Dict[str, dict] = {}
        self._n = 0

    def capabilities(self) -> Set[str]:
        return set(CAPABILITIES)

    def inspect_config(self, ref):
        return {"stack": "mock", "turn_taking": {"interrupt_min_words": 3}}

    def clone_agent(self, ref, *, name):
        self._n += 1
        cid = f"mock-clone-{self._n}"
        self._clones[cid] = dict(self.inspect_config(ref))
        return cid

    def apply_variant(self, clone_ref, variant):
        cfg = self._clones[clone_ref]
        cfg.setdefault("turn_taking", {}).update(variant.get("config_delta", {}))
        return {"clone_ref": clone_ref, "config": cfg,
                "config_hash": self.snapshot_config(cfg)}

    def run_scenario(self, clone_ref, scenario):
        """Synthesize a fresh 'after' recording: replay the scenario's caller
        stimulus, but with the fixed agent yielding (a real improvement)."""
        from ..fleet import _mock_capture
        return _mock_capture.capture_yielding(self.work_dir, clone_ref, scenario)

    def rollback(self, ref, revision):
        return {"ref": ref, "restored_revision": revision}

    def delete_clone(self, clone_ref):
        self._clones.pop(clone_ref, None)
        return {"deleted": clone_ref}


def get_adapter(stack: str, **kw) -> Adapter:
    stack = (stack or "").lower()
    if stack == "vapi":
        return VapiAdapter(**kw)
    if stack == "retell":
        return RetellAdapter(**kw)
    if stack == "livekit":
        return LiveKitAdapter()
    if stack == "pipecat":
        return PipecatAdapter()
    if stack == "mock":
        return MockAdapter(kw.get("work_dir", "."))
    raise CapabilityError(f"no adapter for stack {stack!r}")


__all__ = ["Adapter", "MockAdapter", "VapiAdapter", "RetellAdapter",
           "LiveKitAdapter", "PipecatAdapter", "get_adapter", "CapabilityError",
           "CAPABILITIES"]
