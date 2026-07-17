"""Provider-neutral outbound voice-call control with bounded lifecycle evidence.

This module makes provider calls; it does not claim a carrier path succeeded
until the provider reports lifecycle evidence. Provider reports remain
provider-reported. A completed status establishes connection and media transfer
only to the extent documented by that provider; it does not establish task
success, participant identity, or recording correctness.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple

from .call_runtime import Capability, CapabilityState, CapabilityUnavailable, capability
from .errors import safe_json_dumps
from .manifest import canonical_json

PROVIDERS = ("twilio", "vapi", "retell", "local")
TERMINAL_STATUSES = frozenset({"completed", "failed", "busy", "no-answer", "canceled"})
SUCCESS_STATUSES = frozenset({"completed"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_BODY = 8 * 1024 * 1024

# This matrix describes the lifecycle controller in this module.  Media-plane
# features depend on a configured provider application or sidecar and therefore
# remain UNOBSERVABLE here even when the provider offers them elsewhere.
_COMMON_EXPORT = capability(
    CapabilityState.SUPPORTED,
    "exports Hotato's redacted lifecycle receipts; this is not a provider recording or media export",
)
_REMOTE_DELETE_UNSUPPORTED = capability(
    CapabilityState.UNSUPPORTED,
    "the lifecycle controller never deletes provider call history",
)
_MEDIA_UNOBSERVABLE = capability(
    CapabilityState.UNOBSERVABLE,
    "provider lifecycle status does not prove delivered media; attach a ConversationSession evidence stream",
)
_SIGNALLING_UNOBSERVABLE = capability(
    CapabilityState.UNOBSERVABLE,
    "the operation depends on provider application or sidecar evidence outside this lifecycle controller",
)


class TelephonyError(RuntimeError):
    pass


def _read_regular_bytes_with_identity(
    path: str, maximum: int
) -> Tuple[bytes, Tuple[int, int]]:
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise TelephonyError("telephony export must be a regular non-symlink file")
    if before.st_size > maximum:
        raise TelephonyError("telephony export exceeded 8 MiB")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise TelephonyError("telephony export changed while it was opened")
        chunks = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise TelephonyError("telephony export exceeded 8 MiB")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or len(raw) != opened.st_size
        ):
            raise TelephonyError("telephony export changed while it was read")
        return raw, (opened.st_dev, opened.st_ino)
    finally:
        os.close(descriptor)


def _read_regular_bytes(path: str, maximum: int) -> bytes:
    return _read_regular_bytes_with_identity(path, maximum)[0]


class HTTPTransport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes, timeout: float) -> Tuple[int, Mapping[str, str], bytes]: ...


class UrllibTransport:
    """Bounded HTTPS transport for fixed provider origins."""

    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes, timeout: float) -> Tuple[int, Mapping[str, str], bytes]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            raise TelephonyError("provider transport requires a credential-free HTTPS URL")
        req = urllib.request.Request(url, data=body or None, method=method, headers=dict(headers))

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, response_headers, new_url):  # type: ignore[no-untyped-def]
                del request, fp, code, message, response_headers, new_url
                return None

        # Credentials must not follow redirects or an ambient proxy. Operators
        # that need controlled proxy egress can inject an explicit transport.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        try:
            with opener.open(req, timeout=timeout) as response:  # nosec: fixed origins built below
                raw = response.read(_MAX_BODY + 1)
                if len(raw) > _MAX_BODY:
                    raise TelephonyError("provider response exceeded 8 MiB")
                return int(response.status), dict(response.headers.items()), raw
        except urllib.error.HTTPError as exc:
            raw = exc.read(_MAX_BODY + 1)
            if len(raw) > _MAX_BODY:
                raw = raw[:_MAX_BODY]
            return int(exc.code), dict(exc.headers.items()), raw
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelephonyError(f"provider request failed: {type(exc).__name__}") from exc


@dataclass(frozen=True)
class CallSpec:
    id: str
    provider: str
    to: str
    from_: Optional[str]
    agent_id: Optional[str]
    phone_number_id: Optional[str]
    twiml_url: Optional[str]
    callback_url: Optional[str]
    timeout_seconds: int
    record: bool
    metadata: Dict[str, str]


@dataclass(frozen=True)
class CallHandle:
    provider: str
    call_id: str
    normalized_status: str
    provider_status: str
    created_at: str
    receipt: Dict[str, Any]


@dataclass(frozen=True)
class ProviderLifecycleContract:
    """One canonical signalling contract shared by lifecycle and drive paths.

    ``TelephonyClient`` owns provider call creation/status/cancellation.
    :mod:`hotato.drive` composes a scripted caller with that signalling surface
    and then delegates recording retrieval to :mod:`hotato.capture`.  Keeping
    endpoint templates, identifier fields, and status normalization here makes
    those two higher-level paths fail a contract test when a provider surface is
    changed in only one place.

    This is deliberately a signalling contract.  It makes no claim about
    delivered audio, recording availability, task outcome, or carrier evidence.
    """

    provider: str
    api_origin: str
    create_path: str
    get_path: str
    call_id_fields: Tuple[str, ...]
    terminal_provider_statuses: Tuple[str, ...]


PROVIDER_LIFECYCLE_CONTRACTS: Mapping[str, ProviderLifecycleContract] = {
    "twilio": ProviderLifecycleContract(
        provider="twilio",
        api_origin="https://api.twilio.com",
        create_path="/2010-04-01/Accounts/{account_sid}/Calls.json",
        get_path="/2010-04-01/Accounts/{account_sid}/Calls/{call_id}.json",
        call_id_fields=("sid",),
        terminal_provider_statuses=(
            "completed", "busy", "failed", "no-answer", "canceled",
        ),
    ),
    "vapi": ProviderLifecycleContract(
        provider="vapi",
        api_origin="https://api.vapi.ai",
        create_path="/call",
        get_path="/call/{call_id}",
        call_id_fields=("id",),
        terminal_provider_statuses=("ended", "failed"),
    ),
    "retell": ProviderLifecycleContract(
        provider="retell",
        api_origin="https://api.retellai.com",
        create_path="/v2/create-phone-call",
        get_path="/v2/get-call/{call_id}",
        call_id_fields=("call_id", "id"),
        terminal_provider_statuses=("ended", "error", "not-connected"),
    ),
}


def provider_lifecycle_contract(provider: str) -> ProviderLifecycleContract:
    """Return the fixed signalling contract for a remote provider."""

    if not isinstance(provider, str) or provider not in PROVIDER_LIFECYCLE_CONTRACTS:
        raise ValueError("provider lifecycle contract requires twilio, vapi, or retell")
    return PROVIDER_LIFECYCLE_CONTRACTS[provider]


def provider_lifecycle_url(
    provider: str,
    operation: str,
    *,
    base_url: Optional[str] = None,
    account_sid: Optional[str] = None,
    call_id: Optional[str] = None,
) -> str:
    """Build a create/get URL from the canonical provider contract.

    ``base_url`` remains injectable so the established drive tests and private
    provider-compatible gateways can use a controlled origin.  This helper does
    not perform I/O and therefore does not weaken either caller's egress gate.
    Identifiers are quoted as path segments before interpolation.
    """

    contract = provider_lifecycle_contract(provider)
    if operation == "create":
        template = contract.create_path
    elif operation == "get":
        template = contract.get_path
        if not isinstance(call_id, str) or not call_id or len(call_id) > 500:
            raise ValueError("call_id must be a bounded non-empty string")
    else:
        raise ValueError("operation must be 'create' or 'get'")
    if "{account_sid}" in template:
        if not isinstance(account_sid, str) or not account_sid:
            raise ValueError("twilio lifecycle URL requires account_sid")
        encoded_account = urllib.parse.quote(account_sid, safe="")
    else:
        encoded_account = ""
    encoded_call = urllib.parse.quote(call_id or "", safe="")
    if base_url is None:
        origin = contract.api_origin
    elif isinstance(base_url, str) and base_url:
        origin = base_url.rstrip("/")
    else:
        raise ValueError("base_url must be a non-empty string")
    return origin + template.format(
        account_sid=encoded_account,
        call_id=encoded_call,
    )


def _text(value: Any, label: str, *, required: bool = False, maximum: int = 1000) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def validate_spec(value: Any) -> CallSpec:
    if not isinstance(value, dict):
        raise ValueError("telephony call spec must be a mapping")
    allowed = {"schema", "id", "provider", "to", "from", "agent_id", "phone_number_id", "twiml_url", "callback_url", "timeout_seconds", "record", "metadata"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("telephony call spec contains unknown field(s): " + ", ".join(unknown))
    if value.get("schema") != "hotato.telephony-call.v1":
        raise ValueError("telephony call spec schema must be 'hotato.telephony-call.v1'")
    spec_id = _text(value.get("id"), "id", required=True, maximum=200)
    if not _SAFE_ID.fullmatch(spec_id or ""):
        raise ValueError("id must be a filesystem-safe identifier")
    provider = str(value.get("provider", "")).lower()
    if provider not in PROVIDERS:
        raise ValueError("provider must be one of " + ", ".join(PROVIDERS))
    timeout = value.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise ValueError("timeout_seconds must be an integer in [1, 600]")
    record = value.get("record", True)
    if not isinstance(record, bool):
        raise ValueError("record must be a boolean")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict) or len(metadata) > 64 or not all(isinstance(k, str) and isinstance(v, str) and len(k) <= 100 and len(v) <= 1000 for k, v in metadata.items()):
        raise ValueError("metadata must contain at most 64 bounded string pairs")
    if "hotato_run_id" in metadata:
        raise ValueError("metadata key 'hotato_run_id' is reserved for call correlation")
    result = CallSpec(
        id=spec_id or "", provider=provider,
        to=_text(value.get("to"), "to", required=True) or "",
        from_=_text(value.get("from"), "from"),
        agent_id=_text(value.get("agent_id"), "agent_id"),
        phone_number_id=_text(value.get("phone_number_id"), "phone_number_id"),
        twiml_url=_text(value.get("twiml_url"), "twiml_url", maximum=4096),
        callback_url=_text(value.get("callback_url"), "callback_url", maximum=4096),
        timeout_seconds=timeout, record=record, metadata=dict(metadata),
    )
    if provider == "twilio" and (not result.from_ or not result.twiml_url):
        raise ValueError("twilio requires from and twiml_url")
    if provider == "vapi" and (not result.agent_id or not result.phone_number_id):
        raise ValueError("vapi requires agent_id and phone_number_id")
    if provider == "retell" and (not result.agent_id or not result.from_):
        raise ValueError("retell requires agent_id and from")
    return result


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise TelephonyError(f"missing environment credential: {name}")
    return value


def _json(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelephonyError("provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise TelephonyError("provider returned a non-object JSON response")
    return value


def normalize_provider_status(provider: str, value: Any) -> str:
    """Normalize one provider-reported status into Hotato's lifecycle states."""

    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("provider must be one of " + ", ".join(PROVIDERS))
    status = str(value or "unknown").strip().lower().replace("_", "-")
    maps = {
        "vapi": {"ended": "completed", "queued": "queued", "ringing": "ringing", "in-progress": "in-progress", "failed": "failed"},
        "retell": {"ended": "completed", "registered": "queued", "ongoing": "in-progress", "error": "failed", "not-connected": "no-answer"},
    }
    return maps.get(provider, {}).get(status, status)


def _normalize_status(provider: str, value: Any) -> str:
    """Compatibility alias for code that predates the shared contract."""

    return normalize_provider_status(provider, value)


def provider_status(doc: Mapping[str, Any]) -> str:
    """Extract and validate the raw status from a provider response."""

    value = doc.get("status")
    if value is None:
        value = doc.get("call_status")
    if value is None:
        return "unknown"
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise TelephonyError("provider returned an invalid or oversized call status")
    return value.strip()


def _provider_status(doc: Mapping[str, Any]) -> str:
    """Compatibility alias for the original private helper."""

    return provider_status(doc)


def provider_status_is_terminal(provider: str, value: Any) -> bool:
    """Whether a raw provider status maps to a terminal Hotato lifecycle state."""

    if provider == "local":
        return normalize_provider_status(provider, value) in TERMINAL_STATUSES
    contract = provider_lifecycle_contract(provider)
    raw = str(value or "unknown").strip().lower().replace("_", "-")
    return raw in contract.terminal_provider_statuses


def provider_status_is_success(provider: str, value: Any) -> bool:
    """Whether a raw provider status maps to lifecycle completion.

    Completion remains signalling evidence; it is not an outcome or recording
    correctness verdict.
    """

    return normalize_provider_status(provider, value) in SUCCESS_STATUSES


def provider_call_id(provider: str, doc: Mapping[str, Any]) -> str:
    """Extract a bounded provider call id using the canonical field contract."""

    contract = provider_lifecycle_contract(provider)
    for name in contract.call_id_fields:
        value = doc.get(name)
        if isinstance(value, str) and value and len(value) <= 500:
            return value
    raise TelephonyError(
        "provider create response did not contain a bounded call id in "
        + ", ".join(contract.call_id_fields)
    )


_SAFE_PROVIDER_TIMESTAMPS = frozenset({
    "createdAt", "updatedAt", "startedAt", "endedAt",
})


def _redacted_response(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return a lifecycle allow-list plus a digest of the complete response.

    Recursive deny-lists are unsafe for provider objects: a new nested field
    such as ``messages[].content`` can carry customer speech without using a
    key named ``transcript``.  Lifecycle receipts therefore retain only fixed,
    non-payload scalars.  The digest still binds the receipt to the response
    that was observed without copying audio URLs, numbers, messages, tool
    payloads, metadata, or provider credentials into the artifact.
    """

    canonical = safe_json_dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    selected: Dict[str, Any] = {}
    for name in ("status", "call_status"):
        if name in doc:
            selected[name] = _provider_status({name: doc[name]})
    for name in ("duration", "duration_ms"):
        value = doc.get(name)
        if name in doc and isinstance(value, (int, float)) and not isinstance(value, bool):
            selected[name] = value
    for name in sorted(_SAFE_PROVIDER_TIMESTAMPS):
        value = doc.get(name)
        if (
            isinstance(value, str)
            and len(value) <= 64
            and re.fullmatch(r"[0-9TZ:.,+\-]+", value)
        ):
            selected[name] = value
    limits = doc.get("subscriptionLimits")
    if isinstance(limits, dict):
        allowed_limits = {
            name: limits[name]
            for name in (
                "concurrencyBlocked", "concurrencyLimit",
                "remainingConcurrentCalls",
            )
            if name in limits and isinstance(limits[name], (int, float, bool))
        }
        if allowed_limits:
            selected["subscriptionLimits"] = allowed_limits
    return {
        "selected": selected,
        "omitted_top_level_field_count": len(set(doc) - set(selected)),
        "canonical_bytes": len(canonical),
        "canonical_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "payload_policy": "fixed_lifecycle_allowlist",
    }


def _receipt(value: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(value)
    result["receipt_id"] = "sha256:" + hashlib.sha256(
        canonical_json(result).encode("utf-8")
    ).hexdigest()
    return result


def _call_id_sha256(provider: str, call_id: str) -> str:
    return "sha256:" + hashlib.sha256(
        (provider + "\x00" + call_id).encode("utf-8")
    ).hexdigest()


def _provider(value: Any) -> str:
    if not isinstance(value, str) or value not in PROVIDERS:
        raise ValueError("provider must be one of " + ", ".join(PROVIDERS))
    return value


def build_lifecycle_receipt(
    provider: str,
    call_id: str,
    response: Mapping[str, Any],
    *,
    operation: str,
    observed_at: str,
    spec_id: Optional[str] = None,
    request_field_names: Tuple[str, ...] = (),
    request_body_bytes: Optional[int] = None,
    authority: str = "provider_reported",
) -> Dict[str, Any]:
    """Build the canonical privacy-preserving receipt for a lifecycle event.

    Both ``TelephonyClient`` and ``hotato.drive`` use this function.  Only the
    fixed lifecycle allow-list is copied from ``response``; the full canonical
    response is represented by byte count and digest.  Raw phone numbers, call
    ids, transcripts, recording URLs, tool payloads, and provider extensions do
    not enter the receipt.
    """

    _provider(provider)
    if provider != "local":
        provider_lifecycle_contract(provider)
    if not isinstance(call_id, str) or not call_id or len(call_id) > 500:
        raise ValueError("call_id must be a bounded non-empty string")
    if operation not in {"create", "get", "wait", "cancel"}:
        raise ValueError("unsupported lifecycle receipt operation")
    if not isinstance(response, Mapping):
        raise TypeError("response must be a mapping")
    if authority not in {"provider_reported", "local_measured"}:
        raise ValueError("invalid lifecycle receipt authority")
    value: Dict[str, Any] = {
        "schema": "hotato.telephony-receipt.v1",
        "operation": operation,
        "provider": provider,
        "call_id_sha256": _call_id_sha256(provider, call_id),
        "observed_at": observed_at,
        "provider_response": _redacted_response(dict(response)),
        "authority": authority,
    }
    if spec_id is not None:
        value["spec_id"] = spec_id
    if request_field_names or request_body_bytes is not None:
        value["request"] = {
            "field_names": list(request_field_names),
            "body_bytes": 0 if request_body_bytes is None else request_body_bytes,
        }
    return _receipt(value)


def _handle(value: Any) -> CallHandle:
    if not isinstance(value, CallHandle):
        raise TypeError("handle must be a CallHandle")
    _provider(value.provider)
    if not value.call_id or len(value.call_id) > 500:
        raise ValueError("handle call_id must be a bounded non-empty string")
    return value


class TelephonyClient:
    def __init__(self, transport: Optional[HTTPTransport] = None, *, clock=time.time) -> None:
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self._local_sequence = 0
        self._local_lock = threading.Lock()

    def capabilities(self, provider: str) -> Mapping[str, Capability]:
        """Return lifecycle-controller support without implying media support."""

        provider = _provider(provider)
        lifecycle = capability(CapabilityState.SUPPORTED, "implemented by this provider lifecycle controller")
        if provider == "twilio":
            cancel = capability(
                CapabilityState.SUPPORTED,
                "requests canceled for queued/ringing calls and completed to terminate an in-progress call",
            )
        elif provider == "local":
            cancel = lifecycle
        else:
            cancel = capability(
                CapabilityState.UNSUPPORTED,
                f"{provider} cancellation is not wired in this controller; no provider mutation is guessed",
            )
        status = lifecycle if provider != "local" else capability(
            CapabilityState.UNSUPPORTED,
            "local handles are ephemeral and have no persistent provider status endpoint",
        )
        return {
            "create": lifecycle,
            "status": status,
            "wait": lifecycle,
            "cancel": cancel,
            "portable_export": _COMMON_EXPORT,
            "local_cleanup": capability(CapabilityState.SUPPORTED, "deletes only a matching Hotato portable receipt supplied by path"),
            "remote_delete": _REMOTE_DELETE_UNSUPPORTED,
            "media": _MEDIA_UNOBSERVABLE,
            "dtmf": _SIGNALLING_UNOBSERVABLE,
            "hold": _SIGNALLING_UNOBSERVABLE,
            "cold_transfer": _SIGNALLING_UNOBSERVABLE,
            "warm_transfer": _SIGNALLING_UNOBSERVABLE,
        }

    def _request(self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Dict[str, Any]:
        status, _response_headers, raw = self.transport.request(method, url, headers=headers, body=body, timeout=timeout)
        doc = _json(raw)
        if status < 200 or status >= 300:
            raise TelephonyError(f"provider returned HTTP {status}: fields={sorted(doc)[:20]}")
        return doc

    def create(self, spec_value: Any) -> CallHandle:
        spec = validate_spec(spec_value) if not isinstance(spec_value, CallSpec) else spec_value
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        if spec.provider == "local":
            with self._local_lock:
                self._local_sequence += 1
                local_sequence = self._local_sequence
            call_id = "local-" + hashlib.sha256(
                f"{spec.id}:{timestamp}:{local_sequence}".encode()
            ).hexdigest()[:20]
            doc = {"id": call_id, "status": "queued"}
            fields = ["id", "status"]
        elif spec.provider == "twilio":
            sid = _env("TWILIO_ACCOUNT_SID"); token = _env("TWILIO_AUTH_TOKEN")
            url = provider_lifecycle_url(
                "twilio", "create", account_sid=sid,
            )
            pairs = [("To", spec.to), ("From", spec.from_ or ""), ("Url", spec.twiml_url or ""), ("Timeout", str(spec.timeout_seconds)), ("Record", str(spec.record).lower()), ("RecordingChannels", "dual")]
            if spec.callback_url:
                pairs.extend([("StatusCallback", spec.callback_url), ("StatusCallbackMethod", "POST")])
                for event in ("initiated", "ringing", "answered", "completed"):
                    pairs.append(("StatusCallbackEvent", event))
            body = urllib.parse.urlencode(pairs).encode()
            auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
            doc = self._request("POST", url, {"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, body, 30)
            call_id = provider_call_id("twilio", doc)
            fields = [name for name, _ in pairs]
        elif spec.provider == "vapi":
            # Vapi's current canonical create surface is POST /call.  Some
            # provider-specific guides still show the older /call/phone alias;
            # use the API-reference endpoint so the adapter has one stable
            # request contract.
            url = provider_lifecycle_url("vapi", "create")
            payload = {"assistantId": spec.agent_id, "phoneNumberId": spec.phone_number_id, "customer": {"number": spec.to}, "metadata": {**spec.metadata, "hotato_run_id": spec.id}}
            body = safe_json_dumps(payload, separators=(",", ":")).encode()
            doc = self._request("POST", url, {"Authorization": "Bearer " + _env("VAPI_API_KEY"), "Content-Type": "application/json", "Accept": "application/json"}, body, 30)
            call_id = provider_call_id("vapi", doc)
            fields = sorted(payload)
        else:
            url = provider_lifecycle_url("retell", "create")
            payload = {"from_number": spec.from_, "to_number": spec.to, "override_agent_id": spec.agent_id, "metadata": {**spec.metadata, "hotato_run_id": spec.id}}
            body = safe_json_dumps(payload, separators=(",", ":")).encode()
            doc = self._request("POST", url, {"Authorization": "Bearer " + _env("RETELL_API_KEY"), "Content-Type": "application/json", "Accept": "application/json"}, body, 30)
            call_id = provider_call_id("retell", doc)
            fields = sorted(payload)
        if not isinstance(call_id, str) or not call_id or len(call_id) > 500:
            raise TelephonyError("provider create response did not contain a bounded call id")
        provider_status = _provider_status(doc)
        if provider_status == "unknown":
            provider_status = "queued"
        receipt = build_lifecycle_receipt(
            spec.provider,
            call_id,
            doc,
            operation="create",
            observed_at=timestamp,
            spec_id=spec.id,
            request_field_names=tuple(fields),
            request_body_bytes=0 if spec.provider == "local" else len(body),
            authority="local_measured" if spec.provider == "local" else "provider_reported",
        )
        return CallHandle(spec.provider, call_id, _normalize_status(spec.provider, provider_status), provider_status, timestamp, receipt)

    def get(self, provider: str, call_id: str) -> CallHandle:
        if provider not in PROVIDERS or provider == "local":
            raise TelephonyError("get requires twilio, vapi, or retell")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 500:
            raise ValueError("call_id must be a bounded non-empty string")
        if provider == "twilio":
            sid = _env("TWILIO_ACCOUNT_SID"); token = _env("TWILIO_AUTH_TOKEN")
            url = provider_lifecycle_url(
                "twilio", "get", account_sid=sid, call_id=call_id,
            )
            auth = base64.b64encode(f"{sid}:{token}".encode()).decode(); headers = {"Authorization": "Basic " + auth, "Accept": "application/json"}
        elif provider == "vapi":
            url = provider_lifecycle_url("vapi", "get", call_id=call_id); headers = {"Authorization": "Bearer " + _env("VAPI_API_KEY"), "Accept": "application/json"}
        else:
            url = provider_lifecycle_url("retell", "get", call_id=call_id); headers = {"Authorization": "Bearer " + _env("RETELL_API_KEY"), "Accept": "application/json"}
        doc = self._request("GET", url, headers, b"", 30)
        provider_status = _provider_status(doc)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        receipt = build_lifecycle_receipt(
            provider,
            call_id,
            doc,
            operation="get",
            observed_at=timestamp,
        )
        return CallHandle(provider, call_id, _normalize_status(provider, provider_status), provider_status, timestamp, receipt)

    def wait(self, handle: CallHandle, *, timeout_seconds: float = 600, poll_seconds: float = 2.0, sleeper=time.sleep) -> CallHandle:
        handle = _handle(handle)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 86400:
            raise ValueError("timeout_seconds must be in (0, 86400]")
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 0 < poll_seconds <= 300:
            raise ValueError("poll_seconds must be in (0, 300]")
        if handle.provider == "local":
            response = {"status": "completed", "local_fixture": True}
            receipt = build_lifecycle_receipt(
                "local",
                handle.call_id,
                response,
                operation="wait",
                observed_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())
                ),
                authority="local_measured",
            )
            return CallHandle("local", handle.call_id, "completed", "completed", handle.created_at, receipt)
        deadline = time.monotonic() + timeout_seconds
        current = handle
        while not provider_status_is_terminal(
            current.provider, current.provider_status
        ):
            if time.monotonic() >= deadline:
                raise TelephonyError("timed out waiting for terminal call status")
            sleeper(poll_seconds)
            current = self.get(handle.provider, handle.call_id)
        return current

    def cancel(self, handle: CallHandle) -> CallHandle:
        """Cancel a Twilio or local call; refuse providers without a wired API."""

        handle = _handle(handle)
        if handle.provider not in {"twilio", "local"}:
            entry = self.capabilities(handle.provider)["cancel"]
            raise CapabilityUnavailable(f"cancel is {entry.state.value}: {entry.reason}")
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        if handle.provider == "local":
            doc: Dict[str, Any] = {"id": handle.call_id, "status": "canceled"}
            fields = ["status"]
            body_bytes = 0
        else:
            sid = _env("TWILIO_ACCOUNT_SID")
            token = _env("TWILIO_AUTH_TOKEN")
            url = provider_lifecycle_url(
                "twilio", "get", account_sid=sid, call_id=handle.call_id,
            )
            requested_status = "completed" if handle.normalized_status in {"in-progress", "answered"} else "canceled"
            body = urllib.parse.urlencode({"Status": requested_status}).encode("ascii")
            auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
            doc = self._request(
                "POST", url,
                {"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                body, 30,
            )
            fields = ["Status"]
            body_bytes = len(body)
        provider_status = _provider_status(doc)
        if provider_status == "unknown":
            provider_status = "canceled"
        receipt = build_lifecycle_receipt(
            handle.provider,
            handle.call_id,
            doc,
            operation="cancel",
            observed_at=timestamp,
            request_field_names=tuple(fields),
            request_body_bytes=body_bytes,
            authority=(
                "local_measured" if handle.provider == "local"
                else "provider_reported"
            ),
        )
        return CallHandle(
            handle.provider, handle.call_id,
            _normalize_status(handle.provider, provider_status), provider_status,
            timestamp, receipt,
        )

    def export(self, handle: CallHandle, output_dir: str) -> str:
        """Write one redacted portable lifecycle receipt with exclusive create."""

        handle = _handle(handle)
        root = Path(output_dir)
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise TelephonyError("output_dir must be a directory and cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(f"{handle.provider}:{handle.call_id}".encode("utf-8")).hexdigest()[:24]
        path = root / f"call-{name}.json"
        exported = {
            "schema": "hotato.telephony-export.v1",
            "provider": handle.provider,
            "call_id_sha256": _call_id_sha256(handle.provider, handle.call_id),
            "normalized_status": handle.normalized_status,
            "provider_status": handle.provider_status,
            "observed_at": handle.created_at,
            "lifecycle_receipt": handle.receipt,
            "limitations": [
                "No recording or delivered-media evidence is included.",
                "Provider lifecycle status does not establish task outcome.",
            ],
        }
        data = (canonical_json(exported) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(str(path), flags, 0o600)
        except OSError as exc:
            raise TelephonyError(f"refusing to overwrite telephony export: {path.name}") from exc
        opened = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                current = os.lstat(path)
                if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    path.unlink()
            except OSError:
                pass
            raise
        return str(path)

    def cleanup(self, handle: CallHandle, export_path: Optional[str] = None) -> Mapping[str, Any]:
        """Delete only a matching local export; provider history is untouched."""

        handle = _handle(handle)
        deleted = False
        artifact_sha256: Optional[str] = None
        if export_path is not None:
            path = Path(export_path)
            raw, opened_identity = _read_regular_bytes_with_identity(
                str(path), _MAX_BODY
            )
            try:
                exported = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TelephonyError("export_path does not contain a JSON telephony export") from exc
            if not isinstance(exported, dict) or exported.get("schema") != "hotato.telephony-export.v1":
                raise TelephonyError("export_path is not a Hotato telephony export")
            if (
                exported.get("provider") != handle.provider
                or exported.get("call_id_sha256")
                != _call_id_sha256(handle.provider, handle.call_id)
            ):
                raise TelephonyError("export_path does not belong to this call handle")
            lifecycle_receipt = exported.get("lifecycle_receipt")
            if not isinstance(lifecycle_receipt, dict) or lifecycle_receipt.get("receipt_id") != handle.receipt.get("receipt_id"):
                raise TelephonyError("export_path lifecycle receipt does not match this call handle")
            artifact_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
            try:
                current = os.lstat(path)
            except OSError as exc:
                raise TelephonyError(
                    "export_path changed before cleanup"
                ) from exc
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != opened_identity
            ):
                raise TelephonyError("export_path changed before cleanup")
            path.unlink()
            deleted = True
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        return _receipt({
            "schema": "hotato.telephony-cleanup.v1",
            "provider": handle.provider,
            "call_id_sha256": _call_id_sha256(handle.provider, handle.call_id),
            "observed_at": timestamp,
            "local_export_deleted": deleted,
            "artifact_sha256": artifact_sha256,
            "provider_record_deleted": False,
            "authority": "local_measured",
        })
