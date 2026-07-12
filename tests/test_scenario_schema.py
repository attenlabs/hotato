"""Phase-2 anchor schema: scenario.v1 (the deterministic-simulation input).

Pins the honesty properties that are the point of this slice's scenario file: a
well-formed scenario validates against both the Python validator and the JSON
Schema; every malformed variant raises ``ValueError`` up front (never a partial
simulation); a scenario never carries an ``overall_score`` (it is an input, it
scores nothing) -- structurally, in both the validator and the schema; and the
caller can only ever declare its OWN turns (there is no agent-side field), so a
scenario can never solve the task for the agent.
"""

import copy
import json
from importlib import resources

import pytest

from hotato import scenario as SC

jsonschema = pytest.importorskip("jsonschema")


def _schema(name):
    return json.loads(
        resources.files("hotato").joinpath("schema", name).read_text(encoding="utf-8")
    )


def _validate_json(instance, schema_name="scenario.v1.json"):
    jsonschema.validate(instance=instance, schema=_schema(schema_name))


def _valid_scenario():
    return {
        "kind": "hotato.scenario",
        "version": 1,
        "id": "refund-basic",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "facts": {"order_id": "A-1001"},
        "caller": {
            "script": [
                {"say": "Hi, my order A-1001 arrived damaged and I want a refund."},
                {"when_agent_asks": "order_id", "say": "It is A-1001."},
                {"after": "confirmation", "say": "Thanks, that's all."},
            ],
            "behavior": {
                "speaking_rate": 1.0,
                "interruptions": [{"trigger": "greeting", "offset_ms": 800}],
                "backchannels": {"probability": 0.3},
            },
        },
        "environment": {"noise": "clean", "codec": "g711", "locale": "en-US"},
        "variation_matrix": {
            "locale": ["en-US", "es-ES"],
            "speaking_rate": [0.9, 1.1],
            "noise": ["clean", "cafe"],
            "repetitions": 2,
        },
        "seed": 7,
    }


# --------------------------------------------------------------------------
# valid documents
# --------------------------------------------------------------------------

def test_valid_scenario_validates_both_ways():
    doc = _valid_scenario()
    norm = SC.validate_scenario_doc(doc)
    assert norm["id"] == "refund-basic"
    assert norm["seed"] == 7
    _validate_json(doc)


def test_defaults_applied_when_absent():
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "minimal",
        "goal": {"type": "ask", "target": "hours"},
        "caller": {"script": [{"say": "What time do you open?"}]},
    }
    norm = SC.validate_scenario_doc(doc)
    # facts -> {}, seed -> 0, caller.behavior.speaking_rate -> 1.0
    assert norm["facts"] == {}
    assert norm["seed"] == 0
    assert norm["caller"]["behavior"]["speaking_rate"] == SC.DEFAULT_SPEAKING_RATE
    _validate_json(doc)


def test_load_scenario_file_json(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(_valid_scenario()), encoding="utf-8")
    doc = SC.load_scenario_file(str(p))
    assert doc["id"] == "refund-basic"


def test_load_scenario_file_yaml_subset(tmp_path):
    # The same dependency-free YAML subset the assertion / conversation-test
    # files use round-trips a scenario too (block mappings + block sequences).
    text = (
        "kind: hotato.scenario\n"
        "version: 1\n"
        "id: yaml-scenario\n"
        "goal:\n"
        "  type: get_refund\n"
        "  target: order A-1\n"
        "facts:\n"
        "  order_id: A-1\n"
        "caller:\n"
        "  script:\n"
        "    - say: \"My order A-1 is broken.\"\n"
        "  behavior:\n"
        "    speaking_rate: 1.0\n"
        "    interruptions:\n"
        "      - trigger: greeting\n"
        "        offset_ms: 500\n"
        "    backchannels:\n"
        "      probability: 0.0\n"
        "seed: 3\n"
    )
    p = tmp_path / "s.yaml"
    p.write_text(text, encoding="utf-8")
    doc = SC.load_scenario_file(str(p))
    assert doc["id"] == "yaml-scenario"
    assert doc["caller"]["behavior"]["interruptions"][0]["offset_ms"] == 500


# --------------------------------------------------------------------------
# malformed scenarios -> ValueError (each variant), up front, never partial
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("id"), "id"),
    (lambda d: d.pop("goal"), "goal"),
    (lambda d: d.pop("caller"), "caller"),
    (lambda d: d.__setitem__("kind", "hotato.wrong"), "kind"),
    (lambda d: d.__setitem__("version", 2), "version"),
    (lambda d: d["goal"].pop("target"), "goal.target"),
    (lambda d: d["caller"].__setitem__("script", []), "script"),
    (lambda d: d["caller"]["script"][0].pop("say"), "say"),
    (lambda d: d["caller"]["script"][1].__setitem__("after", "x"), "at most one"),
    (lambda d: d["caller"]["behavior"].__setitem__("speaking_rate", 0), "speaking_rate"),
    (lambda d: d["caller"]["behavior"].__setitem__("speaking_rate", -1), "speaking_rate"),
    (lambda d: d["caller"]["behavior"]["interruptions"][0].pop("offset_ms"), "offset_ms"),
    (lambda d: d["caller"]["behavior"]["backchannels"].__setitem__("probability", 1.5), "probability"),
    (lambda d: d["variation_matrix"].__setitem__("repetitions", 0), "repetitions"),
    (lambda d: d["variation_matrix"].__setitem__("speaking_rate", ["fast"]), "speaking_rate"),
    (lambda d: d.__setitem__("seed", -1), "seed"),
    (lambda d: d.__setitem__("seed", 1.5), "seed"),
    (lambda d: d.__setitem__("overall_score", 0.9), "overall_score"),
])
def test_malformed_scenario_raises(mutate, frag):
    doc = _valid_scenario()
    mutate(doc)
    with pytest.raises(ValueError) as exc:
        SC.validate_scenario_doc(doc)
    assert frag in str(exc.value)


# --------------------------------------------------------------------------
# honesty invariant: no overall_score (validator AND schema)
# --------------------------------------------------------------------------

def test_scenario_schema_rejects_overall_score():
    good = _valid_scenario()
    _validate_json(good)  # sanity: valid without it
    bad = copy.deepcopy(good)
    bad["overall_score"] = 0.9
    with pytest.raises(jsonschema.ValidationError):
        _validate_json(bad)
    with pytest.raises(ValueError, match="overall_score"):
        SC.validate_scenario_doc(bad)


def test_caller_turn_has_no_agent_field():
    # Structural guarantee that a scenario can only declare the CALLER's words:
    # the turn schema's only required field is `say`, and there is no field for
    # an agent utterance. (The renderer + validate_simulation re-check this at
    # run time; here we pin that the SCHEMA offers no agent-side hook.)
    turn_schema = _schema("scenario.v1.json")["definitions"]["turn"]
    assert turn_schema["required"] == ["say"]
    assert "agent" not in turn_schema["properties"]
    assert "reply" not in turn_schema["properties"]
