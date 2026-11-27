"""Which version of the private Atlas collector (hotato-atlas) a run uses, and how a new version
reaches production with no person and no image rebuild.

  atlas/collector.json  {"current": "0.2.0", "candidate": "0.2.1" | null, "candidate_runs": 0,
                         "bad": ["0.1.9", ...], "bad_why": {"0.1.9": "..."},
                         "offered": {"0.2.1": "<commit>"}, "reoffers": {"0.1.9": 1}}

Order of preference: the candidate (a version atlas-wheel built, tested and uploaded), then the
current version, then the version baked into the image. A version that is not baked is installed
at run time from the private Python repository into a scratch directory (ADC through the Artifact
Registry keyring, so no credential is ever on a command line or on disk). Only the hourly job
tries the candidate; recheck, audit and owner commands use current or baked.

Settling, on the hourly run's outcome (atlas_job.verdict):
  clean         the run published, or found the live site already current (or, in shadow, would
                have published): the candidate becomes current.
  fault         the candidate itself failed: the collector raised, exited non-zero, or produced a
                render the guard refused. It is marked bad, the candidate is cleared, an alert is
                logged, and the next run uses current.
  inconclusive  the run ended for a reason that says nothing about the candidate (a transient
                Vertex, GCS, Cloudflare or GitHub error, a write conflict, a missing site
                credential): nothing changes and the next run tries the candidate again.
Every run that tries the candidate is counted BEFORE it starts (candidate_runs). A run that is
killed (out of memory, the Cloud Run timeout, SIGKILL) never settles, so after
ATLAS_CANDIDATE_MAX_RUNS unsettled attempts the next run rolls the candidate back on its own.
An install failure is inconclusive (the index may lag an upload); a version that imports as
another version is a fault.

A bad version comes back only through atlas-wheel: when main still carries that version at the
commit that was offered and its tests and smoke run pass again, it is re-offered (at most
ATLAS_CANDIDATE_MAX_REOFFERS times per version). Uploaded versions are never replaced.
"""
import importlib
import importlib.metadata
import os
import re
import subprocess
import sys
import tempfile

from common import alert

KEY = "atlas/collector.json"
PACKAGE = "hotato-atlas"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def baked():
    try:
        return importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def index_url():
    region = os.environ.get("GCP_REGION", "us-central1")
    repo = os.environ.get("ATLAS_PY_REPO", "hotato-private-py")
    return f"https://{region}-python.pkg.dev/{os.environ.get('GCP_PROJECT', '')}/{repo}/simple/"


def choices(doc, baked_version):
    """Versions to try in order, without repeats or bad ones. -> [(version, role)]."""
    out, seen = [], set()
    bad = set(doc.get("bad") or [])
    for v, role in ((doc.get("candidate"), "candidate"), (doc.get("current"), "current"),
                    (baked_version, "baked")):
        if v and VERSION_RE.fullmatch(v) and v not in bad and v not in seen:
            out.append((v, role))
            seen.add(v)
    return out


def pip_install(version, target, run=subprocess.run):
    cp = run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "--no-deps",
              "--only-binary=:all:", "--keyring-provider", "import", "--index-url", index_url(),
              "--target", target, f"{PACKAGE}=={version}"],
             capture_output=True, text=True, timeout=300)
    if cp.returncode:
        raise RuntimeError(f"pip install {PACKAGE}=={version} failed: {cp.stderr[-300:]}")


def activate(target):
    """Make `target` win over the baked package for every later `import atlas`."""
    sys.path.insert(0, target)
    for name in [m for m in sys.modules if m == "atlas" or m.startswith("atlas.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def loaded_version():
    import atlas
    return getattr(atlas, "__version__", None)


def max_runs():
    return max(1, int(float(os.environ.get("ATLAS_CANDIDATE_MAX_RUNS", "3"))))


def max_reoffers():
    return max(0, int(float(os.environ.get("ATLAS_CANDIDATE_MAX_REOFFERS", "1"))))


def begin_attempt(store, version, job=None):
    """Count a run of the candidate before it starts. -> False when it already used up its
    attempts without settling (it is rolled back here), else True."""
    cap = max_runs()
    seen = {}

    def fn(doc):
        seen.clear()
        if doc.get("candidate") != version:
            seen["gone"] = True
            return doc
        runs = int(doc.get("candidate_runs") or 0)
        seen["runs"] = runs
        if runs < cap:
            doc["candidate_runs"] = runs + 1
        return doc
    store.update_json(KEY, fn, {})
    if seen.get("gone"):
        return False
    if seen["runs"] >= cap:
        reject(store, version, f"{seen['runs']} runs ended without settling (crash, timeout or out of "
                               "memory)", job)
        return False
    return True


def select(store, job, install=pip_install, allow_candidate=True):
    """Install and import the version this run uses. -> {"version", "role"}."""
    doc = store.get_json(KEY, {}) or {}
    have = baked()
    for version, role in choices(doc, have):
        if role == "candidate" and (not allow_candidate or not begin_attempt(store, version, job)):
            continue
        if version != have:
            target = tempfile.mkdtemp(prefix=f"atlas-{version}-")
            try:
                install(version, target)
                activate(target)
            except Exception as exc:  # noqa: BLE001 - any install failure falls back
                # Inconclusive: the index may lag an upload, or the network failed. A candidate
                # keeps its counted attempt, so one that never installs is rolled back at the cap.
                alert("atlas_collector_install", "WARNING",
                      f"{role} collector {version} did not install ({type(exc).__name__}: {exc}); "
                      "falling back", job)
                continue
        try:
            got = loaded_version()
        except Exception as exc:  # noqa: BLE001
            got = f"import failed: {type(exc).__name__}"
        if got != version:
            alert("atlas_collector_install", "WARNING",
                  f"{role} collector {version} imports as {got}; falling back", job)
            if role == "candidate":
                reject(store, version, f"imports as {got}", job)
            continue
        return {"version": version, "role": role}
    raise RuntimeError("no usable Atlas collector: nothing installs and none is baked into the image")


def reject(store, version, why, job=None):
    def fn(doc):
        doc.setdefault("bad", [])
        if version not in doc["bad"]:
            doc["bad"].append(version)
        doc.setdefault("bad_why", {})[version] = why[:300]
        if doc.get("candidate") == version:
            doc["candidate"] = None
            doc["candidate_runs"] = 0
        return doc
    store.update_json(KEY, fn, {})
    alert("atlas_collector_rolled_back", "ERROR",
          f"collector {version} rejected ({why}); runs use the current version", job)


# Exceptions that say nothing about the collector: the platform or a service failed.
TRANSIENT_NAMES = {"ServiceUnavailable", "DeadlineExceeded", "TooManyRequests", "InternalServerError",
                   "BadGateway", "GatewayTimeout", "RetryError", "TransportError", "Conflict",
                   "PreconditionFailed", "SiteError", "DeployRefused", "RepoAccessError", "StateError",
                   "URLError", "HTTPError", "MemoryError", "TimeoutExpired"}


def _names(exc):
    return {c.__name__ for c in type(exc).__mro__}


def verdict_for_exception(exc):
    """-> "fault" when the collector itself failed, else "inconclusive"."""
    if getattr(exc, "collector_fault", False):
        return "fault"
    if _names(exc) & TRANSIENT_NAMES or isinstance(exc, (ConnectionError, TimeoutError)):
        return "inconclusive"
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb.tb_frame.f_code.co_filename
        tb = tb.tb_next
    # Raised inside the collector package (not in this repository's jobs): the candidate's code.
    parts = (last or "").replace("\\", "/").split("/")
    return "fault" if "atlas" in parts[:-1] and "jobs" not in parts else "inconclusive"


def settle(store, chosen, verdict, job=None, why=""):
    """After a run: promote a clean candidate, reject a faulty one, leave an inconclusive one."""
    if chosen.get("role") != "candidate":
        return None
    if verdict == "fault":
        reject(store, chosen["version"], why or "the run failed in the collector", job)
        return "rejected"
    if verdict != "clean":
        return "unsettled"

    def fn(doc):
        if doc.get("candidate") == chosen["version"]:
            doc["previous"] = doc.get("current")
            doc["current"] = chosen["version"]
            doc["candidate"] = None
            doc["candidate_runs"] = 0
        return doc
    store.update_json(KEY, fn, {})
    return "promoted"


def offer(store, version, commit=None):
    """atlas-wheel: a new tested version becomes the candidate (unless it is known bad)."""
    def fn(doc):
        if version in (doc.get("bad") or []) or version == doc.get("current"):
            return doc
        if doc.get("candidate") != version:
            doc["candidate_runs"] = 0
        doc["candidate"] = version
        if commit:
            doc.setdefault("offered", {})[version] = commit
        return doc
    return store.update_json(KEY, fn, {})


def reoffer_problem(doc, version, commit):
    """None when a bad `version` may be offered again after a clean rebuild at `commit`."""
    if version not in (doc.get("bad") or []):
        return f"{version} is not marked bad"
    n = int((doc.get("reoffers") or {}).get(version, 0))
    if n >= max_reoffers():
        return f"{version} was already re-offered {n} time(s); bump the version to ship a fix"
    was = (doc.get("offered") or {}).get(version)
    if not was or was != commit:
        return (f"{version} was uploaded from {str(was)[:12] or 'an unrecorded commit'}, main is at "
                f"{commit[:12]}: an uploaded version cannot be replaced, bump the version")
    return None


def reoffer(store, version, commit):
    """Clear a bad mark after a clean rebuild of the same commit. -> the problem, or None."""
    out = {}

    def fn(doc):
        out["problem"] = reoffer_problem(doc, version, commit)
        if out["problem"]:
            return doc
        doc["bad"] = [v for v in doc["bad"] if v != version]
        (doc.get("bad_why") or {}).pop(version, None)
        counts = doc.setdefault("reoffers", {})
        counts[version] = int(counts.get(version, 0)) + 1
        doc["candidate"], doc["candidate_runs"] = version, 0
        return doc
    store.update_json(KEY, fn, {})
    return out["problem"]
