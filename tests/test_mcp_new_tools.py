"""The plan-§17 MCP expansion: the three new fleet tools plus the uniform
control envelope on EVERY tool response.

Two things are pinned here:

  * ``candidate_inspect`` / ``experiment_status`` / ``experiment_create`` are
    REGISTERED on the built server and CALLABLE against a seeded workspace. The
    ``mcp`` extra is not a test dependency (mirroring ``test_mcp_fleet_tools``),
    so a minimal fake ``FastMCP`` captures the registrations without it -- this
    tests exactly what ``build_server`` wires up.
  * every tool response (pure reads included) carries the four envelope keys:
    ``evidence_status``, ``refusal_reason``, ``artifact_digests``,
    ``pending_irreversible_action``.
"""
import json
import sys
import types

import pytest

from hotato import core
from hotato import mcp_server as m
from hotato.fleet.api import FleetAPI
from tests import _trial_audio as ta

_ENVELOPE_KEYS = ("evidence_status", "refusal_reason", "artifact_digests",
                  "pending_irreversible_action")


# --- a minimal FastMCP stand-in so build_server runs without the mcp extra ---

class _FakeFastMCP:
    """Records ``@server.tool`` registrations; enough of the FastMCP surface for
    ``build_server`` to run and be introspected."""

    def __init__(self, name):
        self.name = name
        self.tools = {}

    def tool(self, name=None, description=None):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn
        return deco

    def run(self):  # pragma: no cover - transport is never started in tests
        raise AssertionError("the stdio transport must not start in a test")


@pytest.fixture()
def fake_mcp():
    """Install the fake ``mcp.server.fastmcp`` module for the duration of a test,
    then restore whatever was there before."""
    saved = {k: sys.modules.get(k)
             for k in ("mcp", "mcp.server", "mcp.server.fastmcp")}
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FakeFastMCP
    server_mod.fastmcp = fastmcp_mod
    mcp_mod.server = server_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = server_mod
    sys.modules["mcp.server.fastmcp"] = fastmcp_mod
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# --- seeding (mirrors test_mcp_fleet_tools._populate) -----------------------

def _seed(home, tmp_path):
    """Create a workspace + agent + one discovered candidate in a tmp home."""
    api = FleetAPI(home=home)
    api.init_workspace("default")
    api.agent_add("default", "bot", stack="vapi")
    wav = str(tmp_path / "c.wav"); ta.talkover_call(wav)
    ing = api.ingest_recording("default", "bot", wav)
    disc = api.discover("default", "bot", wav, recording_id=ing["recording_id"])
    api.close()
    return disc


def _battery_run_json(tmp_path):
    """A committed battery envelope on disk (a scored suite), the input
    ``experiment_create`` precommits a manifest from."""
    bdir = tmp_path / "battery"
    bdir.mkdir()
    env = core.run_suite(suite="barge-in")
    json.dump(env, open(bdir / "run.json", "w"))
    return str(bdir)


# --- registration + callability --------------------------------------------

def test_new_tools_are_registered_on_the_built_server(fake_mcp):
    server = m.build_server()
    for name in ("candidate_inspect", "experiment_status", "experiment_create"):
        assert name in server.tools, f"{name} was not registered"
    # and the previously-shipped tools are still there
    for name in ("voice_eval_run", "fleet_status", "candidate_list",
                 "contract_list", "trial_explain", "artifact_verify",
                 "experiment_propose", "experiment_run", "clone_cleanup"):
        assert name in server.tools


def test_candidate_inspect_returns_components_and_trust(fake_mcp, tmp_path):
    home = str(tmp_path / "home")
    disc = _seed(home, tmp_path)
    cand_id = disc["candidates"][0]["candidate_id"]

    server = m.build_server()
    out = server.tools["candidate_inspect"](home=home, workspace_id="default",
                                            candidate_id=cand_id)
    assert out["found"] is True
    assert out["candidate_id"] == cand_id
    # the stored measured components are surfaced by name
    for key in ("severity", "input_health", "recurrence", "novelty",
                "covered_by_contract"):
        assert key in out["components"]
    # trust findings ride along (input health is the discover-time trust signal)
    assert "input_health" in out["trust"]
    assert "human must label" in out["note"]     # never auto-labels

    # a missing candidate is a clean found=False, still enveloped
    miss = server.tools["candidate_inspect"](home=home, candidate_id="cand-nope")
    assert miss["found"] is False
    _assert_envelope(miss)


def test_experiment_status_reads_verdict_and_gate(fake_mcp, tmp_path):
    home = str(tmp_path / "home")
    api = FleetAPI(home=home)
    api.init_workspace("default")
    api.registry.add_trial("default", "t1", agent_id="bot", verdict="improved",
                           evidence_tier=3, manifest_hash="mh-abc",
                           manifest_digest="dg-abc")
    api.registry.add_decision("default", "d1", trial_id="t1",
                              recommendation="approval required", approved=0)
    api.close()

    server = m.build_server()
    out = server.tools["experiment_status"](home=home, workspace_id="default",
                                            trial_id="t1")
    assert out["found"] is True
    assert out["verdict"] == "improved"
    assert out["evidence_tier"] == 3
    assert out["manifest_hash"] == "mh-abc"
    assert out["recommendation"] == "approval required"
    # an improved trial names the human-gated deployment as still pending
    assert out["pending_irreversible_action"] is not None
    assert out["evidence_status"] == 3
    assert out["artifact_digests"] == ["dg-abc"]

    miss = server.tools["experiment_status"](home=home, trial_id="nope")
    assert miss["found"] is False
    _assert_envelope(miss)


def test_experiment_create_precommits_manifest_without_deploying(fake_mcp, tmp_path):
    home = str(tmp_path / "home")
    api = FleetAPI(home=home)
    api.init_workspace("default")
    api.agent_add("default", "bot", stack="mock")
    api.close()
    battery = _battery_run_json(tmp_path)

    server = m.build_server()
    out = server.tools["experiment_create"](home=home, workspace_id="default",
                                            agent_id="bot", trial_id="t-create",
                                            battery_path=battery, min_n=1)
    assert out["ok"] is True
    assert out["trial_id"] == "t-create"
    assert out["manifest_hash"]
    assert out["manifest_digest"]
    assert out["fixtures"]                    # the pinned fixture universe
    # precommit only: nothing scored yet, so no deployment is pending, and the
    # manifest digest is surfaced as the touched artifact
    assert out["pending_irreversible_action"] is None
    assert out["artifact_digests"] == [out["manifest_digest"]]

    # the trial is recorded as "created" (not deployed)
    status = server.tools["experiment_status"](home=home, trial_id="t-create")
    assert status["found"] is True
    assert status["verdict"] == "created"


# --- the uniform envelope on EVERY tool response ----------------------------

def _assert_envelope(resp):
    assert isinstance(resp, dict)
    for key in _ENVELOPE_KEYS:
        assert key in resp, f"response missing envelope key {key!r}: {resp.get('kind')}"
    assert isinstance(resp["artifact_digests"], list)


def test_every_tool_response_carries_the_four_envelope_keys(fake_mcp, tmp_path):
    home = str(tmp_path / "home")
    disc = _seed(home, tmp_path)
    cand_id = disc["candidates"][0]["candidate_id"]
    battery = _battery_run_json(tmp_path)

    server = m.build_server()

    # a representative call for every fleet tool the server exposes
    calls = {
        "fleet_status": lambda: server.tools["fleet_status"](home=home),
        "candidate_list": lambda: server.tools["candidate_list"](home=home),
        "candidate_inspect": lambda: server.tools["candidate_inspect"](
            home=home, candidate_id=cand_id),
        "contract_list": lambda: server.tools["contract_list"](home=home),
        "trial_explain": lambda: server.tools["trial_explain"](
            home=home, trial_id="none"),
        "experiment_status": lambda: server.tools["experiment_status"](
            home=home, trial_id="none"),
        "experiment_propose": lambda: server.tools["experiment_propose"](
            agent_id="bot", contract_id="c1"),
        "experiment_create": lambda: server.tools["experiment_create"](
            home=home, agent_id="bot", trial_id="t-env", battery_path=battery),
        "clone_cleanup": lambda: server.tools["clone_cleanup"]("mock", "c1", home),
    }
    for name, call in calls.items():
        resp = call()
        _assert_envelope(resp)

    # the pure reads carry NO verdict, so evidence_status is null for them
    assert server.tools["fleet_status"](home=home)["evidence_status"] is None
    assert server.tools["candidate_list"](home=home)["evidence_status"] is None
    # a bad battery path is refused with a populated refusal_reason
    bad = server.tools["experiment_create"](home=home, agent_id="bot",
                                            trial_id="t-bad",
                                            battery_path="/nonexistent/battery")
    assert bad["ok"] is False
    assert bad["refusal_reason"]
