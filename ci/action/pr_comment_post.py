"""Post (or update) ONE sticky pull-request comment from a rendered hotato
Markdown body, and mirror the same body onto the job summary. Standalone,
stdlib-only, and FAIL-OPEN by contract: every missing precondition (no
rendered file, no token, not a pull request, a fork pull request, any API
error) is a no-op that prints a note and exits 0. It NEVER raises and NEVER
returns non-zero, so a comment failure can never change the gate's exit code
-- the hotato exit code owns the gate, always.

This is the separate, opt-in "post it to the PR" step (``comment-pr: true``).
The deterministic, offline rendering lives in hotato itself: ``hotato pr
comment`` (the full report: verdict, failing cases, red-to-green against the
base, evidence files) and, on older pins, ``hotato contract verify
--pr-comment``. The gate writes the rendered leaf; this file only takes those
bytes and, when a token is present, mirrors them onto one sticky comment.

Inputs, all from the environment (the composite step maps them in, so no input
is ever shell-evaluated):

* ``HOTATO_PR_COMMENT_FILE``  -- path to the rendered Markdown body.
* ``HOTATO_PR_COMMENT_TOKEN`` (or ``GITHUB_TOKEN``) -- the API token; absent
  means no-op.
* ``HOTATO_PR_COMMENT_SUMMARY`` -- ``true`` to also append the body to
  ``GITHUB_STEP_SUMMARY`` (with a one-line note when it was not posted), so a
  fork pull request or a token without write access still shows the report.
* ``GITHUB_REPOSITORY``       -- ``owner/repo``.
* ``GITHUB_EVENT_PATH`` / ``GITHUB_REF`` -- source of the pull-request number
  and of the fork check.
* ``GITHUB_API_URL``          -- API base (defaults to api.github.com).

The sticky markers match the renderers' (a test pins them in lockstep), so
re-runs update the same comment in place rather than stacking a new one each
push. A comment carrying the older contract-verify marker is updated to the new
body too, never left behind next to a second comment. Only a comment written
by the token's own identity is ever edited: ``GET /user`` names it for a
personal token, and the workflow token (which cannot read ``/user``) is the
Actions bot. A human's or another bot's comment that quotes the marker is never
touched; a fresh comment is created instead.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

# Must match hotato.pr_report.MARKER and hotato.contract._PR_COMMENT_MARKER
# (a test pins them in lockstep). The first is the current report; the second
# is the earlier contract-verify block, still recognised so an existing sticky
# comment is updated in place instead of a second one being added.
REPORT_MARKER = "<!-- hotato-pr-report -->"
MARKER = "<!-- hotato-contract-verify -->"
MARKERS = (REPORT_MARKER, MARKER)
_BOT_LOGIN = "github-actions[bot]"
_UA = "hotato-pr-comment"
# 100 comments a page; a PR with more than 30,000 comments is not searched
# to the end, and then the poster skips rather than risk a duplicate.
_MAX_PAGES = 300
# GitHub refuses an issue comment body above 65,536 characters.
_MAX_BODY = 65000
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


def _load_event(event_path):
    if not event_path:
        return None
    try:
        with open(event_path, "r", encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def is_fork(event_path):
    """True when the event is a pull request opened from a fork (its head
    repository differs from the base, or the fork is gone). GitHub hands such a
    run a read-only token, so the poster skips the API and the report lands on
    the job summary instead. ``False`` when it cannot tell."""
    event = _load_event(event_path)
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pr, dict):
        return False
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    if "repo" in head and head.get("repo") is None:
        return True  # the fork was deleted
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    h, b = head_repo.get("full_name"), base_repo.get("full_name")
    if isinstance(h, str) and isinstance(b, str):
        return h.lower() != b.lower()
    return bool(head_repo.get("fork"))


def _markers(marker):
    return (marker,) if isinstance(marker, str) else tuple(marker)


def find_existing_comment(comments, marker=MARKERS, author=_BOT_LOGIN):
    """The sticky comment to update in place: the first comment written by
    ``author`` (the token's own login) whose body carries one of the markers.
    A comment by anyone else is never returned, even when it quotes the
    marker, so the poster can only ever edit its own comment. ``None`` when
    there is none. Pure; never raises."""
    if not isinstance(comments, list) or not isinstance(author, str) \
            or not author:
        return None
    markers = _markers(marker)
    for c in comments:
        if not isinstance(c, dict) or not isinstance(c.get("id"), int):
            continue
        user = c.get("user") if isinstance(c.get("user"), dict) else {}
        if user.get("login") != author:
            continue
        body = str(c.get("body") or "")
        if any(m in body for m in markers):
            return c
    return None


def find_existing(comments, marker=MARKERS, author=_BOT_LOGIN):
    """The id of the sticky comment (see :func:`find_existing_comment`), or
    ``None``. Pure; never raises."""
    c = find_existing_comment(comments, marker, author)
    return c["id"] if c else None


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
    """Every comment on the pull request, page by page until a short page
    (the last one). Returns ``(comments, complete)``; ``complete`` is False
    only when the page cap stopped the walk before the last page."""
    out = []
    for page in range(1, _MAX_PAGES + 1):
        url = (f"{api}/repos/{repo}/issues/{number}/comments"
               f"?per_page=100&page={page}")
        batch = _request(url, token)
        if not isinstance(batch, list) or not batch:
            return out, True
        out.extend(batch)
        if len(batch) < 100:
            return out, True
    return out, False


def _http_status(exc):
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def own_login(api, token):
    """The login the token writes comments as. ``GET /user`` answers for a
    personal access token; the workflow token (an installation token) is
    refused there with 401/403/404, and its comments are the Actions bot's.
    Any other failure raises, and the caller skips the post."""
    try:
        me = _request(f"{api}/user", token)
    except urllib.error.HTTPError as exc:
        if _http_status(exc) in (401, 403, 404):
            return _BOT_LOGIN
        raise
    login = me.get("login") if isinstance(me, dict) else None
    return login if isinstance(login, str) and login else _BOT_LOGIN


def post_or_update(api, repo, number, token, body, marker=MARKERS):
    """Update the sticky comment if one exists (skipping the write when its
    body is already identical), else create it. Returns a short status
    string. Fully wrapped: any API/network error is caught and reported, never
    raised, so the caller always exits 0."""
    try:
        author = own_login(api, token)
        comments, complete = _list_comments(api, repo, number, token)
        existing = find_existing_comment(comments, marker, author)
        if existing is None and not complete:
            return ("skipped: the pull request has more comments than the"
                    " poster searches, so it posts nothing rather than risk"
                    " a duplicate; the report is in the job summary (gate"
                    " unaffected)")
        if existing is not None:
            cid = existing["id"]
            if str(existing.get("body") or "") == body:
                return f"comment {cid} is already up to date"
            _request(f"{api}/repos/{repo}/issues/comments/{cid}",
                     token, method="PATCH", payload={"body": body})
            return f"updated comment {cid}"
        _request(f"{api}/repos/{repo}/issues/{number}/comments",
                 token, method="POST", payload={"body": body})
        return "posted a new comment"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        code = _http_status(exc)
        if code in (401, 403, 404):
            return (f"skipped: the token cannot write to this pull request"
                    f" (HTTP {code}); grant pull-requests: write, the report"
                    " is in the job summary (gate unaffected)")
        return f"skipped (a comment API problem, gate unaffected): {exc}"


def _clip_body(body):
    if len(body) <= _MAX_BODY:
        return body
    cut = body[:_MAX_BODY]
    cut = cut[: cut.rfind("\n") + 1]
    return (cut + "\n_Truncated to stay under the pull-request comment size"
            " limit; the job summary and the results artifact are"
            " complete._\n")


def write_summary(body, note=None):
    """Append the body (and, when it was not posted, a one-line note) to the
    job summary. Fail-open: any problem is silently a no-op."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    text = body.rstrip("\n") + "\n"
    if note:
        text += "\n_Not posted to the pull request: " + " ".join(
            str(note).split()) + "._\n"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        return False
    return True


def _post(body):
    """Decide and perform the post. Returns ``(posted, message)``."""
    token = (os.environ.get("HOTATO_PR_COMMENT_TOKEN")
             or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        return False, ("no token provided; skipping PR comment (the gate is"
                       " unaffected)")

    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" not in repo:
        return False, "no owner/repo in GITHUB_REPOSITORY; skipping PR comment"

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    number = pr_number(event_path, os.environ.get("GITHUB_REF"))
    if number is None:
        return False, "not a pull request; skipping PR comment"

    if is_fork(event_path):
        return False, ("a pull request from a fork gets a read-only token;"
                       " skipping PR comment, the report is in the job"
                       " summary (gate unaffected)")

    api = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip(
        "/")
    status = post_or_update(api, repo, number, token, _clip_body(body))
    return not status.startswith("skipped"), status


def main() -> int:
    body = read_body(os.environ.get("HOTATO_PR_COMMENT_FILE"))
    if body is None:
        _note("no rendered PR-comment block to post; nothing to do")
        return 0
    try:
        posted, message = _post(body)
    except Exception as exc:  # fail-open: never change the gate
        posted, message = False, f"skipped (unexpected poster error): {exc}"
    _note(message)
    if (os.environ.get("HOTATO_PR_COMMENT_SUMMARY") or "").strip() == "true":
        write_summary(body, None if posted else message.split(";")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
