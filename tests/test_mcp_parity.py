"""The MCP scoring tool must preserve the exact core envelope and add its
uniform control fields, and the server must register the expected tool set: the
``voice_eval_run`` scorer plus the read/verify/propose and clone-scoped fleet
tools. The envelope-parity check needs no MCP SDK (``_run_tool`` does not import
mcp); the registration check is skipped if the SDK is absent.
"""

import json

import pytest

from hotato.core import run_suite
from hotato import mcp_server


def test_run_tool_envelope_matches_core():
    response = mcp_server._run_tool(suite="barge-in", stack="generic")
    control = {
        key: response.pop(key)
        for key in (
            "evidence_status", "refusal_reason", "artifact_digests",
            "pending_irreversible_action",
        )
    }
    via_tool = json.dumps(response, sort_keys=True)
    via_core = json.dumps(run_suite(suite="barge-in", stack="generic"), sort_keys=True)
    assert via_tool == via_core
    assert control["evidence_status"] == 2
    assert control["refusal_reason"] is None
    assert control["pending_irreversible_action"] is None
    assert len(control["artifact_digests"]) == 8
    assert all(len(digest) == 64 for digest in control["artifact_digests"])


def test_expected_tools_registered():
    try:
        import mcp  # noqa: F401
    except Exception:
        pytest.skip("MCP SDK not installed; tool-registration check skipped")

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

    # The expected inventory is read from the ONE canonical source of truth
    # (mcp_server.TOOL_NAMES) rather than re-listed here, so a tool added to the
    # server and its inventory stays covered without editing this test. The
    # scoring tool plus the read/verify/propose and clone-scoped fleet tools;
    # none deploys to production.
    expected = set(mcp_server.TOOL_NAMES)
    fleet_tools = expected - {"voice_eval_run"}
    assert "voice_eval_run" in names, names
    assert fleet_tools.issubset(set(names)), names
    assert set(names) == expected, names


def test_initialize_reports_hotato_version():
    """serverInfo.version in the MCP initialize handshake must report hotato's
    application version, not the MCP SDK's own version. FastMCP does not forward
    an app version to its low-level Server, so ``build_server`` pins it; the
    server_version on the initialization options is exactly what populates
    serverInfo.version, at the floor SDK (mcp>=1.2.0) and current."""
    try:
        import mcp  # noqa: F401
    except Exception:
        pytest.skip("MCP SDK not installed; version check skipped")

    from hotato import __version__ as hotato_version

    server = mcp_server.build_server()
    opts = server._mcp_server.create_initialization_options()
    assert opts.server_name == "hotato", opts.server_name
    assert opts.server_version == hotato_version, opts.server_version
