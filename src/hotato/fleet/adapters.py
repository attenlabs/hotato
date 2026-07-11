"""Stack adapter capability contract for Fleet experiments.

Each adapter DECLARES the capabilities it is designed to provide, and capability
discovery reports each one HONESTLY: a capability is reported available ONLY when
its operation is actually implemented (returns a real result). An operation that
is a stub -- one that only raises ``NotImplementedError`` -- is marked
``@_unimplemented`` and reported ``available=False`` so an adapter never advertises
work it cannot do (plan §8).

Discovery surface:
  * ``describe()`` -> per-capability ``{available, authorized, reason}``. This is
    the honest record: it distinguishes "implemented but needs credentials"
    (``available=True, authorized=False``) from "not implemented"
    (``available=False``), and it never crashes just to answer the question.
  * ``capabilities()`` -> the set of AVAILABLE (implemented) capabilities.
  * ``supports(cap)`` -> ``True`` only when ``cap`` is available; ``False`` for a
    stub. A back-compat boolean that does not lie.

Capabilities:
  inspect_config, pull_recordings, dual_channel_capture, clone_agent,
  apply_variant, run_scenario, capture_result, snapshot_config, canary_route,
  rollback, delete_clone

Live provider adapters (Vapi, Retell) implement the offline capabilities
(config normalization + hashing) plus the clone/apply path, and REFUSE those
networked capabilities without credentials (``authorized=False``) so production
mutation is never silent. Scripted scenario execution and result capture are NOT
implemented for a hosted provider, so those are reported ``available=False``
rather than advertised. A MockAdapter implements the whole loop locally (using
synthetic recapture) so the clone -> apply -> scenario -> capture -> recompute
path is exercisable and tested without any live account; it alone reports the
full loop available.
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

# Capabilities that are invoked as an adapter METHOD. Availability of these is
# derived from whether the resolved method is a real implementation or an
# ``@_unimplemented`` stub -- so a declared-but-unimplemented op is caught
# automatically and reported available=False (never advertised). Capabilities
# NOT in this map are "feature" capabilities handled by other subsystems (e.g.
# recording pulls, canary routing); they have no adapter method that could raise
# NotImplementedError, so their availability is by declaration.
_OPERATION_METHODS = {
    "inspect_config": "inspect_config",
    "clone_agent": "clone_agent",
    "apply_variant": "apply_variant",
    "run_scenario": "run_scenario",
    "capture_result": "capture_result",
    "snapshot_config": "snapshot_config",
    "rollback": "rollback",
    "delete_clone": "delete_clone",
}


def _unimplemented(method):
    """Mark an adapter capability method as an unimplemented stub.

    Capability discovery (``describe`` / ``capabilities`` / ``supports``) treats a
    marked method as ``available=False``, so an adapter never advertises an
    operation that only raises ``NotImplementedError``. The method still raises if
    called directly -- the marker changes discovery, not runtime behavior."""
    method._hotato_unimplemented = True
    return method


class CapabilityError(RuntimeError):
    """Raised when a capability is used that the adapter does not make available,
    or that is implemented but requires credentials not present."""


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
    """Base adapter. Subclasses set ``stack``, declare their intended capabilities
    via ``_offered()``, and implement the ops they make available."""
    stack = "generic"
    version = "1"

    # --- capability discovery (honest) ---------------------------------
    def _offered(self) -> Set[str]:
        """The capabilities this adapter is DESIGNED to provide. Membership here
        is intent; ``describe()`` decides what is actually available."""
        return set()

    def _has_credentials(self) -> bool:
        """Whether the credentials this adapter's networked ops need are present.
        Adapters that need no credentials are always 'authorized'."""
        return True

    def _needs_credentials(self, cap: str) -> bool:
        """Whether ``cap`` requires credentials to be authorized (default: no)."""
        return False

    def _is_available(self, cap: str) -> bool:
        """True when ``cap`` is actually implemented. For a method-backed op this
        is False when the resolved method is an ``@_unimplemented`` stub; for a
        feature capability it is True when the adapter declares it."""
        method = _OPERATION_METHODS.get(cap)
        if method is None:
            return cap in self._offered()
        fn = getattr(type(self), method, None)
        if fn is None:
            return False
        return not getattr(fn, "_hotato_unimplemented", False)

    def _describe_one(self, cap: str) -> Dict[str, object]:
        if not self._is_available(cap):
            return {"available": False, "authorized": False,
                    "reason": "not implemented for this stack"}
        if self._needs_credentials(cap) and not self._has_credentials():
            return {"available": True, "authorized": False,
                    "reason": ("implemented; requires credentials (connect a "
                               "stack and supply an API key)")}
        return {"available": True, "authorized": True, "reason": "ready"}

    def describe(self) -> Dict[str, Dict[str, object]]:
        """Per-capability discovery: ``{cap: {available, authorized, reason}}``.

        ``available=True`` only when the capability is really implemented -- an
        operation that raises ``NotImplementedError`` reports ``available=False``.
        ``authorized=False`` means implemented-but-needs-credentials, reported
        without ever invoking (or crashing on) the operation."""
        return {cap: self._describe_one(cap) for cap in sorted(self._offered())}

    def capabilities(self) -> Set[str]:
        """The capabilities that are actually AVAILABLE (implemented). A declared
        capability whose operation only raises is excluded, so this set never
        overstates what works."""
        return {cap for cap in self._offered() if self._is_available(cap)}

    def supports(self, cap: str) -> bool:
        """Back-compat boolean: True only when ``cap`` is available (implemented);
        False for a stub. Never reports a raising operation as supported."""
        return self._is_available(cap) and cap in self._offered()

    def _require(self, cap: str):
        if not self.supports(cap):
            raise CapabilityError(
                f"{self.stack} adapter v{self.version} does not support {cap!r}")

    # --- offline capabilities (default implementations) ----------------
    def snapshot_config(self, config: dict) -> str:
        """Hash the exact effective turn-taking configuration -> a deployment
        identity. Available to every adapter that offers it (no network)."""
        self._require("snapshot_config")
        return hashlib.sha256(
            _manifest.canonical_json(config).encode("utf-8")).hexdigest()

    # --- networked capabilities (stubs; overridden by real implementations) ---
    # These raise NotImplementedError and are marked @_unimplemented so discovery
    # reports them available=False for any adapter that does not override them.
    @_unimplemented
    def inspect_config(self, ref):
        self._require("inspect_config"); raise NotImplementedError

    @_unimplemented
    def clone_agent(self, ref, *, name):
        self._require("clone_agent"); raise NotImplementedError

    @_unimplemented
    def apply_variant(self, clone_ref, variant):
        self._require("apply_variant"); raise NotImplementedError

    @_unimplemented
    def run_scenario(self, clone_ref, scenario):
        self._require("run_scenario"); raise NotImplementedError

    @_unimplemented
    def capture_result(self, clone_ref, scenario):
        self._require("capture_result"); raise NotImplementedError

    @_unimplemented
    def rollback(self, ref, revision):
        self._require("rollback"); raise NotImplementedError

    @_unimplemented
    def delete_clone(self, clone_ref):
        self._require("delete_clone"); raise NotImplementedError


class _CredentialGatedAdapter(Adapter):
    """A live provider adapter: offline config capabilities work; the networked
    capabilities that ARE implemented (inspect / clone / apply, recording pulls)
    refuse without credentials so production state is never mutated silently.
    Scenario execution and result capture are NOT implemented for a hosted
    provider, so discovery reports them available=False (never advertised)."""

    # ops/features this adapter is designed to provide. run_scenario and
    # capture_result stay declared so describe() can report them explicitly as
    # available=False ("intended, not implemented"); rollback/delete_clone are
    # deliberately NOT offered (live rollback/delete are not wired for a hosted
    # provider). The MockAdapter, which implements the whole loop, offers all.
    _OFFERED = frozenset({
        "inspect_config", "pull_recordings", "dual_channel_capture",
        "clone_agent", "apply_variant", "run_scenario", "capture_result",
        "snapshot_config",
    })
    # implemented ops/features that require credentials to be authorized.
    _CREDENTIALED = frozenset({
        "inspect_config", "clone_agent", "apply_variant",
        "pull_recordings", "dual_channel_capture",
    })

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    def _offered(self) -> Set[str]:
        return set(self._OFFERED)

    def _has_credentials(self) -> bool:
        return bool(self.api_key)

    def _needs_credentials(self, cap: str) -> bool:
        return cap in self._CREDENTIALED

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

    # run_scenario / capture_result are NOT implemented for a hosted provider.
    # Marked @_unimplemented -> discovery reports them available=False; calling
    # one raises with guidance rather than pretending to run. Credentials are
    # irrelevant: the operation does not exist regardless of the key.
    @_unimplemented
    def run_scenario(self, clone_ref, scenario):
        raise NotImplementedError(
            f"{self.stack} scripted scenario execution requires a connected capture "
            "runner; use hotato capture against the clone, then hotato fix trial")

    @_unimplemented
    def capture_result(self, clone_ref, scenario):
        raise NotImplementedError(
            f"{self.stack} result capture requires a connected capture runner; use "
            "hotato pull / hotato capture against the clone")


class VapiAdapter(_CredentialGatedAdapter):
    stack = "vapi"


class RetellAdapter(_CredentialGatedAdapter):
    stack = "retell"


class LiveKitAdapter(Adapter):
    """Source-config target: offline config hashing only. A local scenario runner
    and config inspection are on the roadmap but NOT implemented yet, so discovery
    reports only ``snapshot_config`` as available (the others report
    ``available=False`` rather than being advertised)."""
    stack = "livekit"

    def _offered(self) -> Set[str]:
        return {"inspect_config", "snapshot_config", "run_scenario", "capture_result"}


class PipecatAdapter(LiveKitAdapter):
    stack = "pipecat"


class MockAdapter(Adapter):
    """A fully local adapter implementing the WHOLE loop for tests: cloning is a
    dict copy, applying a variant tweaks the config, running a scenario
    synthesizes an 'after' recording from the scenario's before audio (a fix
    that makes the agent yield), and capturing returns that recorded artifact.
    Lets the experiment path be exercised end to end with zero live account; it
    alone reports every capability available."""
    stack = "mock"

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        os.makedirs(work_dir, exist_ok=True)
        self._clones: Dict[str, dict] = {}
        self._captures: Dict[tuple, dict] = {}
        self._n = 0

    def _offered(self) -> Set[str]:
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
        stimulus, but with the fixed agent yielding (a real improvement). The
        result is retained so ``capture_result`` can return it."""
        from ..fleet import _mock_capture
        cap = _mock_capture.capture_yielding(self.work_dir, clone_ref, scenario)
        self._captures[(clone_ref, scenario.get("id"))] = cap
        return cap

    def capture_result(self, clone_ref, scenario):
        """Return the captured artifact for the most recent scenario run against
        this clone; if none has run yet, drive the scenario now. A real, separate
        operation, so the ``capture_result`` capability is honestly available."""
        key = (clone_ref, scenario.get("id"))
        if key not in self._captures:
            return self.run_scenario(clone_ref, scenario)
        return self._captures[key]

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
