"""Release guard: the human MCP docs must describe the MCP tools that
``mcp_server.py`` actually registers, and must not undercount them.

Motivation: the tool set grew from a single scoring tool (``voice_eval_run``)
to that scorer plus the fleet read/verify/propose tools, and the prose docs
drifted behind the code (a doc still said the server "exposes exactly one
tool"). ``mcp_server.py`` is the source of truth here: this test parses the
``@...tool(...)`` decorators out of it -- it never hardcodes the count -- and
asserts that

  1. every registered tool name is named in ``docs/MCP.md``, and
  2. no human MCP doc claims "one tool" (or "exactly one tool") when more than
     one tool is in fact registered.

If a tool is added or renamed, update ``docs/MCP.md`` (and the other human docs
below) rather than editing this test to match stale prose.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = REPO_ROOT / "src" / "hotato" / "mcp_server.py"
MCP_DOC = REPO_ROOT / "docs" / "MCP.md"

# The human-authored docs that describe the MCP tool surface. Generated
# artifacts (llms-full.txt), historical release notes (CHANGELOG.md), and the
# error-envelope prose in src (which refers to "the one MCP tool" that shares
# the error shape, a different claim) are deliberately out of scope.
HUMAN_MCP_DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "MCP.md",
    REPO_ROOT / "docs" / "API.md",
    REPO_ROOT / "llms.txt",
]

# Phrases that assert the server exposes a single tool. Case-insensitive.
UNDERCOUNT_PATTERNS = [
    re.compile(r"\bexactly one tool\b", re.IGNORECASE),
    re.compile(r"\bone tool\b", re.IGNORECASE),
]


def _registered_tool_names() -> list[str]:
    """Every tool name registered via a ``@<something>.tool(...)`` decorator in
    mcp_server.py. Prefers the decorator's ``name=`` keyword (the actual
    registered name); falls back to the decorated function's name."""
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            # Match @<x>.tool(...) and, defensively, a bare @<x>.tool.
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                    and dec.func.attr == "tool":
                registered = node.name
                for kw in dec.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        registered = kw.value.value
                names.append(registered)
                break
            if isinstance(dec, ast.Attribute) and dec.attr == "tool":
                names.append(node.name)
                break
    return names


def test_at_least_one_tool_is_registered():
    names = _registered_tool_names()
    assert names, "no @...tool decorators found in mcp_server.py -- parser drift?"


def test_every_registered_tool_is_named_in_mcp_doc():
    names = _registered_tool_names()
    doc = MCP_DOC.read_text(encoding="utf-8")
    missing = [n for n in names if n not in doc]
    assert not missing, (
        f"docs/MCP.md does not name these registered MCP tools: {missing}. "
        f"Registered: {sorted(set(names))}. Add them to docs/MCP.md."
    )


def test_voice_eval_run_is_registered_and_documented():
    # The scoring tool is the primary surface; it must always be present.
    names = _registered_tool_names()
    assert "voice_eval_run" in names, (
        f"voice_eval_run is no longer a registered MCP tool; registered: {names}"
    )
    assert "voice_eval_run" in MCP_DOC.read_text(encoding="utf-8")


def test_no_human_doc_undercounts_the_tools():
    names = _registered_tool_names()
    if len(names) <= 1:
        # A genuine single-tool claim would be accurate; nothing to guard.
        return
    offenders = []
    for doc in HUMAN_MCP_DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for pat in UNDERCOUNT_PATTERNS:
            m = pat.search(text)
            if m:
                offenders.append(f"{doc.relative_to(REPO_ROOT)}: '{m.group(0)}'")
    assert not offenders, (
        f"{len(names)} MCP tools are registered but a human doc still claims a "
        f"single tool: {offenders}. Update the prose to describe the full tool "
        f"set (the scorer plus the fleet tools)."
    )
