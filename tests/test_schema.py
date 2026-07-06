"""M2: the agent-facing contract is FROZEN and self-validating.

Every shape the tool emits (suite, single pass, single fail, and the one MCP
tool) validates against the shipped JSON Schema; the suite output is byte-stable
against a checked-in golden (modulo engine version); and the schema itself
enforces the honesty invariant (accuracy_claim must be null) and the envelope
exit-code contract (only 0 or 1).
"""

import json
import os
from importlib import resources

import pytest

jsonschema = pytest.importorskip("jsonschema")

from hotato import mcp_server
from hotato.core import run_single, run_suite

_HERE = os.path.dirname(os.path.abspath(__file__))


def _schema():
    return json.loads(
        resources.files("hotato")
        .joinpath("schema", "envelope.v1.json")
        .read_text(encoding="utf-8")
    )


def _validate(env):
    jsonschema.validate(instance=env, schema=_schema())


def _norm(env):
    env = json.loads(json.dumps(env))
    env["engine"]["version"] = "*"
    return env


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def test_suite_envelope_validates():
    _validate(run_suite(suite="barge-in"))


def test_single_pass_envelope_validates():
    _validate(run_single(stereo=_bundled("01-hard-interruption"), expect="yield"))


def test_single_fail_envelope_validates():
    env = run_single(
        stereo=_bundled("01-hard-interruption"), expect="yield",
        stack="livekit", max_time_to_yield_sec=0.0,
    )
    assert env["exit_code"] == 1
    _validate(env)


def test_mcp_tool_envelope_validates():
    _validate(mcp_server._run_tool(suite="barge-in", stack="generic"))


def test_golden_suite_is_byte_stable():
    with open(os.path.join(_HERE, "golden", "suite_barge-in.json"), encoding="utf-8") as fh:
        golden = json.load(fh)
    got = _norm(run_suite(suite="barge-in"))
    # compare canonically so key order can never cause a false diff
    assert json.dumps(got, sort_keys=True) == json.dumps(golden, sort_keys=True)


def test_schema_rejects_fabricated_accuracy_claim():
    """The honesty invariant is enforced BY the schema, not just by convention."""
    import copy
    env = copy.deepcopy(run_suite(suite="barge-in"))
    env["limits"]["accuracy_claim"] = 0.95  # a lie
    with pytest.raises(jsonschema.ValidationError):
        _validate(env)


def test_schema_rejects_non_envelope_exit_code():
    env = run_suite(suite="barge-in")
    env["exit_code"] = 2  # 2 is a CLI usage error, never an envelope value
    with pytest.raises(jsonschema.ValidationError):
        _validate(env)


def test_schema_rejects_bad_fix_class():
    env = run_single(
        stereo=_bundled("01-hard-interruption"), expect="yield",
        stack="livekit", max_time_to_yield_sec=0.0,
    )
    env["fix_map"][0]["fix_class"] = "upsell"  # not an allowed class
    with pytest.raises(jsonschema.ValidationError):
        _validate(env)
