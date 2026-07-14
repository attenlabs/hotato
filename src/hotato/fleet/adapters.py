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
mutation is never silent. DRIVE-A-CALL: the Vapi and Twilio adapters implement
``run_scenario`` -- they ORIGINATE a real call against a live agent
(:mod:`hotato.drive`) and feed its recording into the normal capture -> score
pipeline. That op refuses without BOTH credentials AND an explicit egress opt-in,
so a real, billable call is never placed silently, and it never mutates
production config (a Vapi call is driven FROM the staging CLONE; nothing
PUT/PATCHes a live assistant). Retell has no confirmed create-call API and
LiveKit/Pipecat are capture-in-your-infra, so those stay honestly unadvertised
for ``run_scenario`` (``available=False``). A MockAdapter implements the whole
loop locally (using synthetic recapture) so the clone -> apply -> scenario ->
capture -> recompute path is exercisable and tested without any live account; it
alone reports the full loop available.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, Optional, Set

from .. import errors as _errors
from .. import manifest as _manifest

# A successful DELETE body is diagnostic-only and normally empty.  Keep the
# same small ceiling used for notification acknowledgements: enough for a
# provider message, never enough for a remote peer to drive an unbounded read.
_HTTP_DELETE_RESPONSE_MAX_BYTES = 64 * 1024

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


def _nest_dotted(patch: dict) -> dict:
    """Turn a flat patch that may use dotted paths ("stopSpeakingPlan.numWords": 0)
    or a {field,to} pair into a nested JSON merge-patch ({stopSpeakingPlan:{numWords:0}}).
    A plain nested dict passes through unchanged."""
    if isinstance(patch, dict) and "field" in patch and "to" in patch:
        patch = {patch["field"]: patch["to"]}

    def _merge(dst: dict, src: dict) -> None:
        for k2, v2 in src.items():
            if isinstance(v2, dict) and isinstance(dst.get(k2), dict):
                _merge(dst[k2], v2)
            else:
                dst[k2] = v2

    out = {}
    for k, v in (patch or {}).items():
        piece = v
        for seg in reversed(str(k).split(".")):
            piece = {seg: piece}
        # merge, don't clobber: {"a.b": 1, "a": {"c": 2}} keeps both b and c
        _merge(out, piece)
    return out


# --- drive-a-call gating (a real, billable phone call is never placed silently) ---
# run_scenario ORIGINATES a real call against a live agent. Two independent gates,
# mirroring the existing opt-in posture (HOTATO_ALLOW_MONO / HOTATO_ALLOW_PRIVATE_URLS
# / --egress-opt-in): (1) real credentials must be present, and (2) egress must be
# explicitly opted into. Absent either, run_scenario raises the same clean, structured
# CapabilityError refusal a hosted adapter has always given -- it never quietly dials.
_DRIVE_OPT_IN_ENV = "HOTATO_DRIVE_OPT_IN"
_TRUTHY = ("1", "true", "yes", "on")

_DRIVE_REFUSAL = (
    "run_scenario originates a REAL, billable phone call against a live agent, so "
    "it requires an explicit egress opt-in in addition to credentials. Set "
    f"{_DRIVE_OPT_IN_ENV}=1 or pass egress_opt_in=true in the scenario to authorize "
    "placing the call; this build never dials a real number silently."
)


def _drive_egress_opt_in(scenario) -> bool:
    """True only on an EXPLICIT opt-in: the ``HOTATO_DRIVE_OPT_IN`` env var, or an
    ``egress_opt_in`` truthy flag carried on the scenario. Default is off."""
    if os.environ.get(_DRIVE_OPT_IN_ENV, "").strip().lower() in _TRUTHY:
        return True
    if isinstance(scenario, dict):
        v = scenario.get("egress_opt_in")
        if v is True or (isinstance(v, str) and v.strip().lower() in _TRUTHY):
            return True
    return False


def _drive_param(scenario, *keys, env=None, default=None):
    """Resolve a drive parameter from the scenario (top-level or a nested
    ``drive`` block) or an environment variable, in that order. Lets a caller pass
    ``to_number`` / ``phone_number_id`` / ``customer_number`` / ``base_url`` /
    poll knobs either inline on the scenario or via the environment."""
    if isinstance(scenario, dict):
        drive = scenario.get("drive")
        drive = drive if isinstance(drive, dict) else {}
        for k in keys:
            for src in (scenario, drive):
                if src.get(k) not in (None, ""):
                    return src[k]
    if env:
        val = os.environ.get(env, "").strip()
        if val:
            return val
    return default


def _drive_poll_kwargs(scenario) -> Dict[str, object]:
    """Pass-through poll knobs (``poll_interval``/``max_wait``/``timeout``) a
    caller set on the scenario -- so a test can drive with ``poll_interval=0`` and
    an operator can widen ``max_wait`` for a slow agent without a code change."""
    kw: Dict[str, object] = {}
    if isinstance(scenario, dict):
        for k in ("poll_interval", "max_wait", "timeout"):
            v = scenario.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                kw[k] = v
    return kw


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
        "snapshot_config", "delete_clone",
    })
    # implemented ops/features that require credentials to be authorized.
    # run_scenario is credentialed too: where a subclass IMPLEMENTS it (VapiAdapter),
    # describe() reports authorized=False without a key; where it stays
    # @_unimplemented (Retell), available=False short-circuits before this check.
    _CREDENTIALED = frozenset({
        "inspect_config", "clone_agent", "apply_variant", "delete_clone", "capture_result",
        "pull_recordings", "dual_channel_capture", "run_scenario",
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
        if not str(name).lower().startswith("hotato"):
            # stamp the staging marker delete_clone requires before it deletes
            name = f"hotato-{name}"
        merge_patch = _nest_dotted(variant.get("config_delta", variant) or {})
        created = _apply.create_clone(stack=self.stack, source_id=source_id, name=name,
                                      merge_patch=merge_patch, api_key=self.api_key)
        # normalise: expose the created clone id so run_scenario/delete_clone can use it
        cid = created.get("clone_id") or created.get("id")
        if not cid:
            nested = created.get("created")
            if isinstance(nested, dict):
                cid = nested.get("id")
        created["clone_id"] = cid
        return created

    # run_scenario / capture_result are NOT implemented for a hosted provider.
    # Marked @_unimplemented -> discovery reports them available=False; calling
    # one raises with guidance rather than pretending to run. Credentials are
    # irrelevant: the operation does not exist regardless of the key.
    # delete_clone / capture_result are implemented + verified only where the
    # provider's endpoints are proven (VapiAdapter). The base stays honestly
    # @_unimplemented so a provider whose API differs (e.g. Retell) never
    # advertises an op that was never tested against it.
    @_unimplemented
    def delete_clone(self, clone_ref):
        raise NotImplementedError(
            f"{self.stack} delete_clone is not wired for this provider; only "
            "stacks with a verified delete endpoint implement it")

    @_unimplemented
    def run_scenario(self, clone_ref, scenario):
        # Drive-a-call IS implemented -- but only where the provider has a
        # confirmed create-call API the pull path was verified against. VapiAdapter
        # OVERRIDES this with a real implementation (POST /call -> poll ->
        # capture_vapi); Twilio drives via a separate transport adapter. This base
        # stays honestly @_unimplemented so a provider WITHOUT a confirmed
        # create-call endpoint (Retell) never advertises an origination it cannot
        # perform. Capture existing calls with capture_result / hotato pull instead.
        raise NotImplementedError(
            f"{self.stack} has no confirmed create-call API to originate a scored "
            "call; capture existing calls with capture_result / hotato pull, or "
            "use a provider whose create-call endpoint is verified (vapi/twilio)")

    @_unimplemented
    def capture_result(self, clone_ref, scenario=None, *, call_id=None, out_path=None):
        raise NotImplementedError(
            f"{self.stack} capture_result is not wired for this provider; only "
            "stacks with a verified recording endpoint implement it")


class VapiAdapter(_CredentialGatedAdapter):
    stack = "vapi"

    # Both verified against the live Vapi API (clone lifecycle + stereo pull).
    def delete_clone(self, clone_ref):
        """Delete a STAGING clone (never the source). Two TECHNICAL guards, not a
        docstring promise: (1) the id must be a plain platform id (same rule as
        every id-to-URL path here, so nothing can smuggle an extra URL segment);
        (2) the assistant is FETCHED first and must carry the "hotato" staging
        name marker apply_variant stamps at create time -- an id that points at a
        production assistant (which never carries the marker) REFUSES instead of
        deleting. Idempotent-ish: a 404 (already gone) is a no-op."""
        self._require("delete_clone"); self._need_key("delete_clone")
        cid = clone_ref.get("clone_id") if isinstance(clone_ref, dict) else clone_ref
        if not cid:
            return {"deleted": False, "reason": "no clone id"}
        import urllib.error
        import urllib.request

        from .. import apply as _apply
        cid = _validated_source_id(cid, self.stack)
        url = _apply._CLONE_ENDPOINTS[self.stack]["read_url_template"].format(id=cid)
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "User-Agent": f"hotato/{_apply._ua_version()} (+https://hotato.dev)"}
        try:
            current = _apply._http_json("GET", url, headers=headers, body=None,
                                        timeout=30)
        except ValueError as e:
            # Branch on the real numeric HTTP status apply._http_json attaches
            # (.status_code), never on a substring of the message: the message
            # embeds the caller id and up to 300 raw vendor-response bytes, and
            # either can incidentally contain the digits "404" for an unrelated
            # (and possibly non-404) status -- e.g. a genuine 500/auth/rate-limit
            # error would then be silently reported as a successful delete.
            if getattr(e, "status_code", None) == 404:
                return {"deleted": True, "clone_id": cid, "already_gone": True}
            raise
        name = str((current or {}).get("name") or "")
        if not name.lower().startswith("hotato"):
            raise ValueError(
                f"refusing to delete {cid}: its name {name!r} does not carry the "
                "hotato staging marker, so it may be a production assistant. "
                "delete_clone only removes staging clones this tool created.")
        req = urllib.request.Request(url, method="DELETE", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                _errors.read_bounded_http_body(
                    response,
                    max_bytes=_HTTP_DELETE_RESPONSE_MAX_BYTES,
                    subject="Vapi delete-clone response",
                )
            return {"deleted": True, "clone_id": cid}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"deleted": True, "clone_id": cid, "already_gone": True}
            raise ValueError(f"delete_clone HTTP {e.code} for {cid}: {e.reason}") from e

    def capture_result(self, clone_ref, scenario=None, *, call_id=None, out_path=None):
        """Pull a call's DUAL-CHANNEL recording to a WAV (real, works today). Given
        a ``call_id`` (or the most recent call for the agent), fetch the call,
        take its stereo recording url, and download it. Returns
        {recording, call_id, stereo}."""
        self._require("capture_result"); self._need_key("capture_result")
        import os
        import tempfile

        from .. import capture as _cap
        h = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if call_id is None:
            calls = _cap._http_get_json("https://api.vapi.ai/call?limit=1", headers=h, timeout=30)
            cl = calls if isinstance(calls, list) else calls.get("results", [])
            if not cl:
                raise ValueError("no calls on the account to capture")
            call = cl[0]
        else:
            call = _cap._http_get_json(f"https://api.vapi.ai/call/{call_id}", headers=h, timeout=30)
        art = call.get("artifact") or {}
        rec = art.get("recording") or {}
        url = rec.get("stereoUrl") or art.get("stereoRecordingUrl")
        if not url:
            raise ValueError(f"call {call.get('id')} has no stereo recording")
        out_path = out_path or os.path.join(tempfile.mkdtemp(prefix="hotato-cap-"),
                                            f"{call.get('id','call')}.wav")
        # the URL comes from the vendor's JSON response (untrusted): fetch it
        # through capture's validated download (scheme allowlist, private-host
        # refusal, HOTATO_INGEST_ALLOWED_HOSTS pin, atomic write) -- never raw.
        _cap._download(url, out_path, timeout=90)
        return {"recording": out_path, "call_id": call.get("id"), "stereo": True}

    def run_scenario(self, clone_ref, scenario):
        """DRIVE a real call FROM the (clone) assistant, then pull its stereo
        recording -- a real agent conversation scored through the normal pipeline.
        Two gates before any dial: credentials (``_need_key``) AND an explicit
        egress opt-in (``_drive_egress_opt_in``); absent either, refuses with the
        same clean structured CapabilityError a hosted op has always given, and
        never places the call. Production is untouched: the call is originated
        FROM the CLONE assistant (``clone_ref``), and drive.place_call_vapi only
        POSTs a new call + GETs status -- it never PUT/PATCHes an assistant config.

        The scenario carries the drive parameters (inline or under a ``drive``
        block, or via env): ``phone_number_id`` (VAPI_PHONE_NUMBER_ID) and
        ``customer_number`` (HOTATO_DRIVE_CUSTOMER_NUMBER). Returns
        drive.place_call_vapi's ``{recording, provider, provider_call_id, status,
        origin}`` -- ``origin.kind == "real"``, ``origin.caller ==
        "assistant-originated"`` (the assistant places the call; the caller was
        NOT a scripted human)."""
        self._require("run_scenario"); self._need_key("run_scenario")
        if not _drive_egress_opt_in(scenario):
            raise CapabilityError(_DRIVE_REFUSAL)
        from .. import drive as _drive
        phone_number_id = _drive_param(
            scenario, "phone_number_id", "phoneNumberId", env="VAPI_PHONE_NUMBER_ID")
        customer_number = _drive_param(
            scenario, "customer_number", env="HOTATO_DRIVE_CUSTOMER_NUMBER")
        if not phone_number_id or not customer_number:
            raise CapabilityError(
                "vapi run_scenario needs a phone_number_id (VAPI_PHONE_NUMBER_ID) "
                "and a customer_number (HOTATO_DRIVE_CUSTOMER_NUMBER) to originate "
                "the call FROM the assistant; set them on the scenario or the "
                "environment")
        base_url = _drive_param(
            scenario, "base_url", env="VAPI_BASE_URL", default="https://api.vapi.ai")
        return _drive.place_call_vapi(
            clone_ref, phone_number_id=phone_number_id,
            customer_number=customer_number, api_key=self.api_key,
            base_url=base_url, **_drive_poll_kwargs(scenario))


class RetellAdapter(_CredentialGatedAdapter):
    stack = "retell"


class TwilioAdapter(Adapter):
    """Telephony-transport adapter for DRIVE-A-CALL. Twilio has no hosted
    'assistant config' to clone/apply/inspect, so it offers only the offline
    config hash plus the two drive ops, both real and both credential-gated:

    * ``run_scenario`` -- render the scenario.v1 caller script to TwiML and place
      a FIXED-TIMELINE scripted call AT the agent, recorded dual-channel, then
      pull it (:func:`hotato.drive.place_call_twilio`). Additionally requires an
      explicit egress opt-in so a real, billable call is never dialled silently.
    * ``capture_result`` -- pull a Twilio dual-channel recording by its
      ``recording_sid`` (reuses :func:`hotato.capture.capture_twilio`).

    Credentials are the Twilio ``account_sid`` + ``auth_token`` (Basic auth), not
    a single Bearer key, so this subclasses ``Adapter`` directly rather than the
    Bearer-key ``_CredentialGatedAdapter``. It never clones/mutates anything."""
    stack = "twilio"

    _OFFERED = frozenset({
        "snapshot_config", "run_scenario", "capture_result",
        "pull_recordings", "dual_channel_capture",
    })
    _CREDENTIALED = frozenset({
        "run_scenario", "capture_result", "pull_recordings", "dual_channel_capture",
    })

    def __init__(self, account_sid: Optional[str] = None,
                 auth_token: Optional[str] = None):
        self.account_sid = account_sid
        self.auth_token = auth_token

    def _offered(self) -> Set[str]:
        return set(self._OFFERED)

    def _has_credentials(self) -> bool:
        return bool(self.account_sid and self.auth_token)

    def _needs_credentials(self, cap: str) -> bool:
        return cap in self._CREDENTIALED

    def _need_creds(self, cap):
        if not (self.account_sid and self.auth_token):
            raise CapabilityError(
                f"twilio {cap} requires credentials (TWILIO_ACCOUNT_SID + "
                "TWILIO_AUTH_TOKEN); this build never places a call silently")

    def run_scenario(self, clone_ref, scenario):
        """DRIVE a real, FIXED-TIMELINE scripted call at the agent and pull its
        dual-channel recording. Two gates before any dial: credentials AND an
        explicit egress opt-in; absent either, refuses (CapabilityError) without
        placing the call. The scenario is BOTH the caller script (rendered to
        TwiML) and the drive-parameter carrier: ``to_number`` (the agent's number,
        HOTATO_DRIVE_TO_NUMBER) and ``from_number`` (your Twilio number,
        HOTATO_DRIVE_FROM_NUMBER). Returns drive.place_call_twilio's
        ``{recording, provider, provider_call_id, recording_sid, status,
        origin}`` -- ``origin.kind == "real"``, ``origin.caller ==
        "scripted-twiml"`` (a fixed-timeline caller, honestly not a human)."""
        self._require("run_scenario"); self._need_creds("run_scenario")
        if not _drive_egress_opt_in(scenario):
            raise CapabilityError(_DRIVE_REFUSAL)
        from .. import drive as _drive
        to_number = _drive_param(scenario, "to_number", env="HOTATO_DRIVE_TO_NUMBER")
        from_number = _drive_param(scenario, "from_number", env="HOTATO_DRIVE_FROM_NUMBER")
        if not to_number or not from_number:
            raise CapabilityError(
                "twilio run_scenario needs a to_number (the agent's phone number, "
                "HOTATO_DRIVE_TO_NUMBER) and a from_number (your Twilio number, "
                "HOTATO_DRIVE_FROM_NUMBER); set them on the scenario or the "
                "environment")
        base_url = _drive_param(
            scenario, "base_url", env="TWILIO_BASE_URL",
            default="https://api.twilio.com")
        return _drive.place_call_twilio(
            scenario, to_number=to_number, from_number=from_number,
            sid=self.account_sid, token=self.auth_token, base_url=base_url,
            **_drive_poll_kwargs(scenario))

    def capture_result(self, clone_ref=None, scenario=None, *, recording_sid=None,
                       out_path=None, allow_mono=False):
        """Pull a Twilio DUAL-CHANNEL recording by ``recording_sid`` (RE...) and
        return {recording, recording_sid, provider}. Reuses the verified
        capture_twilio path; requires the sid (there is no single 'most recent'
        recording per agent to guess at)."""
        self._require("capture_result"); self._need_creds("capture_result")
        if not recording_sid:
            raise ValueError(
                "twilio capture_result needs a recording_sid (RE...) to pull")
        from .. import capture as _cap
        rec = _cap.capture_twilio(
            recording_sid=recording_sid, account_sid=self.account_sid,
            auth_token=self.auth_token, out_path=out_path, allow_mono=allow_mono)
        return {"recording": rec, "recording_sid": recording_sid,
                "provider": "twilio"}


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
        return VapiAdapter(api_key=kw.get("api_key"))
    if stack == "retell":
        return RetellAdapter(api_key=kw.get("api_key"))
    if stack == "twilio":
        # explicit extraction (Twilio uses account_sid+auth_token, not api_key);
        # a stray kwarg like work_dir from a generic caller is ignored, not passed
        # into the constructor.
        return TwilioAdapter(account_sid=kw.get("account_sid"),
                             auth_token=kw.get("auth_token"))
    if stack == "livekit":
        return LiveKitAdapter()
    if stack == "pipecat":
        return PipecatAdapter()
    if stack == "mock":
        return MockAdapter(kw.get("work_dir", "."))
    raise CapabilityError(f"no adapter for stack {stack!r}")


__all__ = ["Adapter", "MockAdapter", "VapiAdapter", "RetellAdapter",
           "TwilioAdapter", "LiveKitAdapter", "PipecatAdapter", "get_adapter",
           "CapabilityError", "CAPABILITIES"]
