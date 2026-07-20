"""Optional webhook notifications for ``hotato sweep --notify`` and ``hotato
fleet run --notify``: a plain JSON POST to a URL the operator names, off by
default (no flag, no request -- see ``docs/EGRESS.md``).

Fires ONCE, after a sweep or a fleet run finishes, with counts and the top
candidate MOMENTS (timing numbers only -- never audio, never a credential,
never transcript text). A ``text`` field carries a one-line human summary so a
Slack incoming webhook renders it directly with no template work on the
receiving end.

Fail-open at the network boundary: a webhook is a side channel, never the
run's result. A DNS failure, a refused connection, a timeout, or a non-2xx
response from the receiving endpoint is ONE warning line on stderr and never
raises and never changes the caller's exit code -- a down or slow webhook must
not fail an otherwise-successful sweep. The one thing this module refuses
outright, before any network attempt, is the URL's scheme: http(s) only. That
check IS a raise (``ValueError``, the CLI's standard usage-error contract --
exit code 2) because a ``file://`` or ``data:`` typo turning a notify flag
into a local-file read or a surprising protocol is a usage mistake the
operator should see immediately, not a delivery failure to shrug off.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from . import errors as _errors

__all__ = [
    "validate_notify_url",
    "validate_notify_urls",
    "post_notification",
    "notify_all",
    "build_payload",
    "sweep_payload",
    "fleet_run_payload",
    "contract_verify_payload",
]

_ALLOWED_SCHEMES = ("http", "https")
_HTTP_NOTIFY_RESPONSE_MAX_BYTES = 64 * 1024

# The candidate fields a notify payload is ever allowed to carry: an id, its
# kind label, and pure timing numbers. Anything else a caller passes in a
# candidate dict (a filename, a transcript snippet, a components blob) is
# dropped here rather than trusted -- this whitelist, not caller discipline,
# is what keeps a future field added to analyze.py/fleet from silently
# leaking into an outbound webhook.
_CANDIDATE_FIELDS = ("id", "kind", "t_sec", "onset_sec", "severity", "durations")


def _version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "0"


def validate_notify_url(url: str) -> str:
    """Refuse anything but an http(s) URL with a host, BEFORE any network
    attempt. Raises ``ValueError`` (the CLI's standard usage-error contract --
    exit code 2) so a typo'd ``--notify`` URL is caught immediately, the same
    way a bad ``--min-gap`` is caught before the (slow, network) pull -- never
    after a sweep has already run."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"--notify {_errors.sanitize_url(url)!r} uses the unsupported scheme "
            f"{scheme or '(none)'!r}; "
            "only http:// and https:// webhook URLs are accepted. file://, "
            "data:, and similar are refused so a typo cannot turn --notify "
            "into a local-file read or another protocol."
        )
    if not parsed.hostname:
        raise ValueError(
            f"--notify {_errors.sanitize_url(url)!r} has no host; refusing to send to it."
        )
    return url


def validate_notify_urls(urls: Optional[Iterable[str]]) -> List[str]:
    """Validate every ``--notify`` URL up front (see :func:`validate_notify_url`).
    Returns a clean list, empty if none were given. Call this before any
    network-heavy work (the pull, the fleet run) so a bad URL is an immediate
    usage error, not a surprise after the run already finished."""
    return [validate_notify_url(u) for u in (urls or [])]


def post_notification(url: str, payload: Dict[str, Any], timeout: int = 10) -> bool:
    """POST ``payload`` as JSON to ``url`` with the repo's ``hotato/<version>``
    User-Agent (the same UA every other outbound request in this repo sends;
    see ``capture.py``/``apply.py``/``inspectcfg.py``).

    Fail-open: returns ``False`` and logs one stderr warning on ANY network or
    HTTP-level problem (DNS, connection refused, timeout, a non-2xx response,
    a body that ``json.dumps`` cannot handle) -- never raises. Returns ``True``
    on a successful delivery (any 2xx response). The URL's scheme is still
    checked first and DOES raise (see :func:`validate_notify_url`): that is a
    usage error, not a delivery failure.
    """
    validate_notify_url(url)
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"hotato/{_version()} (+https://hotato.dev)",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _errors.read_bounded_http_body(
                resp,
                max_bytes=_HTTP_NOTIFY_RESPONSE_MAX_BYTES,
                subject=(
                    "notification response from "
                    f"{_errors.sanitize_url(url)}"
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - fail-open by design; see module docstring
        sys.stderr.write(
            f"[notify] {_errors.sanitize_url(url)}: delivery failed, continuing "
            f"({_errors.sanitize_urls_in_text(exc)})\n"
        )
        return False


def notify_all(urls: Optional[Iterable[str]], payload: Dict[str, Any],
                timeout: int = 10) -> None:
    """Post ``payload`` to every URL in ``urls`` (already-validated, e.g. by
    :func:`validate_notify_urls`). Each delivery is independent and fail-open:
    one bad webhook never stops the others from being tried."""
    for url in urls or []:
        post_notification(url, payload, timeout=timeout)


def _clean_candidate(c: Dict[str, Any]) -> Dict[str, Any]:
    return {k: c[k] for k in _CANDIDATE_FIELDS if c.get(k) is not None}


def build_payload(*, kind: str, text: str, counts: Dict[str, Any],
                   top_candidates: Optional[List[Dict[str, Any]]] = None,
                   artifacts: Optional[Dict[str, str]] = None,
                   **identifiers: Any) -> Dict[str, Any]:
    """The shared envelope every notify payload uses: ``tool``/``kind``/
    ``version`` plus the caller's ``counts``, whitelist-cleaned
    ``top_candidates``, local ``artifacts`` paths, any caller/agent
    ``identifiers``, and the Slack-renderable ``text`` line. No audio, no
    credentials, no transcript text ever passes through this function -- the
    candidate whitelist in :func:`_clean_candidate` is what enforces that, not
    caller discipline."""
    payload: Dict[str, Any] = {
        "tool": "hotato",
        "kind": kind,
        "version": _version(),
        "text": text,
        "counts": dict(counts),
        "top_candidates": [_clean_candidate(c) for c in (top_candidates or [])],
        "artifacts": dict(artifacts or {}),
    }
    payload.update(identifiers)
    return payload


def sweep_payload(*, stack: str, aggregate: Dict[str, Any], out_file: Optional[str] = None,
                   pull_dir: Optional[str] = None, top: int = 5) -> Dict[str, Any]:
    """Build the ``hotato sweep --notify`` payload from the same ``aggregate``
    dict :func:`hotato.analyze.analyze_folder` returns (or its ``--format
    json`` capped copy)."""
    cands = aggregate.get("candidates") or []
    top_candidates = [
        {"id": f"{c.get('source')}#{i}", "kind": c.get("kind"), "t_sec": c.get("t_sec"),
         "durations": c.get("durations")}
        for i, c in enumerate(cands[:top])
    ]
    counts = {
        "calls_scanned": aggregate.get("calls_scanned", 0),
        "calls_skipped": aggregate.get("calls_skipped", 0),
        "candidates_found": aggregate.get("total_candidates", 0),
    }
    text = (
        f"hotato sweep {stack}: {counts['calls_scanned']} call(s) scanned, "
        f"{counts['candidates_found']} candidate moment(s)"
        + (f" -> {out_file}" if out_file else "")
    )
    artifacts: Dict[str, str] = {}
    if out_file:
        artifacts["dashboard"] = out_file
    if pull_dir:
        artifacts["pull_dir"] = pull_dir
    return build_payload(kind="sweep", text=text, counts=counts,
                         top_candidates=top_candidates, artifacts=artifacts,
                         stack=stack)


def fleet_run_payload(*, workspace_id: str, agent_id: str, res: Dict[str, Any],
                      home: Optional[str] = None, top: int = 5) -> Dict[str, Any]:
    """Build the ``hotato fleet run --notify`` payload from the ``dict``
    :meth:`hotato.fleet.api.FleetAPI.run` returns."""
    cands = (res.get("top_candidates") or [])[:top]
    top_candidates = [
        {"id": c.get("candidate_id"), "kind": c.get("cluster"),
         "onset_sec": c.get("onset_sec"), "severity": c.get("severity")}
        for c in cands
    ]
    counts = {
        "recordings_ingested": len(res.get("ingested") or []),
        "clusters": res.get("clusters", 0),
        "candidates_found": res.get("reviewed_candidates", 0),
    }
    text = (
        f"hotato fleet run {agent_id}: {counts['recordings_ingested']} "
        f"recording(s) ingested, {counts['candidates_found']} candidate "
        "moment(s) -> review with `hotato fleet review`"
    )
    artifacts = {"home": home} if home else {}
    return build_payload(kind="fleet_run", text=text, counts=counts,
                         top_candidates=top_candidates, artifacts=artifacts,
                         workspace_id=workspace_id, agent_id=agent_id)


def contract_verify_payload(v: Dict[str, Any], *, top: int = 5) -> Dict[str, Any]:
    """Build the ``hotato contract verify --notify`` run-summary payload from
    the batch proof dict :func:`hotato.contract.verify_contracts` returns.

    Carries the pass/fail counts and the top FAILING contracts' ids + measured
    timing (each result's ``measurement`` block: ``did_yield``/
    ``seconds_to_yield``/``talk_over_sec``) and nothing else -- no audio, no
    credentials, no transcript text, and no ``dir``/bundle file paths that would
    leak the local layout (this payload deliberately carries no ``artifacts``,
    unlike sweep's, for exactly that reason). The distinct ``tampered``/
    ``refused``/``assertions_failed`` counts stay SEPARATE from ``failed`` --
    reported as their own count fields, never collapsed into one blended verdict,
    exactly as ``contract verify`` itself reports them. This is a side-channel
    summary of an already-decided verify: the CLI's exit code is unchanged
    whether or not a webhook is set, and this payload is emitted on every verify
    (pass or fail) when ``--notify`` is given. The candidate whitelist in
    :func:`_clean_candidate` still enforces the share-safe shape -- the
    ``measurement`` numbers ride in the whitelisted ``durations`` field, never a
    new leaking key."""
    results = v.get("results") or []
    summary = v.get("summary") or {}
    failing = [r for r in results if not r.get("passed")]
    top_candidates = [
        {"id": r.get("id"), "kind": r.get("expect"),
         "durations": dict(r.get("measurement") or {})}
        for r in failing[:top]
    ]
    counts = {
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "tampered": v.get("tampered", 0),
        "refused": v.get("refused", 0),
        "assertions_failed": v.get("assertions_failed", 0),
    }
    text = (
        f"hotato contract verify: {counts['passed']}/{v.get('count', 0)} pass"
        + (f", {counts['failed']} failing" if counts["failed"] else "")
    )
    return build_payload(kind="contract-verify", text=text, counts=counts,
                         top_candidates=top_candidates,
                         exit_code=v.get("exit_code"))
