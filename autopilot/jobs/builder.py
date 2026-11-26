"""builder job: headless coding agent (Claude Code CLI on Vertex AI) takes the top
BACKLOG.md item, works on a branch, and opens a PR with the github-bot-token.
The token can push branches and open PRs only; branch
protection and CODEOWNERS bind it. Changes to owner-only paths are refused before the push."""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import bot_token, debit, dry_run, env_float, gate_violation, gh, main_guard, now, redact, repo  # noqa: E402
import vertex_models as models  # noqa: E402
from gates.policy import classify_file, load_config  # noqa: E402

TOOLS = "Read,Edit,Write,Glob,Grep,Bash(pytest:*),Bash(python3 -m pytest:*),Bash(git diff:*),Bash(git status:*)"


# ---- pure logic -------------------------------------------------------------
def top_item(md):
    """First un-done numbered item: `N. title` (done items are marked `[x]`)."""
    for ln in md.splitlines():
        m = re.match(r"^\s*\d+\.\s+(?!\[x\])(.*)$", ln)
        if m:
            return m.group(1).strip()
    return None


BACKLOG = "autopilot/BACKLOG.md"  # path inside the cloned repository


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


def forbidden_paths(files, cfg=None):
    cfg = cfg or load_config()
    return [f for f in files if classify_file(f, cfg) == "owner_only"]


def agent_prompt(item):
    return (f"Implement this backlog item in the repository in the current directory: {item}\n"
            "Write the code and tests. Run the test suite until it passes. Do not edit autopilot/ "
            "or .github/. Do not add placeholder or 'coming soon' text. "
            "Keep the change small. Do not commit; stop when tests pass.")


def agent_argv(prompt, model=None):
    return ["claude", "-p", prompt, "--allowedTools", TOOLS, "--permission-mode", "acceptEdits",
            "--max-budget-usd", str(env_float("BUILDER_MAX_USD", 5)),
            "--model", model or os.environ.get("BUILDER_MODEL", "claude-sonnet-4-5"), "--output-format", "text"]


def agent_env():
    # the agent never sees a GitHub credential (bot token or App key)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_BOT", "GITHUB_APP"))}
    env.update(CLAUDE_CODE_USE_VERTEX="1", ANTHROPIC_VERTEX_PROJECT_ID=os.environ.get("GCP_PROJECT", ""),
               CLOUD_ML_REGION=os.environ.get("GCP_REGION", "us-central1"))
    return env


# ---- runner -----------------------------------------------------------------
def sh(argv, cwd, **kw):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, **kw)


def work(store, rec):
    r = repo()
    model = rec["model"] = models.resolve("builder")
    if not debit(store, "agents", env_float("BUILDER_MAX_USD", 5), env_float("AGENTS_CAP_USD", 0)):
        return {"skipped": "agents spend cap reached"}
    tok = bot_token() if not dry_run() else None
    with tempfile.TemporaryDirectory() as d:
        url = f"https://x-access-token:{tok}@github.com/{r}.git" if tok else f"https://github.com/{r}.git"
        cp = sh(["git", "clone", "--depth", "50", url, d], None)
        if cp.returncode != 0:  # never let the tokenised URL reach the exception text
            raise RuntimeError(f"git clone of {r} failed ({cp.returncode}): {redact(cp.stderr)[-300:]}")
        # keep the credential out of .git/config while the agent works in this checkout
        sh(["git", "remote", "set-url", "origin", f"https://github.com/{r}.git"], d)
        item = top_item(open(os.path.join(d, BACKLOG)).read())
        if not item:
            return {"skipped": "backlog empty"}
        rec["item"] = item
        branch = f"autopilot/{slug(item)}-{now().strftime('%Y%m%d')}"
        sh(["git", "checkout", "-b", branch], d).check_returncode()
        agent = sh(agent_argv(agent_prompt(item), model), d, env=agent_env(), timeout=3300)
        rec["agent_exit"] = agent.returncode
        tests = sh([sys.executable, "-m", "pytest", "-q"], d)
        rec["tests_exit"] = tests.returncode
        files = sh(["git", "status", "--porcelain"], d).stdout.splitlines()
        files = [ln[3:] for ln in files]
        bad = forbidden_paths(files)
        if bad:
            gate_violation(rec, "owner_only_paths", ", ".join(bad[:10]))
        if bad or not files or tests.returncode:
            return {"pr": None, "reason": "forbidden paths" if bad else ("no change" if not files else "tests red"),
                    "forbidden": bad}
        if dry_run():
            return {"pr": None, "reason": "dry-run", "files": files}
        sh(["git", "add", "-A"], d)
        sh(["git", "-c", "user.name=hotato-autopilot", "-c", "user.email=autopilot@users.noreply.github.com",
            "commit", "-m", item], d)
        tok = bot_token()  # re-read: an App installation token lasts 1 h, the agent may have used most of it
        sh(["git", "remote", "set-url", "origin", f"https://x-access-token:{tok}@github.com/{r}.git"], d)
        sh(["git", "push", "origin", branch], d).check_returncode()
        pr = gh(f"/repos/{r}/pulls", tok, "POST", {"title": item, "head": branch, "base": "main",
                                                  "body": f"Backlog item: {item}\n\nOpened by the builder job."})
        gh(f"/repos/{r}/issues/{pr['number']}/labels", tok, "POST", {"labels": ["autopilot"]})
        return {"pr": pr["number"], "files": files}


if __name__ == "__main__":
    main_guard("builder", work)
