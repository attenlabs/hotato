"""Which version of the private Atlas collector (hotato-atlas) a run uses, and how a new version
reaches production with no person and no image rebuild.

  atlas/collector.json  {"current": "0.2.0", "candidate": "0.2.1" | null, "bad": ["0.1.9", ...],
                         "candidate_runs": 0}

Order of preference: the candidate (a version atlas-wheel built, tested and uploaded), then the
current version, then the version baked into the image. A version that is not baked is installed
at run time from the private Python repository into a scratch directory (ADC through the Artifact
Registry keyring, so no credential is ever on a command line or on disk).

Settling: a candidate that completes one clean hourly run becomes current. A candidate whose run
crashes is marked bad (never chosen again), the candidate is cleared and an alert is logged; the
next run uses current. A failed install falls back the same way, within the same run.
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


def select(store, job, install=pip_install):
    """Install and import the version this run uses. -> {"version", "role"}."""
    doc = store.get_json(KEY, {}) or {}
    have = baked()
    for version, role in choices(doc, have):
        if version != have:
            target = tempfile.mkdtemp(prefix=f"atlas-{version}-")
            try:
                install(version, target)
                activate(target)
            except Exception as exc:  # noqa: BLE001 - any install failure falls back
                alert("atlas_collector_install", "WARNING",
                      f"{role} collector {version} did not install ({type(exc).__name__}: {exc}); "
                      "falling back", job)
                if role == "candidate":
                    reject(store, version, "install failed", job)
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
        if doc.get("candidate") == version:
            doc["candidate"] = None
        return doc
    store.update_json(KEY, fn, {})
    alert("atlas_collector_rolled_back", "ERROR",
          f"collector {version} rejected ({why}); runs use the current version", job)


def settle(store, chosen, ok, job=None):
    """After a run: promote a candidate that ran clean, reject one that crashed."""
    if chosen.get("role") != "candidate":
        return None
    if not ok:
        reject(store, chosen["version"], "the run crashed", job)
        return "rejected"

    def fn(doc):
        if doc.get("candidate") == chosen["version"]:
            doc["previous"] = doc.get("current")
            doc["current"] = chosen["version"]
            doc["candidate"] = None
        return doc
    store.update_json(KEY, fn, {})
    return "promoted"


def offer(store, version):
    """atlas-wheel: a new tested version becomes the candidate (unless it is known bad)."""
    def fn(doc):
        if version in (doc.get("bad") or []) or version == doc.get("current"):
            return doc
        doc["candidate"] = version
        return doc
    return store.update_json(KEY, fn, {})
