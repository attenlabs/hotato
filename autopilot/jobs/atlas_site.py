"""Where the Atlas pages are deployed, and how.

hotato.dev is one Cloudflare Pages project and a Pages deployment replaces the whole site. So
the Atlas never deploys "its" pages on their own: it re-deploys exactly the site release that is
live now, with only atlas/ (and the sitemap index that points at it) regenerated.

  1. The live production deployment is read from the Cloudflare API: its source commit and the
     project's production branch. Whatever release the owner shipped last is what gets
     re-deployed, so an Atlas publish can never roll the rest of the site back or forward.
  2. That exact commit of the private site repository is checked out (read-only credential).
  3. A spot check confirms the checkout is what the live site serves (llms.txt bytes and the
     fingerprinted stylesheet). A mismatch means the live site was deployed from files that are
     not that commit: nothing ships (fail closed) until the next site release.
  4. The pages are rendered into the checkout with the site's own chrome, and the site's own
     staging script (tools/stage-deploy.sh at that commit) deploys it: only publishable files,
     nothing untracked except atlas/. Its `npx wrangler` is served by a shim that runs the
     pinned wrangler in the image and adds the production branch and the source commit.
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error

from common import APP_SECRETS, GH_API, _gh_headers, app_jwt, http, http_resp, read_secret, redact

CF_API = "https://api.cloudflare.com/client/v4"
REPOS_TOKEN_SECRET = "atlas-repos-token"
CF_TOKEN_SECRET = "cloudflare-pages-token"


class SiteError(RuntimeError):
    """The site cannot be prepared or deployed; nothing ships this run."""


class DeployRefused(SiteError):
    """The site's own staging gate refused the tree: retrying would not help."""


# ---- read access to the private repositories ---------------------------------------

class RepoAccessError(SiteError):
    """No configured credential can read a private repository."""


def can_read(repo, token, timeout=60):
    """True when `token` can read `repo` over git (the path the checkout uses)."""
    try:
        cp = subprocess.run(["git", "ls-remote", "--heads", f"https://github.com/{repo}.git"],
                            env=git_env(token), capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return cp.returncode == 0


def _app_token(repo, fetch=None):
    app = [read_secret(s, fetch) for s in APP_SECRETS]
    if not all(app):
        return None, "GitHub App secrets not set"
    try:
        jwt = app_jwt(app[0], app[1])
        _, _, body = http_resp(f"{GH_API}/repos/{repo}/installation", headers=_gh_headers(jwt), timeout=20)
        inst = json.loads(body)["id"]
        payload = json.dumps({"repositories": [repo.split("/", 1)[1]],
                              "permissions": {"contents": "read"}}).encode()
        _, _, body = http_resp(f"{GH_API}/app/installations/{inst}/access_tokens", "POST",
                               _gh_headers(jwt), payload, timeout=20)
        return json.loads(body)["token"], None
    except urllib.error.HTTPError as e:
        return None, f"GitHub App: HTTP {e.code} (not installed on {repo}, or no contents access)"
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        return None, f"GitHub App: {type(e).__name__}"


def repo_token(repo, fetch=None, verify=None):
    """A token that is proven to read `repo` (owner/name). Tried in order, each one checked with
    a real read (git ls-remote) before it is used:
      1. atlas-repos-token, when the owner set it (a fine-grained read-only token): adding that
         secret is the fallback that needs no code change,
      2. a GitHub App installation token minted for that repository only (contents: read),
      3. the github-bot-token.
    None reads it -> RepoAccessError naming what was tried and how to fix it."""
    verify = verify or can_read
    tried = []
    tok = read_secret(REPOS_TOKEN_SECRET, fetch)
    if tok:
        if verify(repo, tok):
            return tok, "repos-token"
        tried.append(f"{REPOS_TOKEN_SECRET}: cannot read it")
    else:
        tried.append(f"{REPOS_TOKEN_SECRET}: not set")
    tok, why = _app_token(repo, fetch)
    if tok and verify(repo, tok):
        return tok, "app"
    tried.append(why or "GitHub App: token minted but cannot read it")
    pat = read_secret("github-bot-token", fetch)
    if pat and verify(repo, pat):
        return pat, "pat"
    tried.append("github-bot-token: " + ("cannot read it" if pat else "not set"))
    raise RepoAccessError(f"no credential can read {repo} ({'; '.join(tried)}). Fix: install the "
                          f"GitHub App on {repo} with contents read, or add a read-only token as "
                          f"secret {REPOS_TOKEN_SECRET}; no code change is needed")


def git_env(token):
    """Credential through git's environment config, never on a command line or in .git/config."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(),
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


def _git(args, cwd, env, timeout=300):
    cp = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if cp.returncode:
        raise SiteError(f"git {args[0]} failed ({cp.returncode}): {redact(cp.stderr)[-300:]}")
    return cp.stdout


def checkout(repo, commit, dest, token, attempts=3, sleep=time.sleep):
    """Exactly `commit` of `repo` at `dest` (a git checkout: the staging script asks git
    which files are tracked)."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise SiteError(f"live deployment names no full source commit ({commit!r})")
    env = git_env(token)
    os.makedirs(dest, exist_ok=True)
    _git(["init", "-q"], dest, env)
    _git(["remote", "add", "origin", f"https://github.com/{repo}.git"], dest, env)
    for i in range(attempts):
        try:
            _git(["fetch", "-q", "--depth", "1", "origin", commit], dest, env)
            break
        except SiteError:
            if i == attempts - 1:
                raise SiteError(f"commit {commit[:12]} of the live site cannot be fetched from {repo}: "
                                "it was deployed from a commit that is not pushed, or the credential "
                                "cannot read the repository")
            sleep(10 * 3 ** i)
    _git(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"], dest, env)
    head = _git(["rev-parse", "HEAD"], dest, env).strip()
    if head != commit:
        raise SiteError(f"checked out {head[:12]}, expected {commit[:12]}")
    return dest


# ---- Cloudflare: which release is live ---------------------------------------------

def cf_get(path, token):
    try:
        _, body = http(CF_API + path, headers={"Authorization": f"Bearer {token}"})
    except urllib.error.HTTPError as e:
        raise SiteError(f"Cloudflare API {path.split('?')[0]}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise SiteError(f"Cloudflare API unreachable: {type(e).__name__}") from e
    doc = json.loads(body)
    if not doc.get("success"):
        raise SiteError(f"Cloudflare API {path.split('?')[0]}: {doc.get('errors')}")
    return doc["result"]


def account_id(token):
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if acct:
        return acct
    accounts = cf_get("/accounts?per_page=5", token)
    if len(accounts) != 1:
        raise SiteError(f"set cloudflare_account_id: the token sees {len(accounts)} accounts")
    return accounts[0]["id"]


def live_source(token, account, project):
    """-> {production_branch, commit, deployment_id, created_on} of the live production deployment."""
    p = cf_get(f"/accounts/{account}/pages/projects/{project}", token)
    dep = p.get("canonical_deployment") or {}
    meta = ((dep.get("deployment_trigger") or {}).get("metadata") or {})
    if not dep or not p.get("production_branch"):
        raise SiteError(f"Pages project {project} has no production deployment")
    return {"production_branch": p["production_branch"], "commit": meta.get("commit_hash") or "",
            "deployment_id": dep.get("id", ""), "created_on": dep.get("created_on", "")}


def same_deployment(before, after):
    """The live production deployment did not change between two reads."""
    return all(before.get(k) == after.get(k) for k in ("deployment_id", "commit", "production_branch"))


ALLOWED_CHANGES = ("atlas/", "sitemap_index.xml")


def changes_outside_atlas(site_dir, commit):
    """Paths that differ from `commit` outside atlas/ and sitemap_index.xml: tracked edits,
    deletions, untracked and ignored files. Raises SiteError when git cannot answer."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(),
           "GIT_CONFIG_NOSYSTEM": "1"}
    head = _git(["rev-parse", "HEAD"], site_dir, env).strip()
    if head != commit:
        return [f"HEAD is {head[:12]}, not the live commit {commit[:12]}"]
    out = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"],
               site_dir, env)
    paths = []
    items = out.split("\0")
    i = 0
    while i < len(items):
        rec = items[i]
        i += 1
        if len(rec) < 4:
            continue
        code, path = rec[:2], rec[3:]
        paths.append(path)
        if code[0] in "RC":  # renames and copies carry the source path next
            paths.append(items[i])
            i += 1
    return sorted(p for p in set(paths) if not (p == ALLOWED_CHANGES[1] or p.startswith(ALLOWED_CHANGES[0])))


CSS_RE = re.compile(r"/assets/hotato\.[0-9a-f]{6,}\.css")


def live_mismatches(site_dir, site_url, fetch):
    """Spot check that the live site serves this checkout. fetch(url) -> bytes or None."""
    out = []
    local = open(os.path.join(site_dir, "llms.txt"), "rb").read() if os.path.exists(
        os.path.join(site_dir, "llms.txt")) else None
    live = fetch(f"{site_url}/llms.txt")
    if local is None or live is None or live != local:
        out.append("llms.txt differs from the live site")
    home = fetch(f"{site_url}/") or b""
    idx = os.path.join(site_dir, "index.html")
    want = CSS_RE.search(open(idx, encoding="utf-8", errors="replace").read()) if os.path.exists(idx) else None
    got = CSS_RE.search(home.decode("utf-8", "replace"))
    if not want or not got or want.group(0) != got.group(0):
        out.append("the live home page links a different stylesheet")
    return out


# ---- deploy through the site's own staging script ---------------------------------

SHIM = r"""#!/bin/sh
# Serves `npx wrangler pages deploy ...` for the site's staging script with the wrangler pinned
# in the job image, adding the production branch and the source commit. Anything else is refused.
set -eu
[ "${1:-}" = "--yes" ] && shift
if [ "${1:-}" != "wrangler" ] || [ "${2:-}" != "pages" ] || [ "${3:-}" != "deploy" ]; then
  echo "atlas deploy: refused: npx $*" >&2
  exit 97
fi
shift
project=""
prev=""
for a in "$@"; do
  [ "$prev" = "--project-name" ] && project="$a"
  case "$a" in
    --project-name=*) project="${a#--project-name=}" ;;
    --branch|--branch=*|--commit-hash|--commit-hash=*)
      echo "atlas deploy: refused: the staging script already passes $a" >&2
      exit 97 ;;
  esac
  prev="$a"
done
if [ "$project" != "$ATLAS_PAGES_PROJECT" ]; then
  echo "atlas deploy: refused: project '$project' is not $ATLAS_PAGES_PROJECT" >&2
  exit 97
fi
exec "$ATLAS_WRANGLER" "$@" --branch "$ATLAS_DEPLOY_BRANCH" --commit-hash "$ATLAS_DEPLOY_COMMIT" \
  --commit-message "$ATLAS_DEPLOY_MESSAGE"
"""


def deploy(site_dir, *, token, account, project, branch, commit, message, wrangler,
           attempts=3, sleep=time.sleep, timeout=900):
    """Run the checkout's own tools/stage-deploy.sh. -> {"url", "output"}. Transient failures
    are retried with backoff; a refusal by the staging gate is not."""
    script = os.path.join(site_dir, "tools", "stage-deploy.sh")
    if not os.path.isfile(script):
        raise SiteError("the live site release has no tools/stage-deploy.sh")
    with tempfile.TemporaryDirectory(prefix="atlas-deploy-") as tmp:
        shim = os.path.join(tmp, "bin", "npx")
        os.makedirs(os.path.dirname(shim))
        with open(shim, "w") as fh:
            fh.write(SHIM)
        os.chmod(shim, 0o755)
        env = {"PATH": os.path.dirname(shim) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": tmp, "TMPDIR": tmp, "LANG": "C.UTF-8", "CI": "1", "NO_COLOR": "1",
               "WRANGLER_SEND_METRICS": "false", "CLOUDFLARE_API_TOKEN": token,
               "CLOUDFLARE_ACCOUNT_ID": account, "ATLAS_WRANGLER": wrangler,
               "ATLAS_PAGES_PROJECT": project, "ATLAS_DEPLOY_BRANCH": branch,
               "ATLAS_DEPLOY_COMMIT": commit, "ATLAS_DEPLOY_MESSAGE": message}
        last = ""
        for i in range(attempts):
            try:
                cp = subprocess.run(["bash", script], cwd=site_dir, env=env, capture_output=True,
                                    text=True, timeout=timeout)
                out = redact((cp.stdout or "") + (cp.stderr or ""))
            except subprocess.TimeoutExpired:
                cp, out = None, "deploy timed out"
            if cp is not None and cp.returncode == 0:
                m = re.search(r"https://[0-9a-z-]+\." + re.escape(project) + r"\.pages\.dev\S*", out)
                return {"url": m.group(0) if m else "", "output": out[-1500:]}
            last = out[-1500:]
            # Refused before reaching wrangler (the staging gate, or the shim): deterministic.
            if cp is not None and (cp.returncode == 97 or not re.search(r"^stage: \d+ files$", out, re.M)):
                raise DeployRefused(f"the site's staging gate refused the tree: {last[-500:]}")
            if i < attempts - 1:
                sleep(30 * 3 ** i)
        raise SiteError(f"deploy failed {attempts} times: {last[-500:]}")


def fetch_url(url, timeout=30):
    """GET without caches. -> bytes, or None on any failure."""
    sep = "&" if "?" in url else "?"
    try:
        _, body = http(f"{url}{sep}t={int(time.time())}" if url.endswith(".json") else url,
                       headers={"Cache-Control": "no-cache", "User-Agent": "hotato-autopilot"},
                       timeout=timeout)
        return body
    except (urllib.error.URLError, OSError, ValueError):
        return None
