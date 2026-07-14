"""``hotato.state_adapter``: the post-call STATE ADAPTER (Authority 2).

The ``state`` / ``state_change`` assertion kinds (see :mod:`hotato.assert_`)
never trust the agent's spoken claim ("I issued the refund"). They query a
post-call SYSTEM OF RECORD through this tiny, pluggable interface and compare
the ACTUAL state -- so an agent that says a thing happened but did not do it
fails the assertion. There is no model/LLM path here: a query is a plain
lookup + dict comparison, deterministic and offline.

Interface -- one method:

    ``query(resource, **filters) -> dict | None``

Return the first record of ``resource`` whose fields match every ``filters``
key==value, or ``None`` when no such record exists. A reserved ``when`` filter
selects a named SNAPSHOT (``"before"`` / ``"after"``, default ``"after"``) so
``state_change`` can read a before/after delta from the same interface.

Three concrete adapters ship:

* :class:`MockStateAdapter` -- the local JSON / SQLite SANDBOX. Deterministic,
  offline, byte-stable; no network. This is a shipped OFFLINE feature (a state
  fixture you author or capture once), not a stand-in for a real system.
* :class:`HttpStateAdapter` -- queries a customer's REST system of record over
  stdlib ``urllib`` (a resource map turns ``query(resource, **filters)`` into a
  request and extracts the record via a response pointer). A NETWORK path:
  documented in ``docs/EGRESS.md`` + ``docs/THREAT-MODEL.md`` and refused by
  :func:`load_state_adapter` unless the config sets ``egress_opt_in: true``.
* :class:`SqlStateAdapter` -- queries a SQL system of record (stdlib
  ``sqlite3`` for a file/system DB, or any caller-supplied DBAPI connection /
  a ``dsn`` + driver). PARAMETERIZED-only (never string interpolation) and
  read-only (SELECT / WITH ... SELECT only); a non-local DSN is a network path.

A REAL adapter that cannot RELIABLY read the system of record (network error,
timeout, non-2xx other than a record-absent 404, a misshapen response, a DB
error) raises :class:`StateAdapterError`; the frozen ``state`` / ``state_change``
evaluators catch it and report INCONCLUSIVE -- "could not determine state" is
absent input, never a fabricated PASS/FAIL. A record the system of record can
be read and genuinely does NOT hold returns ``None`` (a grounded FAIL upstream).

The post-call query runs AFTER scoring and is folded into the conversation
artifact's evaluations, never into the timing verdict.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import errors as _errors
from .errors import open_regular as _open_regular


_HTTP_STATE_RESPONSE_MAX_BYTES = 8 * 1024 * 1024

__all__ = [
    "StateAdapter",
    "MockStateAdapter",
    "HttpStateAdapter",
    "SqlStateAdapter",
    "StateAdapterError",
    "load_state_adapter",
]


class StateAdapterError(RuntimeError):
    """A REAL adapter (HTTP / SQL) could not RELIABLY read the system of record
    for a query -- a network error, a timeout, a non-2xx status other than a
    record-absent 404, a non-JSON / misshapen response, a missing credential,
    or a driver/DB error. The frozen ``state`` / ``state_change`` evaluators
    (see :mod:`hotato.assert_`) already ``except Exception`` around
    :meth:`StateAdapter.query` and turn a raised error into an INCONCLUSIVE
    result with the reason attached -- so this type never crashes a run and
    never yields a fabricated verdict: "could not determine state" is treated
    as absent input, distinct from "the record genuinely is not there" (which
    a query answers with ``None`` -> a grounded FAIL).

    The structured cause is on :attr:`detail` and is also mirrored onto the
    adapter's ``last_error`` so a direct caller can inspect it without parsing
    the message. Credential values are NEVER placed in either."""

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.detail: Dict[str, Any] = detail or {}


class StateAdapter:
    """The pluggable post-call state interface. A concrete adapter implements
    :meth:`query`; the ``state``/``state_change`` kinds depend only on this
    contract, so a customer adapter (HTTP/DB) drops in without touching the
    assertion engine."""

    def query(self, resource: str, **filters: Any) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class MockStateAdapter(StateAdapter):
    """A deterministic, offline state adapter backed by an in-memory sandbox.

    The sandbox is a mapping ``{resource: rows}`` where ``rows`` is either:

    * a list of record dicts (a single, post-call snapshot), or
    * a single record dict (sugar for a one-row list), or
    * a snapshotted mapping ``{"before": [...], "after": [...]}`` -- the shape
      ``state_change`` reads for a delta (``query(..., when="before")`` /
      ``query(..., when="after")``). A ``when`` for which no snapshot exists
      returns ``None`` (the assertion reports INCONCLUSIVE, never a guess).

    ``query`` pops the reserved ``when`` key, resolves the resource's rows for
    that snapshot, then returns the first row matching every remaining filter,
    or ``None``. A returned dict is a shallow COPY, so an assertion can never
    mutate the sandbox.
    """

    def __init__(self, data: Dict[str, Any]):
        if not isinstance(data, dict):
            raise ValueError(
                "MockStateAdapter data must be a mapping of {resource: rows}"
            )
        self._data = data

    @classmethod
    def from_json_file(cls, path: str) -> "MockStateAdapter":
        """Load a sandbox from a JSON file (``{resource: rows}``). A FIFO/named
        pipe path raises immediately (via :func:`hotato.errors.open_regular`)
        instead of blocking forever; malformed JSON raises ``ValueError``."""
        with _open_regular(path, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path!r} is not a valid state sandbox JSON: {exc}") from exc
        return cls(data)

    @classmethod
    def from_sqlite_file(cls, path: str) -> "MockStateAdapter":
        """Build a sandbox from a local SQLite file: each table becomes a
        resource whose rows are its records. A table named ``<name>__before`` /
        ``<name>__after`` is folded into a snapshotted ``<name>`` resource
        (the before/after shape ``state_change`` reads). Read-only; the DB is
        never written."""
        import sqlite3

        data: Dict[str, Any] = {}
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for t in tables:
                # Table names come from the DB's own catalogue; quote defensively.
                rows = [
                    dict(r)
                    for r in conn.execute('SELECT * FROM "%s"' % t.replace('"', '""'))
                ]
                if t.endswith("__before"):
                    data.setdefault(t[: -len("__before")], {})["before"] = rows
                elif t.endswith("__after"):
                    data.setdefault(t[: -len("__after")], {})["after"] = rows
                else:
                    data[t] = rows
        finally:
            conn.close()
        return cls(data)

    def _rows(self, resource: str, when: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        val = self._data.get(resource)
        if val is None:
            return None
        if isinstance(val, dict) and ("before" in val or "after" in val):
            rows = val.get(when or "after")
            if rows is None:
                return None
            return rows if isinstance(rows, list) else [rows]
        if isinstance(val, dict):
            return [val]
        if isinstance(val, list):
            return val
        return None

    def query(self, resource: str, **filters: Any) -> Optional[Dict[str, Any]]:
        when = filters.pop("when", None)
        rows = self._rows(resource, when)
        if rows is None:
            return None
        for row in rows:
            if isinstance(row, dict) and all(
                k in row and row[k] == v for k, v in filters.items()
            ):
                return dict(row)
        return None


# =========================================================================
# Shared helpers for the REAL adapters
# =========================================================================

_ENV_NAME_KEYS = ("token_env", "username_env", "password_env", "env")


def _require_env(name: str, purpose: str) -> str:
    """Read a credential from the named environment variable, or raise a clear
    ValueError (the CLI's exit-2 usage-error path). Adapters take env-var
    NAMES, never inline secrets, so a config file can be committed / shared
    without a credential ever living in it; the secret is supplied at run time
    via the environment (or a ``0600`` file that exports it)."""
    val = os.environ.get(name)
    if not val:
        raise ValueError(
            f"{purpose} reads its credential from the environment variable "
            f"{name!r}, which is not set (or empty). Export it (e.g. from a "
            f"0600 file) before running; hotato never takes an inline secret "
            f"in a state-config file."
        )
    return val


def _resolve_pointer(body: Any, pointer: Optional[str]) -> Tuple[str, Any]:
    """Extract the record OBJECT a query names out of a decoded JSON response.

    ``pointer`` is a JSON-pointer-ish path: ``/``-separated segments (a leading
    ``/`` is optional and ``.`` is also accepted as a separator), each a dict
    key or -- when all-digits -- a list index. An empty / ``None`` pointer
    means "the whole response body is the record".

    Returns ``(status, value)`` where ``status`` is:

    * ``"found"``      -- the pointer landed on a record object (``value`` is
      the dict);
    * ``"absent"``     -- the response was well-formed but the pointer names
      nothing that exists (a missing key, an out-of-range index, an explicit
      ``null``) -> a genuinely-absent record (``None`` -> grounded FAIL);
    * ``"malformed"``  -- the pointer disagrees with the response shape
      (descending into a scalar, a non-digit index into a list, or landing on
      a non-object) -> we cannot read the record -> INCONCLUSIVE.
    """
    if not pointer:
        segments: List[str] = []
    else:
        norm = pointer.strip().lstrip("/")
        sep = "/" if "/" in norm else "."
        segments = [s for s in norm.split(sep) if s != ""]
    cur = body
    for seg in segments:
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
            else:
                return "absent", None
        elif isinstance(cur, list):
            if seg.isdigit():
                idx = int(seg)
                if idx < len(cur):
                    cur = cur[idx]
                else:
                    return "absent", None
            else:
                return "malformed", None  # a dict-key segment into a list
        else:
            return "malformed", None       # cannot descend into a scalar / None
    if cur is None:
        return "absent", None
    if isinstance(cur, dict):
        return "found", cur
    return "malformed", None                # landed on a non-object (list/scalar)


def _validate_resource_map(resources: Any, required: Tuple[str, ...]) -> Dict[str, Dict[str, Any]]:
    """A resource map is ``{resource_name: {spec...}}``; each spec must carry
    every ``required`` key. Shared shape-check for the HTTP and SQL maps
    (each then validates its own field types)."""
    if not isinstance(resources, dict) or not resources:
        raise ValueError(
            "a state adapter needs a non-empty 'resources' map "
            "{resource: {...}}; got " + type(resources).__name__
        )
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in resources.items():
        if not isinstance(spec, dict):
            raise ValueError(f"resource {name!r}: its mapping must be an object")
        for key in required:
            if key not in spec:
                raise ValueError(
                    f"resource {name!r}: missing required key {key!r} "
                    f"(need all of {list(required)})"
                )
        out[str(name)] = spec
    return out


# =========================================================================
# HttpStateAdapter -- a REST system of record over stdlib urllib
# =========================================================================

_HTTP_METHODS = ("GET", "POST")


class HttpStateAdapter(StateAdapter):
    """Query a customer's REST system of record for the post-call state.

    A RESOURCE MAP turns a ``query`` into one request::

        resources = {
          "appointment": {
            "path_template": "/patients/{patient_id}/appointment",
            "method": "GET",                       # GET (default) or POST
            "params_map": {"status": "appt_status"},  # filter -> wire name
            "response_pointer": "data/appointment",   # where the record sits
          },
        }

    ``query("appointment", patient_id="P1", status="booked")`` fills the path
    template from the filters (each value URL-encoded), sends the remaining
    filters as query params (GET) or a JSON body (POST) under their mapped wire
    names, then extracts the record dict at ``response_pointer`` from the JSON
    response (see :func:`_resolve_pointer`).

    Egress: only the mapped filter VALUES leave the machine (in the URL / body);
    never audio, never a transcript, never the adapter's own config. HTTPS is
    required by default; a plain ``http://`` base URL is refused unless
    ``allow_http=True`` (a state query would otherwise send filter values and
    the auth header in cleartext). Credential-safe redirects are reused from
    :mod:`hotato.capture` so a 3xx can never carry the auth header to another
    host.

    Errors: a network failure / timeout / non-2xx (other than a record-absent
    404) / non-JSON body / unreadable pointer raises :class:`StateAdapterError`
    (-> INCONCLUSIVE) and records the structured cause on :attr:`last_error`.
    A reachable system of record that simply has no such record returns
    ``None`` (-> grounded FAIL). The reserved ``when="before"`` snapshot has no
    meaning for a live API (there is no pre-call snapshot to read), so it
    returns ``None`` -> ``state_change`` is honestly INCONCLUSIVE.
    """

    def __init__(
        self,
        *,
        base_url: str,
        resources: Dict[str, Any],
        auth: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
        allow_http: bool = False,
    ):
        from urllib.parse import urlparse

        if not isinstance(base_url, str) or not base_url:
            raise ValueError("HttpStateAdapter needs a non-empty base_url")
        parsed = urlparse(base_url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"HttpStateAdapter base_url {base_url!r} uses the unsupported "
                f"scheme {scheme or '(none)'!r}; only http:// and https:// are "
                "accepted."
            )
        if not parsed.hostname:
            raise ValueError(f"HttpStateAdapter base_url {base_url!r} has no host")
        if scheme == "http" and not allow_http:
            raise ValueError(
                f"HttpStateAdapter refuses the plain-http base_url {base_url!r}: "
                "a state query would send your mapped filter values and the auth "
                "header in cleartext. Use https://, or pass allow_http=True to "
                "override for a trusted local endpoint."
            )
        self._base_url = base_url.rstrip("/")
        try:
            self._timeout = float(timeout)
        except (TypeError, ValueError):
            raise ValueError(f"HttpStateAdapter timeout must be a number, got {timeout!r}")
        self._resources = self._validate_http_resources(resources)
        # Resolve credentials to headers ONCE, up front, so a missing env var
        # fails fast (exit 2) rather than mid-run; values are held only in
        # memory and never logged or placed in last_error.
        self._auth_headers = self._build_auth_headers(auth or {})
        self.last_error: Optional[Dict[str, Any]] = None

    # -- construction-time validation ---------------------------------------

    @staticmethod
    def _validate_http_resources(resources: Any) -> Dict[str, Dict[str, Any]]:
        out = _validate_resource_map(resources, ("path_template",))
        for name, spec in out.items():
            if not isinstance(spec["path_template"], str):
                raise ValueError(f"resource {name!r}: 'path_template' must be a string")
            method = str(spec.get("method", "GET")).upper()
            if method not in _HTTP_METHODS:
                raise ValueError(
                    f"resource {name!r}: 'method' must be one of {_HTTP_METHODS}, "
                    f"got {spec.get('method')!r}"
                )
            pmap = spec.get("params_map") or {}
            if not isinstance(pmap, dict):
                raise ValueError(f"resource {name!r}: 'params_map' must be an object")
            ptr = spec.get("response_pointer")
            if ptr is not None and not isinstance(ptr, str):
                raise ValueError(f"resource {name!r}: 'response_pointer' must be a string")
        return out

    @staticmethod
    def _build_auth_headers(auth: Dict[str, Any]) -> Dict[str, str]:
        if not auth:
            return {}
        atype = str(auth.get("type", "none")).lower()
        if atype == "none":
            return {}
        if atype == "bearer":
            name = auth.get("token_env")
            if not name:
                raise ValueError("bearer auth needs 'token_env' (an env-var NAME)")
            return {"Authorization": "Bearer " + _require_env(name, "HTTP bearer auth")}
        if atype == "basic":
            import base64

            user = auth.get("username")
            if not user and auth.get("username_env"):
                user = _require_env(auth["username_env"], "HTTP basic auth (username)")
            if not user:
                raise ValueError("basic auth needs 'username' or 'username_env'")
            pw_name = auth.get("password_env")
            if not pw_name:
                raise ValueError("basic auth needs 'password_env' (an env-var NAME)")
            pw = _require_env(pw_name, "HTTP basic auth (password)")
            token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
            return {"Authorization": "Basic " + token}
        if atype == "header":
            spec = auth.get("headers")
            if not isinstance(spec, dict) or not spec:
                raise ValueError("header auth needs a non-empty 'headers' object")
            out: Dict[str, str] = {}
            for hname, hval in spec.items():
                if isinstance(hval, dict) and "env" in hval:
                    out[str(hname)] = _require_env(hval["env"], f"HTTP header {hname!r}")
                elif isinstance(hval, dict) and "value" in hval:
                    out[str(hname)] = str(hval["value"])  # a non-secret literal
                else:
                    raise ValueError(
                        f"header auth {hname!r}: each header value must be "
                        "{'env': NAME} (a secret from the environment) or "
                        "{'value': LITERAL} (a non-secret constant)"
                    )
            return out
        raise ValueError(
            f"unsupported auth type {atype!r}; use 'bearer', 'basic', 'header', or 'none'"
        )

    # -- request building ----------------------------------------------------

    def _build_request(
        self, resource: str, spec: Dict[str, Any], filters: Dict[str, Any]
    ) -> Tuple[str, Optional[bytes], str]:
        from urllib.parse import quote, urlencode

        used: set = set()

        def _fill(m: "re.Match") -> str:
            key = m.group(1)
            if key not in filters:
                raise StateAdapterError(
                    f"path_template for {resource!r} needs a filter {key!r} that "
                    "the assertion did not supply",
                    {"kind": "config", "resource": resource},
                )
            used.add(key)
            return quote(str(filters[key]), safe="")

        path = re.sub(r"\{([A-Za-z0-9_]+)\}", _fill, spec["path_template"])
        remaining = {k: v for k, v in filters.items() if k not in used}
        pmap = spec.get("params_map") or {}
        wire = {str(pmap.get(k, k)): v for k, v in remaining.items()}
        method = str(spec.get("method", "GET")).upper()
        url = self._base_url + (path if path.startswith("/") else "/" + path)
        body: Optional[bytes] = None
        if method == "POST":
            body = json.dumps(wire).encode("utf-8")
        elif wire:
            url = url + "?" + urlencode({k: str(v) for k, v in wire.items()})
        return url, body, method

    # -- the query -----------------------------------------------------------

    def query(self, resource: str, **filters: Any) -> Optional[Dict[str, Any]]:
        import http.client
        import socket
        import urllib.error
        import urllib.request

        self.last_error = None
        when = filters.pop("when", None)
        if when == "before":
            # A live API exposes only "now"; there is no pre-call snapshot to
            # read. Reporting the 'before' snapshot as absent makes a
            # state_change honestly INCONCLUSIVE rather than fabricating a
            # "no change" from the same current row twice.
            return None
        spec = self._resources.get(resource)
        if spec is None:
            raise StateAdapterError(
                f"no HTTP resource mapping for {resource!r}; add it to the "
                "adapter's 'resources' map",
                {"kind": "config", "resource": resource},
            )

        url, body, method = self._build_request(resource, spec, filters)

        # Credential-safe redirects (an auth header can never follow a 3xx to a
        # different host); reused from capture.py, best-effort.
        try:
            from .capture import _ensure_safe_opener

            _ensure_safe_opener()
        except Exception:  # pragma: no cover - the request still runs without it
            pass

        headers = dict(self._auth_headers)
        headers.setdefault("Accept", "application/json")
        headers.setdefault("User-Agent", f"hotato/{_http_version()} (+https://hotato.dev)")
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        safe_url = _errors.sanitize_url(url)

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec - user-configured system of record
                raw = _errors.read_bounded_http_body(
                    resp,
                    max_bytes=_HTTP_STATE_RESPONSE_MAX_BYTES,
                    subject=f"state response from {safe_url}",
                )
        except _errors.HttpResponseTooLarge as exc:
            detail = {
                "kind": "bad_response", "resource": resource, "url": safe_url,
                "message": str(exc),
            }
            self.last_error = detail
            raise StateAdapterError(detail["message"], detail) from exc
        except urllib.error.HTTPError as exc:
            detail = {
                "kind": "http_status", "status": exc.code, "resource": resource,
                "url": safe_url,
                "message": (
                    f"HTTP {exc.code} from {safe_url}: "
                    f"{_errors.sanitize_urls_in_text(exc.reason)}"
                ),
            }
            self.last_error = detail
            if exc.code == 404:
                # 404 = the addressed record does not exist in the system of
                # record -> a grounded 'absent' (FAIL upstream), not a failure
                # to reach it. Every other non-2xx (401/403/5xx/...) means we
                # could NOT determine the state -> INCONCLUSIVE.
                return None
            raise StateAdapterError(detail["message"], detail) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            is_timeout = isinstance(reason, (socket.timeout, TimeoutError))
            detail = {
                "kind": "timeout" if is_timeout else "network",
                "resource": resource, "url": safe_url,
                "message": f"{'timeout' if is_timeout else 'network error'} "
                           f"reaching {safe_url}: "
                           f"{_errors.sanitize_urls_in_text(reason)}",
            }
            self.last_error = detail
            raise StateAdapterError(detail["message"], detail) from exc
        except (socket.timeout, TimeoutError) as exc:
            detail = {"kind": "timeout", "resource": resource, "url": safe_url,
                      "message": f"timeout reaching {safe_url}: "
                                 f"{_errors.sanitize_urls_in_text(exc)}"}
            self.last_error = detail
            raise StateAdapterError(detail["message"], detail) from exc
        except (ConnectionError, http.client.HTTPException, OSError) as exc:
            detail = {"kind": "network", "resource": resource, "url": safe_url,
                      "message": f"network error reaching {safe_url}: "
                                 f"{_errors.sanitize_urls_in_text(exc)}"}
            self.last_error = detail
            raise StateAdapterError(detail["message"], detail) from exc

        try:
            decoded = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            detail = {"kind": "bad_response", "resource": resource, "url": safe_url,
                      "message": f"response from {safe_url} was not JSON: {exc}"}
            self.last_error = detail
            raise StateAdapterError(detail["message"], detail) from exc

        status, value = _resolve_pointer(decoded, spec.get("response_pointer"))
        if status == "found":
            return value
        if status == "absent":
            return None  # reachable + parsed, but no such record -> grounded FAIL
        detail = {
            "kind": "pointer", "resource": resource, "url": safe_url,
            "message": (
                f"response_pointer {spec.get('response_pointer')!r} did not "
                f"resolve to a record object in the response from {safe_url}"
            ),
        }
        self.last_error = detail
        raise StateAdapterError(detail["message"], detail)


def _http_version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "0"


# =========================================================================
# SqlStateAdapter -- a SQL system of record, parameterized + read-only
# =========================================================================

# A mapped query must READ ONLY. The leading keyword (after comments/whitespace
# and an optional opening paren) must be SELECT or a WITH ... SELECT CTE.
_SELECT_LEAD_RE = re.compile(
    r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*\(?\s*(SELECT|WITH)\b",
    re.IGNORECASE | re.DOTALL,
)


def _strip_sql_string_literals(sql: str) -> str:
    """Blank out ``'...'`` and ``"..."`` literal contents so a ``;`` or a
    keyword INSIDE a literal is not mistaken for statement structure. Used only
    by the read-only / single-statement guards; the value binding never touches
    the SQL text at all (it is parameterized)."""
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", sql)


def _require_read_only_select(sql: str, resource: str) -> None:
    """Reject any mapped query that is not a single read-only SELECT.

    This is a DISCIPLINE check on the operator-authored resource map, not the
    injection defense (that is the parameterized binding in :meth:`query`,
    which never interpolates a filter value into the SQL). Rejects: a leading
    keyword other than SELECT / WITH; a second statement after a ``;``; and any
    data-modifying keyword appearing as a standalone token."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError(f"resource {resource!r}: 'query' must be a non-empty SQL string")
    if not _SELECT_LEAD_RE.match(sql):
        raise ValueError(
            f"SqlStateAdapter is read-only: the mapped query for {resource!r} "
            f"must begin with SELECT (or a WITH ... SELECT CTE); got "
            f"{sql.strip()[:60]!r}"
        )
    scrubbed = _strip_sql_string_literals(sql).strip().rstrip(";").rstrip()
    if ";" in scrubbed:
        raise ValueError(
            f"SqlStateAdapter refuses a multi-statement query for {resource!r}: "
            "a ';' separates a second statement. One SELECT only."
        )
    forbidden = re.search(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|MERGE|"
        r"ATTACH|DETACH|PRAGMA|VACUUM|GRANT|REVOKE|COMMIT|BEGIN)\b",
        scrubbed,
        re.IGNORECASE,
    )
    if forbidden:
        raise ValueError(
            f"SqlStateAdapter is read-only: the mapped query for {resource!r} "
            f"contains the forbidden keyword {forbidden.group(1).upper()!r}."
        )


class SqlStateAdapter(StateAdapter):
    """Query a SQL system of record for the post-call state.

    A RESOURCE MAP gives, per resource, a PARAMETERIZED SELECT and the order
    its filter values bind in::

        resources = {
          "refund": {
            "query": "SELECT id, status, amount FROM refunds "
                     "WHERE order_id = ? AND status = ?",
            "params_order": ["order_id", "status"],
          },
        }

    ``query("refund", order_id="O1", status="issued")`` binds
    ``[filters["order_id"], filters["status"]]`` as the statement's parameters
    -- the driver substitutes them as DATA, so a filter value can never change
    the SQL (SQL-injection safe by construction). The first matching row is
    returned as a ``{column: value}`` dict, or ``None`` when no row matches (a
    grounded FAIL upstream). Every mapped query is validated read-only (SELECT /
    WITH ... SELECT only) at construction AND re-checked before each execute.

    Connection source (exactly one): ``sqlite_path`` (a local file DB via stdlib
    ``sqlite3`` -- fully local, no egress), a caller-supplied DBAPI
    ``connection`` object (any driver the caller opened), or a ``dsn`` + a
    ``driver`` module name to import and ``connect`` (e.g. ``psycopg2`` -- a
    network DB, so :func:`load_state_adapter` requires ``egress_opt_in`` for it,
    and the driver is imported lazily so it is never a hard dependency). Use the
    placeholder style your driver expects in the mapped SQL (``?`` for sqlite3,
    ``%s`` for psycopg2); the adapter only passes the bound sequence through.

    A DB / driver error withholds the verdict (raises :class:`StateAdapterError`
    -> INCONCLUSIVE), never a guess. ``when="before"`` returns ``None`` (a
    point-in-time DB read has no pre-call snapshot; use the mock sandbox for a
    before/after delta)."""

    def __init__(
        self,
        *,
        resources: Dict[str, Any],
        sqlite_path: Optional[str] = None,
        connection: Any = None,
        dsn: Optional[str] = None,
        driver: Optional[str] = None,
    ):
        sources = [sqlite_path is not None, connection is not None, dsn is not None]
        if sum(sources) != 1:
            raise ValueError(
                "SqlStateAdapter needs exactly one connection source: "
                "sqlite_path (local file), connection (a DBAPI object), or "
                "dsn (+ driver)."
            )
        self._resources = self._validate_sql_resources(resources)
        self.last_error: Optional[Dict[str, Any]] = None
        self._owns_conn = False

        if sqlite_path is not None:
            import sqlite3

            from .errors import require_regular_file

            require_regular_file(sqlite_path)  # never block on a FIFO
            self._conn = sqlite3.connect(sqlite_path)
            self._conn.row_factory = sqlite3.Row
            self._owns_conn = True
        elif connection is not None:
            self._conn = connection  # caller owns its lifecycle
        else:
            if not driver:
                raise ValueError(
                    "SqlStateAdapter with a dsn needs 'driver' -- the DBAPI "
                    "module name to import (e.g. 'psycopg2')."
                )
            import importlib

            try:
                mod = importlib.import_module(driver)
            except ImportError as exc:
                raise ValueError(
                    f"SQL driver {driver!r} is not installed; `pip install "
                    f"{driver}` (hotato adds no hard database dependency)."
                ) from exc
            self._conn = mod.connect(dsn)
            self._owns_conn = True

    @staticmethod
    def _validate_sql_resources(resources: Any) -> Dict[str, Dict[str, Any]]:
        out = _validate_resource_map(resources, ("query", "params_order"))
        for name, spec in out.items():
            _require_read_only_select(spec["query"], name)
            order = spec["params_order"]
            if not isinstance(order, list) or not all(isinstance(k, str) for k in order):
                raise ValueError(
                    f"resource {name!r}: 'params_order' must be a list of filter names"
                )
        return out

    def query(self, resource: str, **filters: Any) -> Optional[Dict[str, Any]]:
        self.last_error = None
        when = filters.pop("when", None)
        if when == "before":
            return None  # point-in-time DB read; no pre-call snapshot
        spec = self._resources.get(resource)
        if spec is None:
            raise StateAdapterError(
                f"no SQL resource mapping for {resource!r}; add it to the "
                "adapter's 'resources' map",
                {"kind": "config", "resource": resource},
            )
        sql = spec["query"]
        _require_read_only_select(sql, resource)  # re-check before every execute
        order = spec["params_order"]
        try:
            params = [filters[name] for name in order]
        except KeyError as exc:
            missing = exc.args[0]
            raise StateAdapterError(
                f"SQL query for {resource!r} needs a filter {missing!r} (named "
                "in params_order) that the assertion did not supply",
                {"kind": "config", "resource": resource},
            ) from exc

        cur = None
        try:
            cur = self._conn.cursor()
            # PARAMETERIZED: values are bound by the driver, NEVER interpolated
            # into the SQL text -- the SQL-injection defense.
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return {c: row[i] for i, c in enumerate(cols)}
        except StateAdapterError:
            raise
        except Exception as exc:  # a DB/driver error withholds the verdict
            detail = {"kind": "sql_error", "resource": resource, "message": str(exc)}
            self.last_error = detail
            raise StateAdapterError(
                f"SQL query for {resource!r} failed: {exc}", detail
            ) from exc
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # pragma: no cover
                    pass

    def close(self) -> None:
        """Close a connection this adapter OPENED (sqlite_path / dsn). A
        caller-supplied ``connection`` is left alone (the caller owns it)."""
        if self._owns_conn:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass


# =========================================================================
# load_state_adapter -- the single state-config seam (mock | http | sql)
# =========================================================================


def _read_state_config(path: str) -> Any:
    """Read a state file as JSON (fast path) or the small YAML subset the rest
    of hotato already parses (no PyYAML dependency). FIFO-guarded via
    :func:`hotato.errors.open_regular`."""
    with _open_regular(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path!r} is not valid JSON: {exc}") from exc
    from .assert_ import parse_assertions_yaml

    return parse_assertions_yaml(text)


def _resolve_rel(base_dir: str, p: str) -> str:
    """Resolve a path named in a config relative to the CONFIG file's directory
    (so a config is portable), leaving an absolute path untouched."""
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def load_state_adapter(path: str) -> StateAdapter:
    """Build the :class:`StateAdapter` a ``--state FILE`` names.

    ``FILE`` is one of:

    * a ``.db`` / ``.sqlite`` / ``.sqlite3`` file -> a local mock SQLite
      SANDBOX (offline, no opt-in);
    * a JSON / YAML STATE-CONFIG object with an ``"adapter"`` key
      (``"mock"`` | ``"http"`` | ``"sql"``) selecting the adapter; or
    * a bare ``{resource: rows}`` JSON mapping (no ``"adapter"`` key) -> the
      mock sandbox, so an existing raw ``--state`` sandbox keeps working.

    EGRESS: the ``http`` adapter, and a ``sql`` adapter over a ``dsn`` (a
    non-local database), are NETWORK paths. They are REFUSED (``ValueError`` ->
    the CLI's exit-2 usage-error path) unless the config sets
    ``"egress_opt_in": true`` -- an explicit, per-config opt-in, exactly like
    ``--egress-opt-in`` gates the hosted diarizer and ``--notify``. A local
    sqlite path and the mock sandbox need no opt-in (they open no socket)."""
    low = path.lower()
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return MockStateAdapter.from_sqlite_file(path)

    doc = _read_state_config(path)
    if not isinstance(doc, dict):
        raise ValueError(f"{path!r}: a state file must be a JSON/YAML object")
    adapter = doc.get("adapter")
    if adapter is None:
        return MockStateAdapter(doc)  # legacy bare {resource: rows} sandbox

    base_dir = os.path.dirname(os.path.abspath(path))
    return _build_adapter_from_config(str(adapter).lower(), doc, base_dir)


def _build_adapter_from_config(
    adapter: str, doc: Dict[str, Any], base_dir: str
) -> StateAdapter:
    if adapter == "mock":
        if "json_file" in doc:
            return MockStateAdapter.from_json_file(_resolve_rel(base_dir, doc["json_file"]))
        if "sqlite_file" in doc:
            return MockStateAdapter.from_sqlite_file(_resolve_rel(base_dir, doc["sqlite_file"]))
        if "data" in doc:
            return MockStateAdapter(doc["data"])
        raise ValueError(
            'mock state config needs one of "json_file", "sqlite_file", or "data"'
        )

    if adapter == "http":
        if doc.get("egress_opt_in") is not True:
            raise ValueError(
                "this state config selects the HTTP adapter, which reaches a "
                "network endpoint (an EGRESS path). Set \"egress_opt_in\": true "
                "in the config to allow it; hotato refuses a network state query "
                "without an explicit opt-in."
            )
        return HttpStateAdapter(
            base_url=doc.get("base_url"),
            resources=doc.get("resources"),
            auth=doc.get("auth"),
            timeout=doc.get("timeout", 30.0),
            allow_http=bool(doc.get("allow_http", False)),
        )

    if adapter == "sql":
        resources = doc.get("resources")
        if doc.get("dsn"):
            if doc.get("egress_opt_in") is not True:
                raise ValueError(
                    "this state config selects a SQL adapter over a dsn (a "
                    "non-local database, an EGRESS path). Set "
                    "\"egress_opt_in\": true in the config to allow it."
                )
            return SqlStateAdapter(
                dsn=doc["dsn"], driver=doc.get("driver"), resources=resources
            )
        if "sqlite_path" in doc:
            return SqlStateAdapter(
                sqlite_path=_resolve_rel(base_dir, doc["sqlite_path"]),
                resources=resources,
            )
        raise ValueError(
            'sql state config needs "sqlite_path" (a local DB file) or "dsn" + '
            '"driver" (a remote DB, which additionally needs "egress_opt_in": true)'
        )

    raise ValueError(
        f"unknown state adapter {adapter!r}; expected \"mock\", \"http\", or \"sql\""
    )
