"""render_twiml: the FIXED-TIMELINE scripted caller. Rendering is pure + offline
(no network), so these run without any server."""
import pytest

from hotato import drive


def _scenario(script, behavior=None):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "s-twiml",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "caller": {"script": script},
    }
    if behavior is not None:
        doc["caller"]["behavior"] = behavior
    return doc


def test_renders_one_say_per_turn_wrapped_in_response():
    twiml = drive.render_twiml(_scenario([{"say": "Hello there"}, {"say": "I need a refund"}]))
    assert twiml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert twiml.count("<Response>") == 1 and twiml.endswith("</Response>")
    assert "<Say>Hello there</Say>" in twiml
    assert "<Say>I need a refund</Say>" in twiml
    assert twiml.count("<Say>") == 2


def test_escapes_xml_metacharacters_in_say_text():
    raw = 'Tom & <Jerry> said "hi" it\'s fine'
    twiml = drive.render_twiml(_scenario([{"say": raw}]))
    # every metacharacter is escaped; the raw unescaped text never appears
    assert "&amp;" in twiml and "&lt;Jerry&gt;" in twiml
    assert "&quot;hi&quot;" in twiml and "it&apos;s" in twiml
    assert "<Jerry>" not in twiml
    assert 'Tom & <Jerry>' not in twiml


def test_inserts_lead_in_and_inter_turn_pauses():
    twiml = drive.render_twiml(_scenario([{"say": "one"}, {"say": "two"}, {"say": "three"}]))
    # a lead-in pause + one between each of the three turns = 3 pauses
    assert twiml.count("<Pause") == 3
    assert '<Pause length="1"/>' in twiml


def test_explicit_pause_before_ms_overrides_the_default_gap():
    twiml = drive.render_twiml(_scenario(
        [{"say": "first"}, {"say": "second", "pause_before_ms": 2000}]))
    # the second turn's 2000ms explicit pause rounds to a 2s TwiML Pause
    assert '<Pause length="2"/>' in twiml


def test_fixed_timeline_ignores_reactive_label_triggers():
    # a fixed-timeline TwiML <Say> caller cannot react, so a turn's reactive
    # label (when_agent_asks / after) is NOT rendered -- only the say text is
    # spoken, unconditionally, and the label string never leaks into the TwiML.
    twiml = drive.render_twiml(_scenario([
        {"say": "A-1001", "when_agent_asks": "order_id"},
        {"say": "yes please", "after": "confirm_step"},
    ]))
    assert "<Say>A-1001</Say>" in twiml
    assert "<Say>yes please</Say>" in twiml
    assert "order_id" not in twiml and "confirm_step" not in twiml


def test_voice_and_language_attributes_are_carried_and_escaped():
    twiml = drive.render_twiml(_scenario([{"say": "hi"}]), voice="Polly.Joanna",
                               language="en-US")
    assert '<Say voice="Polly.Joanna" language="en-US">hi</Say>' in twiml


def test_malformed_scenario_raises_valueerror():
    # empty caller.script is rejected by the scenario validator BEFORE any TwiML
    with pytest.raises(ValueError):
        drive.render_twiml(_scenario([]))
    with pytest.raises(ValueError):
        drive.render_twiml({"kind": "hotato.scenario", "version": 1, "id": "x"})
