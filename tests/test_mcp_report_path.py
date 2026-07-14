"""P10: the optional `report_path` param on the MCP scoring tool. When set, the
self-contained HTML report is written there and the envelope carries the
absolute path; everything else in the envelope stays byte-identical to a plain
run (additive only). When unset, behavior is untouched (pinned separately by
test_mcp_parity).
"""

import json
import os
from importlib import resources

from hotato import mcp_server
from hotato.core import run_single, run_suite

_CONTROL_FIELDS = (
    "evidence_status", "refusal_reason", "artifact_digests",
    "pending_irreversible_action",
)


def _core(response):
    result = dict(response)
    for key in _CONTROL_FIELDS:
        result.pop(key)
    return result


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def test_report_path_writes_report_and_keeps_envelope_parity_suite(tmp_path):
    out = tmp_path / "suite-report.html"
    env = mcp_server._run_tool(suite="barge-in", stack="generic",
                               report_path=str(out))
    # the report landed and is the real self-contained page
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert html.count('<svg class="tl-svg"') == 8
    assert "%" not in html
    # the envelope points at it, absolutely
    assert env["report_path"] == os.path.abspath(str(out))
    # and the envelope CORE is byte-identical to a plain run
    core = _core(env)
    core.pop("report_path")
    assert json.dumps(core, sort_keys=True) == json.dumps(
        run_suite(suite="barge-in", stack="generic"), sort_keys=True)


def test_report_path_parity_single_recording(tmp_path):
    wav = _bundled("01-hard-interruption")
    out = tmp_path / "single-report.html"
    env = mcp_server._run_tool(stereo=wav, stack="generic", expect="yield",
                               report_path=str(out))
    assert out.exists()
    core = _core(env)
    assert core.pop("report_path") == os.path.abspath(str(out))
    assert json.dumps(core, sort_keys=True) == json.dumps(
        run_single(stereo=wav, stack="generic", expect="yield"), sort_keys=True)


def test_no_report_path_means_no_report_key(tmp_path):
    env = mcp_server._run_tool(suite="barge-in", stack="generic")
    assert "report_path" not in env
