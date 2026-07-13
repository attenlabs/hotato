"""hotato.interaction-label.v1: supplied, backwards-compatible, never inferred."""
import inspect
import json

import pytest

from hotato import interaction_label as IL


def _schema():
    from importlib import resources
    with resources.files("hotato").joinpath(
            "schema", "interaction-label.v1.json").open(encoding="utf-8") as fh:
        return json.load(fh)


# --- the python validator and the JSON Schema agree -------------------------

def test_python_validator_matches_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema()
    validator = jsonschema.Draft202012Validator(schema)
    good = [
        IL.build(speech_presence="speech", addressed_to_agent=True,
                 floor_intent="take", label_authority="human"),
        IL.build(speech_presence="speech", addressed_to_agent=False,
                 floor_intent="feedback", label_authority="trusted-source"),
        IL.build(speech_presence="non-speech", label_authority="fixture"),
        dict(IL.UNKNOWN),
    ]
    for doc in good:
        validator.validate(doc)          # schema accepts
        assert IL.validate(doc) == doc   # python accepts
    bad = [
        {"kind": "wrong", "speech_presence": "speech", "addressed_to_agent": None,
         "floor_intent": "none", "label_authority": "human"},
        {"kind": IL.KIND, "speech_presence": "maybe", "addressed_to_agent": None,
         "floor_intent": "none", "label_authority": "human"},
        {"kind": IL.KIND, "speech_presence": "speech", "addressed_to_agent": "yes",
         "floor_intent": "take", "label_authority": "human"},
    ]
    for doc in bad:
        assert not validator.is_valid(doc)          # schema rejects
        with pytest.raises(IL.InteractionLabelError):
            IL.validate(doc)                         # python rejects


# --- conditional rules ------------------------------------------------------

def test_non_speech_forces_null_addressee_and_none_intent():
    lab = IL.build(speech_presence="non-speech", addressed_to_agent=True,
                   floor_intent="take", label_authority="human")
    assert lab["addressed_to_agent"] is None
    assert lab["floor_intent"] == "none"
    # and a hand-built non-speech with an addressee is refused
    with pytest.raises(IL.InteractionLabelError):
        IL.validate({"kind": IL.KIND, "speech_presence": "non-speech",
                     "addressed_to_agent": True, "floor_intent": "none",
                     "label_authority": "human"})


def test_unknown_authority_degrades_every_judged_field():
    lab = IL.build(speech_presence="speech", addressed_to_agent=True,
                   floor_intent="take", label_authority="unknown")
    assert lab["speech_presence"] == "unknown"
    assert lab["addressed_to_agent"] is None
    assert lab["floor_intent"] == "unknown"


def test_bad_enums_and_extra_fields_refused():
    with pytest.raises(IL.InteractionLabelError):
        IL.build(label_authority="human", floor_intent="grab")
    with pytest.raises(IL.InteractionLabelError):
        IL.build(label_authority="model")
    with pytest.raises(IL.InteractionLabelError):
        IL.build(label_authority="human", speech_presence="loud")
    with pytest.raises(IL.InteractionLabelError):
        IL.validate({**IL.UNKNOWN, "sneaky": 1})
    with pytest.raises(IL.InteractionLabelError):
        IL.validate({**IL.UNKNOWN, "label_ref": "x" * 513})


# --- backwards compatibility ------------------------------------------------

def test_absent_label_reads_as_unknown():
    assert IL.of({}) == IL.UNKNOWN
    assert IL.of(None) == IL.UNKNOWN
    assert IL.of({"other": "fields", "no": "label"}) == IL.UNKNOWN


def test_attach_is_additive_and_round_trips():
    carrier = {"schema": "hotato.label-record.v1", "decision": "yield"}
    lab = IL.build(speech_presence="speech", addressed_to_agent=True,
                   floor_intent="take", label_authority="human",
                   label_ref="reviewer:alice#e12")
    IL.attach(carrier, lab)
    assert carrier["decision"] == "yield"          # nothing else changed
    assert carrier["schema"] == "hotato.label-record.v1"
    assert IL.of(carrier) == lab                    # round-trips


# --- the whole point: nothing is inferred -----------------------------------

def test_labels_are_supplied_never_inferred():
    # build() takes only explicit categorical values: no audio, pcm, frames,
    # timing, energy, transcript, verdict, or model parameter can reach it.
    params = set(inspect.signature(IL.build).parameters)
    forbidden = {"audio", "pcm", "frames", "wav", "timing", "energy", "onset",
                 "transcript", "text", "verdict", "judge", "model", "score",
                 "rms", "signal", "measurement"}
    assert not (params & forbidden), f"build must not accept a signal: {params & forbidden}"
    # the whole module must not import a scorer/asr/judge to derive a label
    src = inspect.getsource(IL)
    for banned in ("from .core", "from .analyze", "from .rubric",
                   "from .transcribe", "import core", "transcribe(", "evaluate("):
        assert banned not in src, f"interaction_label must not derive labels via {banned!r}"
