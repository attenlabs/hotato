"""The one-tool MCP surface must return the EXACT same envelope as the core, and
must register exactly one tool. The envelope-parity check needs no MCP SDK
(``_run_tool`` does not import mcp); the one-tool check is skipped if the SDK is
absent.
"""

import json

import pytest

from hotato.core import run_suite
from hotato import mcp_server


def test_run_tool_envelope_matches_core():
    via_tool = json.dumps(
        mcp_server._run_tool(suite="barge-in", stack="generic"), sort_keys=True
    )
    via_core = json.dumps(run_suite(suite="barge-in", stack="generic"), sort_keys=True)
    assert via_tool == via_core


def test_exactly_one_tool_registered():
    try:
        import mcp  # noqa: F401
    except Exception:
        pytest.skip("MCP SDK not installed; one-tool registration check skipped")

    server = mcp_server.build_server()
    # FastMCP exposes registered tools via an async list_tools(); fall back to the
    # tool manager if the async API shape differs across SDK versions.
    names = None
    try:
        import asyncio

        tools = asyncio.run(server.list_tools())
        names = [t.name for t in tools]
    except Exception:
        mgr = getattr(server, "_tool_manager", None)
        if mgr is not None:
            listed = mgr.list_tools()
            names = [getattr(t, "name", None) for t in listed]
    assert names is not None, "could not introspect registered MCP tools"
    assert names == ["voice_eval_run"], names
