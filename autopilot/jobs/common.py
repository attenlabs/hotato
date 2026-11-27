"""Shared job plumbing: kill switch, run records, state store, logging contract,
GitHub credentials, Secret Manager, HTTP.

State store: GCS bucket $STATE_BUCKET, or a local dir $STATE_DIR (tests, dry runs).

Write mode (DRY_RUN env, set per job by infra):
  "1" or unset  forced shadow: no GitHub or phone writes, whatever the promotion state says
  "auto"        the promotion state in the bucket decides (jobs/promotion.py)
  "0"           forced live (owner override); demotions are still recorded and alerted
Any other value is treated as "1".

Logging contract (Cloud Logging parses one JSON object per stdout line):
  success  {"hotato_heartbeat": "<job>", "severity": "INFO"}  + heartbeats/<job>.json in the store
           (outcome "ok", or "skipped" when the job woke and did nothing)
  alert    {"hotato_alert": "<kind>", "severity": "WARNING"|"ERROR", "detail": "...", "job": "<job>"}
"""
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

KILL_KEY = "flags/kill"
HEARTBEAT_PREFIX = "heartbeats/"
# Jobs that hold GitHub credentials (mirrors local.gh_secrets in infra/main.tf).
GITHUB_JOBS = {"upkeep", "builder", "reviewer", "freshness", "scorecard"}


def now():
    return dt.datetime.now(dt.timezone.utc)


# ---- state store -------------------------------------------------------------

class Store:
    def __init__(self, bucket=None, local_dir=None):
        self.local = local_dir or os.environ.get("STATE_DIR")
        self.bucket_name = bucket or os.environ.get("STATE_BUCKET")
        self._bucket = None

    def _b(self):
        if self._bucket is None:
            from google.cloud import storage  # lazy: not needed in tests
            self._bucket = storage.Client().bucket(self.bucket_name)
        return self._bucket

    def _p(self, key):
        return os.path.join(self.local, key)

    def exists(self, key):
        if self.local:
            return os.path.exists(self._p(key))
        return self._b().blob(key).exists()

    def get_bytes(self, key):
        if self.local:
            p = self._p(key)
            return open(p, "rb").read() if os.path.exists(p) else None
        blob = self._b().blob(key)
        return blob.download_as_bytes() if blob.exists() else None

    def put_bytes(self, key, data):
        if self.local:
            p = self._p(key)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(data)
        else:
            self._b().blob(key).upload_from_string(data)

    def get_json(self, key, default=None):
        raw = self.get_bytes(key)
        return json.loads(raw) if raw else default

    def put_json(self, key, obj):
        self.put_bytes(key, json.dumps(obj, indent=2, sort_keys=True).encode())

    def update_bytes(self, key, fn, attempts=8):
        """Read-modify-write that is safe when several jobs write the same object at once
        (GCS generation precondition; a file lock in local mode).
        fn(old_bytes_or_None) -> new bytes. fn may run more than once (after a lost race)."""
        if self.local:
            import fcntl
            p = self._p(key)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p + ".lock", "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                old = open(p, "rb").read() if os.path.exists(p) else None
                new = fn(old)
                with open(p + ".tmp", "wb") as fh:
                    fh.write(new)
                os.replace(p + ".tmp", p)
                return new
        from google.api_core import exceptions as gexc
        for i in range(attempts):
            blob = self._b().blob(key)
            try:
                blob.reload()
                gen = blob.generation
                old = blob.download_as_bytes(if_generation_match=gen)
            except gexc.NotFound:
                gen, old = 0, None  # generation 0 = "only if the object does not exist yet"
            new = fn(old)
            try:
                blob.upload_from_string(new, if_generation_match=gen)
                return new
            except gexc.PreconditionFailed:
                time.sleep(min(0.2 * 2 ** i, 5))
        raise RuntimeError(f"update of {key} lost the race {attempts} times")

    def update_json(self, key, fn, default=None):
        """fn(obj) -> new obj. obj is a fresh copy of `default` when the key is absent."""
        def wrap(old):
            obj = json.loads(old) if old else json.loads(json.dumps(default))
            return json.dumps(fn(obj), indent=2, sort_keys=True).encode()
        return json.loads(self.update_bytes(key, wrap))

    def append_jsonl(self, key, rows):
        """Append-only log: one JSON object per line."""
        rows = [rows] if isinstance(rows, dict) else list(rows)
        if not rows:
            return
        add = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode()
        self.update_bytes(key, lambda old: (old or b"") + add)


# ---- logging contract ----------------------------------------------------------

_RUN = {"mode": None, "job": None}  # set by run_job for the current run


def _emit(obj):
    print(json.dumps(obj), flush=True)


_SECRET_RES = [
    re.compile(r"(x-access-token:|oauth2:|://)[^@\s/'\"]+@"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
]


def redact(text):
    """Strip credentials (URL userinfo, GitHub tokens, bearer tokens) from text bound for logs, records or alerts."""
    s = str(text)
    s = _SECRET_RES[0].sub(lambda m: m.group(1) + "***@", s)
    s = _SECRET_RES[1].sub("***", s)
    return _SECRET_RES[2].sub(lambda m: m.group(1) + "***", s)


def alert(kind, severity, detail, job=None):
    """Alertable condition. severity: WARNING or ERROR."""
    if severity not in ("WARNING", "ERROR"):
        raise ValueError(severity)
    obj = {"hotato_alert": kind, "severity": severity, "detail": redact(detail)[:1000]}
    job = job or _RUN["job"]
    if job:
        obj["job"] = job
    _emit(obj)
    return obj


def version():
    return os.environ.get("AUTOPILOT_VERSION") or "unknown"


def heartbeat(store, job, outcome="ok", mode=None):
    """`time` is the latest write; `last_ok` the latest run that did its work. A skipped run carries
    the previous file's `last_ok` forward (null when that file is missing or unreadable), so a job
    that only ever skips still looks stale to a monitor reading `last_ok`."""
    _emit({"hotato_heartbeat": job, "severity": "INFO"})
    key = f"{HEARTBEAT_PREFIX}{job}.json"
    at = now().isoformat()
    if outcome == "ok":
        last_ok = at
    else:
        try:
            prev = store.get_json(key)
            last_ok = prev.get("last_ok") if isinstance(prev, dict) else None
            if last_ok is None and isinstance(prev, dict) and "last_ok" not in prev and prev.get("outcome") == "ok":
                last_ok = prev.get("time")  # file written before last_ok existed: its ok `time` is the last good run
        except Exception:  # unreadable previous heartbeat: no known last good run
            last_ok = None
    store.put_json(key, {"time": at, "outcome": outcome, "last_ok": last_ok,
                         "version": version(), "mode": mode})


def gate_violation(rec, kind, detail):
    """A gate refused something this run tried to do. Demotes a live job and resets promotion."""
    rec.setdefault("gate_violations", []).append({"kind": kind, "detail": str(detail)[:300]})
    alert("gate_violation", "ERROR", f"{kind}: {detail}", rec.get("job"))


# ---- write mode ---------------------------------------------------------------

def env_mode():
    """'shadow' (forced), 'live' (forced) or 'auto'."""
    v = os.environ.get("DRY_RUN", "1").strip().lower()
    return {"0": "live", "auto": "auto"}.get(v, "shadow")


def effective_mode(state_mode):
    m = env_mode()
    if m == "auto":
        return "live" if state_mode == "live" else "shadow"
    return m


def dry_run():
    return effective_mode(_RUN["mode"]) != "live"


# ---- run wrapper ----------------------------------------------------------------

class ModelUnavailable(RuntimeError):
    """No configured Vertex model is available; already alerted as model_unavailable."""


def _check_github(name, rec):
    """Every run, shadow included: a GitHub credential exists and the PAT is not about to
    expire, so a credential cannot lapse unseen before (or after) promotion."""
    try:
        tok = get_github_token(required=False)
    except Exception as exc:  # never crash the job over the check itself
        tok = None
        alert("github_auth_check_failed", "WARNING", f"{type(exc).__name__}: {exc}", name)
    rec["github_auth"] = _GH["kind"] if tok else "none"
    if not tok:
        rec["promotion_block"] = "no GitHub credential"
        if not dry_run():
            alert("github_credentials_missing", "ERROR", "live job has no GitHub App or bot token", name)
        elif env_mode() == "auto":
            alert("github_credentials_missing", "WARNING",
                  "shadow job cannot be promoted without a GitHub credential", name)


def skipped(rec):
    """A run that did nothing: fn set rec["skipped"] or returned a dict with a "skipped" key."""
    res = rec.get("result")
    return bool(rec.get("skipped")) or (isinstance(res, dict) and bool(res.get("skipped")))


def run_job(name, fn, store=None):
    """Kill switch first, then run fn(store, record); always write a run record.
    An unhandled exception in fn logs hotato_alert job_crash and is re-raised."""
    import promotion  # lazy: promotion imports this module
    store = store or Store()
    started = now()
    crash = None
    _RUN.update(job=name, mode=None)
    rec = {"job": name, "started": started.isoformat()}
    try:
        killed = store.exists(KILL_KEY)
        kill_reason = (store.get_bytes(KILL_KEY) or b"").decode("utf-8", "replace")[:200] if killed else None
        try:
            _RUN["mode"] = promotion.begin_run(store, name, started, killed=killed, kill_reason=kill_reason)
        except Exception as exc:  # fail safe: shadow
            _RUN["mode"] = "shadow"
            alert("promotion_state_error", "ERROR", f"begin: {type(exc).__name__}: {exc}", name)
        rec.update(mode=effective_mode(_RUN["mode"]), dry_run=dry_run())
        if killed:
            rec.update(status="killed", detail="kill-switch flag is set")
            alert("kill_switch", "WARNING", f"job skipped: kill-switch flag is set ({kill_reason})", name)
        else:
            try:
                if name in GITHUB_JOBS:
                    _check_github(name, rec)
                rec.update(status="ok", result=fn(store, rec))
            except ModelUnavailable as exc:
                rec.update(status="error", error_kind="model_unavailable", error=redact(exc)[:500])
            except Exception as exc:  # record, alert, then fail the Cloud Run task
                rec.update(status="error", error_kind="job_crash", error=redact(f"{type(exc).__name__}: {exc}")[:500])
                alert("job_crash", "ERROR", rec["error"], name)
                crash = exc
        rec["finished"] = now().isoformat()
        store.put_json(f"runs/{name}/{started.strftime('%Y%m%dT%H%M%SZ')}.json", rec)
        try:
            promotion.end_run(store, name, rec, now())
        except Exception as exc:
            alert("promotion_state_error", "ERROR", f"end: {type(exc).__name__}: {exc}", name)
        if rec["status"] == "ok":
            # "skipped": the job woke and did nothing (store busy, nothing to do, cap reached),
            # so a monitor can tell it from a run that did its work.
            heartbeat(store, name, "skipped" if skipped(rec) else "ok", rec.get("mode"))
        _emit({k: rec[k] for k in ("job", "status")})
    except Exception as exc:
        if crash is None:  # failure outside fn (state bucket unreachable, ...)
            alert("job_crash", "ERROR", redact(f"{type(exc).__name__}: {exc}"), name)
        raise
    finally:
        _RUN.update(job=None, mode=None)
    if crash is not None:
        raise crash
    return 1 if rec["status"] == "error" else 0


def main_guard(name, fn):
    sys.exit(run_job(name, fn))


# ---- HTTP ---------------------------------------------------------------------

USER_AGENT = "hotato-autopilot"  # urllib's default "Python-urllib/x.y" gets HTTP 403 from hotato.dev


def request_headers(headers=None):
    """Caller headers plus a default User-Agent; a caller's own User-Agent (any case) wins."""
    h = dict(headers or {})
    if not any(k.lower() == "user-agent" for k in h):
        h["User-Agent"] = USER_AGENT
    return h


def http_resp(url, method="GET", headers=None, data=None, timeout=30):
    """-> (status, headers{lower-case: value}, body). Raises urllib.error.HTTPError on 4xx/5xx.
    Sends User-Agent hotato-autopilot unless the caller sets its own."""
    req = urllib.request.Request(url, method=method, headers=request_headers(headers), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()


def http(url, method="GET", headers=None, data=None, timeout=30):
    status, _, body = http_resp(url, method, headers, data, timeout)
    return status, body


def get_json(url, headers=None):
    _, body = http(url, headers=headers)
    return json.loads(body)


def sha(text):
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


# ---- Google Cloud: metadata token, Secret Manager --------------------------------

def on_gcp():
    return bool(os.environ.get("CLOUD_RUN_JOB") or os.environ.get("K_SERVICE"))


def gcp_token():
    _, _, body = http_resp(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"}, timeout=10)
    return json.loads(body)["access_token"]


def env_name(secret):
    return secret.upper().replace("-", "_")


def _secret_manager(name):
    """Latest version of a secret, or None when the secret or its versions are absent."""
    project = os.environ.get("GCP_PROJECT")
    if not (project and on_gcp()):
        return None
    url = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{name}/versions/latest:access"
    try:
        _, _, body = http_resp(url, headers={"Authorization": f"Bearer {gcp_token()}"}, timeout=15)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            alert("secret_unreadable", "WARNING", f"{name}: permission denied (grant secretAccessor to this job)")
        return None  # 404 / 400 FAILED_PRECONDITION: no secret or no enabled version
    return base64.b64decode(json.loads(body)["payload"]["data"]).decode()


def read_secret(name, fetch=None):
    """Env var (upper snake case, set by Cloud Run from Secret Manager) first, then Secret
    Manager itself. Missing or empty -> None. Values are never logged."""
    val = os.environ.get(env_name(name))
    if not val:
        val = (fetch or _secret_manager)(name)
    val = (val or "").strip()
    return val or None


# ---- GitHub credentials ----------------------------------------------------------
# Preferred: a GitHub App (secrets github-app-id, github-app-private-key,
# github-app-installation-id): a short-lived installation token is minted per run, so
# nothing expires. Fallback: the github-bot-token PAT, whose expiry is read from the
# `github-authentication-token-expiration` header and alerted at 30 and 7 days.

APP_SECRETS = ("github-app-id", "github-app-private-key", "github-app-installation-id")
PAT_SECRET = "github-bot-token"
GH_API = "https://api.github.com"
_GH = {"token": None, "exp": 0.0, "kind": None, "pat_checked": False}


def reset_github_cache():
    _GH.update(token=None, exp=0.0, kind=None, pat_checked=False)


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def app_jwt(app_id, private_key_pem, at=None):
    """RS256 JWT for GitHub App auth (valid 9 minutes, iat backdated 60 s for clock skew)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    t = int(at if at is not None else time.time())
    if "\\n" in private_key_pem and "\n" not in private_key_pem:
        private_key_pem = private_key_pem.replace("\\n", "\n")  # key pasted as one line
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iat": t - 60, "exp": t + 540, "iss": str(app_id)},
                                 separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(sig)}"


def _gh_headers(token=None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "hotato-autopilot",
         "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def mint_installation_token(app_id, pem, installation_id):
    """-> (token, expires_epoch)."""
    jwt = app_jwt(app_id, pem)
    _, _, body = http_resp(f"{GH_API}/app/installations/{installation_id}/access_tokens", "POST",
                           _gh_headers(jwt), b"{}")
    doc = json.loads(body)
    exp = dt.datetime.fromisoformat(doc["expires_at"].replace("Z", "+00:00")).timestamp()
    return doc["token"], exp


def parse_gh_expiry(value):
    """GitHub sends e.g. '2026-12-31 00:00:00 UTC' or '2026-12-31 00:00:00 -0800'."""
    value = value.strip()
    if value.endswith(" UTC"):
        value = value[:-4] + " +0000"
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            pass
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def expiry_severity(expires, at=None):
    days = (expires - (at or now())).total_seconds() / 86400
    if days < 7:
        return "ERROR"
    if days < 30:
        return "WARNING"
    return None


def check_pat_expiry(headers, at=None):
    """Alert from the github-authentication-token-expiration response header."""
    raw = (headers or {}).get("github-authentication-token-expiration")
    if not raw:
        return None  # token without an expiry date
    try:
        exp = parse_gh_expiry(raw)
    except ValueError:
        alert("github_token_expiring", "WARNING", f"unreadable expiry header {raw!r}")
        return "WARNING"
    sev = expiry_severity(exp, at)
    if sev:
        days = (exp - (at or now())).total_seconds() / 86400
        alert("github_token_expiring", sev,
              f"github-bot-token expires {exp.isoformat()} ({days:.1f} days); install the GitHub App "
              "(github-app-* secrets) or rotate the token")
    return sev


def _check_pat(pat):
    """First API call with the PAT: /rate_limit (costs no quota) to read its expiry header."""
    _GH["pat_checked"] = True
    try:
        _, headers, _ = http_resp(f"{GH_API}/rate_limit", headers=_gh_headers(pat), timeout=15)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            alert("github_token_invalid", "ERROR", "github-bot-token was rejected (401): expired or revoked")
            return
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
    check_pat_expiry(headers)


def get_github_token(required=True, fetch=None):
    """Installation token from the GitHub App when its three secrets are present (minted
    per run, re-minted when under 5 minutes are left), else the PAT. None/raise if neither."""
    if _GH["token"] and _GH["exp"] - time.time() > 300:
        return _GH["token"]
    app = [read_secret(s, fetch) for s in APP_SECRETS]
    if all(app):
        try:
            if importlib.util.find_spec("cryptography") is None:
                raise RuntimeError("the cryptography package is not installed in the job image")
            tok, exp = mint_installation_token(*app)
            _GH.update(token=tok, exp=exp, kind="app")
            return tok
        except Exception as exc:
            alert("github_app_failed", "ERROR", f"{type(exc).__name__}: {exc}; falling back to github-bot-token")
    elif any(app):
        alert("github_app_incomplete", "WARNING",
              "only some of " + ", ".join(APP_SECRETS) + " are set; using github-bot-token")
    pat = read_secret(PAT_SECRET, fetch)
    if pat:
        if not _GH["pat_checked"]:
            _check_pat(pat)
        _GH.update(token=pat, exp=float("inf"), kind="pat")
        return pat
    if required:
        raise RuntimeError("no GitHub credential: set the github-app-* secrets or github-bot-token")
    return None


def bot_token():
    return get_github_token(required=True)


def gh(path, token=None, method="GET", payload=None):
    """GitHub REST. Writes are refused in dry-run mode. Public reads need no token."""
    if method != "GET" and dry_run():
        return {"dry_run": True, "would": f"{method} {path}"}
    data = json.dumps(payload).encode() if payload is not None else None
    _, _, body = http_resp(GH_API + path, method, _gh_headers(token), data)
    return json.loads(body) if body else {}


def repo():
    return os.environ.get("HOTATO_REPO", "OWNER/hotato")


def open_issue(title, body, token=None, labels=("autopilot",)):
    """Open an issue unless an open one with the same title exists. Returns a short status."""
    if dry_run():
        return "dry-run"
    token = token or bot_token()
    q = gh(f"/repos/{repo()}/issues?state=open&labels={','.join(labels)}&per_page=100", token)
    if any(i["title"] == title for i in q):
        return "exists"
    gh(f"/repos/{repo()}/issues", token, "POST", {"title": title, "body": body, "labels": list(labels)})
    return "opened"


# ---- spend ledger ------------------------------------------------------------
# Each job debits its worst-case cost BEFORE spending; a refused debit means the
# phase cap for that category is reached. Complements the Google Cloud budgets.

def period_key(at=None):
    at = at or now()
    return "sprint" if os.environ.get("PHASE", "sprint") == "sprint" else at.strftime("%Y-%m")


def can_spend(total, amount, cap):
    return cap > 0 and total + amount <= cap


def debit(store, category, amount, cap, at=None):
    """Return True and record the debit if it fits under `cap`; else False."""
    key = f"spend/{category}.json"
    ledger = store.get_json(key, {})
    period = period_key(at)
    if not can_spend(ledger.get(period, 0.0), amount, cap):
        return False
    ledger[period] = round(ledger.get(period, 0.0) + amount, 4)
    store.put_json(key, ledger)
    return True


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)
