"""The MCP scoring tool must return the EXACT same envelope as the core, and the
server must register the expected tool set: the ``voice_eval_run`` scorer plus
the eight read/verify/propose and clone-scoped fleet tools. The envelope-parity
check needs no MCP SDK (``_run_tool`` does not import mcp); the registration
check is skipped if the SDK is absent.
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

    # One scoring tool + eight fleet tools (read/verify/propose plus the
    # clone-scoped experiment_run/clone_cleanup; no production deployment).
    fleet_tools = {
        "fleet_status",
        "candidate_list",
        "contract_list",
        "trial_explain",
        "artifact_verify",
        "experiment_propose",
        "experiment_run",
        "clone_cleanup",
    }
    expected = {"voice_eval_run"} | fleet_tools
    assert "voice_eval_run" in names, names
    assert fleet_tools.issubset(set(names)), names
    assert set(names) == expected, names
