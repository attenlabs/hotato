"""atlas-wheel: ship a new version of the private Atlas collector with no person.

Daily. When the collector repository's main branch carries a version that is not yet in the
private Python repository, this job checks it out (read-only credential), runs its test suite,
builds the wheel, checks the wheel holds only the atlas package, smoke-tests the built wheel
offline with the stub engines, uploads it, and offers it as the candidate. The hourly job then
runs the candidate, and promotes it after one clean run or rolls it back (atlas_collector.py).

The wheel is built and installed into a scratch directory first; the collector's tests then run
over the checked-out source with an allow-listed environment (no credential, no job
configuration and no cloud identity variables reach them), and the smoke run uses the installed
wheel alone.

Uploaded versions are never replaced: a version that exists is never uploaded again, and a
version that failed here is not offered. A version the hourly job rolled back is re-offered only
when main still carries it at the commit that was offered and it passes the tests and the smoke
run again (atlas_collector.reoffer, at most ATLAS_CANDIDATE_MAX_REOFFERS times). The source is
never published; only the private repository holds the wheel.
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


# The only variables the collector's tests see. Nothing that names a credential, a secret, the
# project, the site repository or the job's own configuration.
TEST_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TZ")


def test_env(work, pkg):
    env = {k: os.environ[k] for k in TEST_ENV_KEEP if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(HOME=work, TMPDIR=work, PYTHONPATH=pkg, PYTHONDONTWRITEBYTECODE="1",
               PYTHONNOUSERSITE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


# What pip itself may also need to reach the public index for the build backend (setuptools).
BUILD_ENV_KEEP = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
                  "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "PIP_CERT")


def build_env(work):
    """`pip wheel` runs the collector repository's own build backend, so it gets the same
    allow-listed environment as the tests, plus proxy and CA settings: no secret, no token, no
    job configuration. The wheel needs nothing from the private index to build."""
    env = test_env(work, "")
    env.pop("PYTHONPATH")
    env.update({k: os.environ[k] for k in BUILD_ENV_KEEP if k in os.environ})
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


def build_and_test(tree, version, work):
    """Build the wheel, check what it holds, install it, run the collector's suite over its
    source, then smoke-run the installed wheel offline. -> (wheel path, smoke tail)."""
    dist = os.path.join(work, "dist")
    home = os.path.join(work, "home")
    os.makedirs(home, exist_ok=True)
    benv = build_env(home)
    _run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", dist, tree], work, 600, benv)
    wheel = os.path.join(dist, f"hotato_atlas-{version}-py3-none-any.whl")
    if not os.path.isfile(wheel):
        raise RuntimeError(f"build produced no {os.path.basename(wheel)}")
    bad = wheel_problems(wheel, version)
    if bad:
        raise RuntimeError(f"unexpected wheel contents: {bad[:10]}")
    pkg = os.path.join(work, "pkg")
    _run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--no-index", "--target", pkg, wheel],
         work, 300, benv)
    # The suite runs over the source tree it came with (some tests compare files the wheel does
    # not carry, such as data/hotato-cli-manifest.json); the smoke run below uses the wheel alone.
    _run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], tree, 1800,
         test_env(home, os.path.join(tree, "src")))
    env = test_env(home, pkg)
    out = _run([sys.executable, "-c",
                "import sys, atlas, atlas.cli as c; "
                "assert atlas.__file__.startswith(sys.argv[1]), atlas.__file__; "
                "assert atlas.__version__ == sys.argv[2], atlas.__version__; "
                "sys.exit(c.main(['--offline', '--db', sys.argv[3] + '/a.db', '--cache', sys.argv[3] + '/c', "
                "'run', '--engine', 'stub', '--agents', 'stub', '--limit', '1', '--no-pages', "
                "'--out', sys.argv[3] + '/site']))",
                pkg, version, home], home, 600, env)
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


def work(store, rec, token_fn=gcp_token, fetch_index=None, build=None, ship=None):
    repo = os.environ.get("ATLAS_COLLECTOR_REPO", "")
    if not repo:
        rec["skipped"] = "no collector repository configured (atlas_collector_repo)"
        return rec["skipped"]
    build, ship = build or build_and_test, ship or upload
    doc = store.get_json(collector.KEY, {}) or {}
    try:
        gh, kind = site.repo_token(repo)
    except site.RepoAccessError as exc:
        alert("atlas_repo_access", "ERROR", f"collector versions cannot ship: {exc}", "atlas-wheel")
        raise
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
        published = version in published_versions(token, fetch_index)
        rejected = version in (doc.get("bad") or [])
        if rejected:
            problem = collector.reoffer_problem(doc, version, commit)
            if problem:
                rec["skipped"] = f"{version} was rolled back and is not re-offered: {problem}"
                return rec["skipped"]
        elif published:
            rec["skipped"] = f"{version} is already published"
            return rec["skipped"]
        try:
            wheel, _smoke = build(tree, version, work_dir)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            alert("atlas_wheel_failed", "ERROR", f"collector {version} at {commit[:12]} not shipped: {exc}",
                  "atlas-wheel")
            raise
        if not published:
            ship(wheel, version, token)
    if rejected:
        problem = collector.reoffer(store, version, commit)
        if problem:
            rec["skipped"] = problem
            return problem
        rec["reoffered"] = version
        return f"re-offered {version} after a clean rebuild"
    collector.offer(store, version, commit)
    rec["offered"] = version
    return f"offered {version}"


if __name__ == "__main__":
    main_guard("atlas-wheel", work)
