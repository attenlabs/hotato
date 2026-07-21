"""Post (or update) ONE sticky pull-request comment from a rendered hotato
contract-verify PR-comment block. Standalone, stdlib-only, and FAIL-OPEN by
contract: every missing precondition (no rendered file, no token, not a pull
request, any API error) is a no-op that prints a note and exits 0. It NEVER
raises and NEVER returns non-zero, so a comment failure can never change the
gate's exit code -- the verify exit code owns the gate, always.

This is the separate, opt-in "post it to the PR" step. The deterministic,
offline rendering lives in ``hotato contract verify --pr-comment FILE`` (and
the Action writes that leaf); this file only takes the already-rendered bytes
and, when a token is present, mirrors them onto one sticky comment.

Inputs, all from the environment (the composite step maps them in, so no input
is ever shell-evaluated):

* ``HOTATO_PR_COMMENT_FILE``  -- path to the rendered Markdown block.
* ``HOTATO_PR_COMMENT_TOKEN`` (or ``GITHUB_TOKEN``) -- the API token; absent
  means no-op.
* ``GITHUB_REPOSITORY``       -- ``owner/repo``.
* ``GITHUB_EVENT_PATH`` / ``GITHUB_REF`` -- source of the pull-request number.
* ``GITHUB_API_URL``          -- API base (defaults to api.github.com).

The sticky marker matches the renderer's, so re-runs update the same comment in
place rather than stacking a new one each push.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

# Must match hotato.contract._PR_COMMENT_MARKER (a test pins them in lockstep).
MARKER = "<!-- hotato-contract-verify -->"
_UA = "hotato-pr-comment"
_MAX_PAGES = 10
_REF_PR_RE = re.compile(r"^refs/pull/(\d+)/")


def _note(message: str) -> None:
    """A single stderr note. Presentation only; never affects the exit code."""
    print("note: " + " ".join(str(message).split()), file=sys.stderr)


def read_body(path):
    """The rendered block, or ``None`` when it is missing/empty/unreadable.
    Fail-open: any problem is a no-op, never an error."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return None
    return body if body.strip() else None


def pr_number(event_path, ref):
    """The pull-request number from the event payload (preferred) or a
    ``refs/pull/N/...`` ref (fallback). ``None`` when this is not a pull
    request, so the poster stays a no-op off pull requests."""
    if event_path:
        try:
            with open(event_path, "r", encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, ValueError):
            event = None
        if isinstance(event, dict):
            pr = event.get("pull_request")
            if isinstance(pr, dict) and isinstance(pr.get("number"), int):
                return pr["number"]
            num = event.get("number")
            if isinstance(num, int):
                return num
    if ref:
        m = _REF_PR_RE.match(ref)
        if m:
            return int(m.group(1))
    return None


def find_existing(comments, marker=MARKER):
    """The id of the first comment whose body carries ``marker`` (the sticky
    comment to update in place), or ``None``. Pure; never raises."""
    if not isinstance(comments, list):
        return None
    for c in comments:
        if isinstance(c, dict) and marker in str(c.get("body") or ""):
            cid = c.get("id")
            if isinstance(cid, int):
                return cid
    return None


def _request(url, token, *, method="GET", payload=None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", _UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (https API)
        raw = resp.read().decode("utf-8") or "null"
    return json.loads(raw)


def _list_comments(api, repo, number, token):
    out = []
    for page in range(1, _MAX_PAGES + 1):
        url = (f"{api}/repos/{repo}/issues/{number}/comments"
               f"?per_page=100&page={page}")
        batch = _request(url, token)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def post_or_update(api, repo, number, token, body, marker=MARKER):
    """Update the sticky comment if one exists, else create it. Returns a short
    status string. Fully wrapped: any API/network error is caught and reported,
    never raised, so the caller always exits 0."""
    try:
        existing = find_existing(
            _list_comments(api, repo, number, token), marker)
        if existing is not None:
            _request(f"{api}/repos/{repo}/issues/comments/{existing}",
                     token, method="PATCH", payload={"body": body})
            return f"updated comment {existing}"
        _request(f"{api}/repos/{repo}/issues/{number}/comments",
                 token, method="POST", payload={"body": body})
        return "posted a new comment"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"skipped (a comment API problem, gate unaffected): {exc}"


def main() -> int:
    body = read_body(os.environ.get("HOTATO_PR_COMMENT_FILE"))
    if body is None:
        _note("no rendered PR-comment block to post; nothing to do")
        return 0

    token = (os.environ.get("HOTATO_PR_COMMENT_TOKEN")
             or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        _note("no token provided; skipping PR comment (the gate is unaffected)")
        return 0

    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" not in repo:
        _note("no owner/repo in GITHUB_REPOSITORY; skipping PR comment")
        return 0

    number = pr_number(os.environ.get("GITHUB_EVENT_PATH"),
                       os.environ.get("GITHUB_REF"))
    if number is None:
        _note("not a pull request; skipping PR comment")
        return 0

    api = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip(
        "/")
    _note(post_or_update(api, repo, number, token, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
