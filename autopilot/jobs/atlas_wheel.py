"""atlas-wheel: ship a new version of the private Atlas collector with no person.

Daily. When the collector repository's main branch carries a version that is not yet in the
private Python repository, this job checks it out (read-only credential), runs its test suite,
builds the wheel, checks the wheel holds only the atlas package, smoke-tests the built wheel
offline with the stub engines, uploads it, and offers it as the candidate. The hourly job then
runs the candidate, and promotes it after one clean run or rolls it back (atlas_collector.py).

Uploaded versions are never replaced: a version that exists is never built again, and a version
that failed here is not offered. The source is never published; only the private repository
holds the wheel.
"""
import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import uuid
import zipfile

from common import alert, gcp_token, http, http_resp, main_guard
import atlas_collector as collector
import atlas_site as site

PACKAGE = collector.PACKAGE


def repo_base():
    region = os.environ.get("GCP_REGION", "us-central1")
    repo = os.environ.get("ATLAS_PY_REPO", "hotato-private-py")
    return f"https://{region}-python.pkg.dev/{os.environ.get('GCP_PROJECT', '')}/{repo}/"


def _auth(token):
    return {"Authorization": "Basic " + base64.b64encode(f"oauth2accesstoken:{token}".encode()).decode()}


def published_versions(token, fetch=None):
    """Versions of the package already in the private repository (its simple index)."""
    url = repo_base() + f"simple/{PACKAGE}/"
    try:
        body = (fetch or (lambda u: http(u, headers=_auth(token))[1]))(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return set()
        raise
    names = re.findall(r"hotato_atlas-(\d+\.\d+\.\d+)-py3-none-any\.whl", body.decode("utf-8", "replace"))
    return set(names)


def source_version(tree):
    with open(os.path.join(tree, "pyproject.toml"), "rb") as fh:
        doc = tomllib.load(fh)
    name, version = doc["project"]["name"], doc["project"]["version"]
    if name != PACKAGE or not collector.VERSION_RE.fullmatch(version):
        raise RuntimeError(f"collector repository holds {name} {version}, not a {PACKAGE} release")
    return version


def wheel_problems(path, version):
    allowed = ("atlas/", f"hotato_atlas-{version}.dist-info/")
    names = zipfile.ZipFile(path).namelist()
    bad = [n for n in names if not n.startswith(allowed)]
    for need in ("atlas/cli.py", "atlas/distill.py", "atlas/sitepages.py"):
        if need not in names:
            bad.append(f"{need} missing")
    return bad


def _run(argv, cwd, timeout, env=None):
    cp = subprocess.run(["nice", "-n", "19", *argv], cwd=cwd, capture_output=True, text=True,
                        timeout=timeout, env=env)
    if cp.returncode:
        raise RuntimeError(f"{' '.join(argv[:3])} failed ({cp.returncode}): {(cp.stdout + cp.stderr)[-600:]}")
    return cp.stdout


def build_and_test(tree, version, work):
    env = dict(os.environ, PYTHONPATH=os.path.join(tree, "src"), PYTHONDONTWRITEBYTECODE="1")
    env.pop("GITHUB_TOKEN", None)
    _run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], tree, 1800, env)
    dist = os.path.join(work, "dist")
    _run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", dist, tree], work, 600)
    wheel = os.path.join(dist, f"hotato_atlas-{version}-py3-none-any.whl")
    if not os.path.isfile(wheel):
        raise RuntimeError(f"build produced no {os.path.basename(wheel)}")
    bad = wheel_problems(wheel, version)
    if bad:
        raise RuntimeError(f"unexpected wheel contents: {bad[:10]}")
    # Smoke: the built wheel alone, offline, stub engines, against an empty store.
    target = os.path.join(work, "smoke")
    _run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--no-index", "--target", target, wheel],
         work, 300)
    senv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": target, "HOME": work}
    out = _run([sys.executable, "-I", "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import atlas, atlas.cli as c; "
                "assert atlas.__version__ == sys.argv[2], atlas.__version__; "
                "sys.exit(c.main(['--offline', '--db', sys.argv[3] + '/a.db', '--cache', sys.argv[3] + '/c', "
                "'run', '--engine', 'stub', '--agents', 'stub', '--limit', '1', '--no-pages', "
                "'--out', sys.argv[3] + '/site']))",
                target, version, work], work, 600, senv)
    return wheel, out[-300:]


def upload(wheel, version, token):
    """The PyPI legacy upload API that Artifact Registry speaks, from this process (the access
    token stays in memory)."""
    data = open(wheel, "rb").read()
    fields = {":action": "file_upload", "protocol_version": "1", "name": PACKAGE, "version": version,
              "filetype": "bdist_wheel", "pyversion": "py3", "metadata_version": "2.1",
              "sha256_digest": hashlib.sha256(data).hexdigest()}
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="content"; '
                  f'filename="{os.path.basename(wheel)}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
                 + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    headers = dict(_auth(token), **{"Content-Type": f"multipart/form-data; boundary={boundary}"})
    http_resp(repo_base(), "POST", headers, b"".join(parts), timeout=120)


def work(store, rec, token_fn=gcp_token, fetch_index=None):
    repo = os.environ.get("ATLAS_COLLECTOR_REPO", "")
    if not repo:
        rec["skipped"] = "no collector repository configured (atlas_collector_repo)"
        return rec["skipped"]
    doc = store.get_json(collector.KEY, {}) or {}
    gh, kind = site.repo_token(repo)
    rec["credential"] = kind
    with tempfile.TemporaryDirectory(prefix="atlas-wheel-") as work_dir:
        tree = os.path.join(work_dir, "src")
        env = site.git_env(gh)
        os.makedirs(tree)
        site._git(["init", "-q"], tree, env)
        site._git(["remote", "add", "origin", f"https://github.com/{repo}.git"], tree, env)
        site._git(["fetch", "-q", "--depth", "1", "origin", "main"], tree, env)
        site._git(["checkout", "-q", "--detach", "FETCH_HEAD"], tree, env)
        commit = site._git(["rev-parse", "HEAD"], tree, env).strip()
        version = source_version(tree)
        rec.update(version=version, commit=commit[:12])
        token = token_fn()
        if version in published_versions(token, fetch_index) or version in (doc.get("bad") or []):
            rec["skipped"] = f"{version} is already published (or was rejected)"
            return rec["skipped"]
        try:
            wheel, smoke = build_and_test(tree, version, work_dir)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            alert("atlas_wheel_failed", "ERROR", f"collector {version} at {commit[:12]} not shipped: {exc}",
                  "atlas-wheel")
            raise
        upload(wheel, version, token)
    collector.offer(store, version)
    rec["offered"] = version
    return f"offered {version}"


if __name__ == "__main__":
    main_guard("atlas-wheel", work)
