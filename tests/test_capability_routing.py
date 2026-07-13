"""Delta D3: neutral capability routing (hotato.capability-requirement.v1).

Pins the delta contract's eight required cases, driven from the checked-in
routing fixtures under ``tests/data/routing/``, plus:

  * every emitted requirement validates against the shipped JSON Schema; and
  * a model-generated (untrusted authority) label cannot satisfy the
    utterance_addressee_gate eligibility.

The router reads SUPPLIED interaction labels; it never infers addressee/intent.
"""
import json
import os
from importlib import resources

import pytest

from hotato import capability_routing as cr

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROUTING = os.path.join(_HERE, "data", "routing")


def _fixture(name):
    with open(os.path.join(_ROUTING, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


# ---- fixture roles -------------------------------------------------------

# A: the addressed floor bid the agent failed to yield to (shared by cases 1-5).
ADDRESSED_MISS = "addressed-interruption"
# B candidates: the opposite-risk event that varies per case.
NON_ADDRESSED = "non-addressed-speech"
ADDRESSED_BACKCHANNEL = "addressed-backchannel"
UNKNOWN_ADDRESSEE = "unknown-addressee"
NON_SPEECH_AMBIENT = "non-speech-ambient"
SELF_ECHO = "self-echo"
UNSCORABLE = "unscorable-non-addressed"


def _pair(a_name, b_name):
    return [_fixture(a_name), _fixture(b_name)]


# ---- JSON Schema cross-check --------------------------------------------

def _schema():
    return json.loads(
        resources.files("hotato")
        .joinpath("schema", "capability-requirement.v1.json")
        .read_text(encoding="utf-8")
    )


def _assert_schema_valid(requirement):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=requirement, schema=_schema())


# ---- the eight required cases -------------------------------------------

def test_case1_addressed_miss_plus_non_addressed_speech_routes_to_gate():
    req = cr.route_capability(_pair(ADDRESSED_MISS, NON_ADDRESSED))
    assert req is not None
    assert req["kind"] == "hotato.capability-requirement.v1"
    assert req["fix_class"] == "engagement-control"
    assert req["required_capability"] == "utterance_addressee_gate"
    assert req["trigger"] == "paired_discrimination_failure"
    assert req["evidence_refs"] == [ADDRESSED_MISS, NON_ADDRESSED]
    assert req["acceptance_tests"] == [
        "addressed_interruption_reaches_agent",
        "non_addressed_speech_does_not_reach_agent",
        "opposite_risk_fixture_does_not_regress",
    ]
    assert req["missing_evidence"] == []
    # the input-health causes checked and cleared across the pair.
    assert req["excluded_causes"] == [
        "self_echo", "non_speech_ambient", "invalid_channel_map",
        "unscorable_input",
    ]
    _assert_schema_valid(req)


def test_case2_addressed_miss_plus_addressed_feedback_routes_to_intent():
    req = cr.route_capability(_pair(ADDRESSED_MISS, ADDRESSED_BACKCHANNEL))
    assert req is not None
    assert req["required_capability"] == "turn_intent_discriminator"
    assert req["fix_class"] == "engagement-control"  # umbrella class kept
    assert req["trigger"] == "paired_discrimination_failure"
    assert req["evidence_refs"] == [ADDRESSED_MISS, ADDRESSED_BACKCHANNEL]
    assert req["acceptance_tests"] == [
        "addressed_feedback_does_not_take_floor",
        "opposite_risk_fixture_does_not_regress",
    ]
    assert req["missing_evidence"] == []
    _assert_schema_valid(req)


def test_case3_addressed_miss_plus_unknown_addressee_routes_to_engagement():
    req = cr.route_capability(_pair(ADDRESSED_MISS, UNKNOWN_ADDRESSEE))
    assert req is not None
    assert req["required_capability"] == "engagement_control"
    assert req["trigger"] == "insufficient_labels"
    # every trusted axis absent on the unknown event is named.
    assert req["missing_evidence"] == [
        "addressed_to_agent", "floor_intent", "label_authority",
    ]
    assert req["acceptance_tests"] == ["collect_trusted_addressee_label"]
    _assert_schema_valid(req)


def test_case4_addressed_miss_plus_non_speech_ambient_no_capability():
    # A non-speech ambient VAD trigger is a config / input-health finding.
    assert cr.route_capability(_pair(ADDRESSED_MISS, NON_SPEECH_AMBIENT)) is None


def test_case5_addressed_miss_plus_echo_no_capability():
    # Agent playback echo is a routing / AEC finding, not a capability.
    assert cr.route_capability(_pair(ADDRESSED_MISS, SELF_ECHO)) is None


def test_case6_unscorable_mixed_channel_pair_no_recommendation():
    assert cr.route_capability(_pair(ADDRESSED_MISS, UNSCORABLE)) is None
    # a pair where BOTH events are unscorable also yields no recommendation.
    both = [_fixture(UNSCORABLE), _fixture(UNSCORABLE)]
    assert cr.route_capability(both) is None


def test_case7_lone_event_makes_no_paired_claim():
    # A single event, whether passed bare or as a one-item battery.
    assert cr.route_capability(_fixture(ADDRESSED_MISS)) is None
    assert cr.route_capability([_fixture(ADDRESSED_MISS)]) is None
    assert cr.route_capability([_fixture(NON_ADDRESSED)]) is None


_VENDOR_TOKENS = (
    "saa", "attenlabs", "attention labs", "attentionlabs",
    "vapi", "retell", "livekit", "pipecat", "twilio", "krisp",
)


def _emit_all_verdicts():
    """Every non-None verdict the router can emit, for the neutrality sweep."""
    verdicts = []
    for pair in (
        _pair(ADDRESSED_MISS, NON_ADDRESSED),
        _pair(ADDRESSED_MISS, ADDRESSED_BACKCHANNEL),
        _pair(ADDRESSED_MISS, UNKNOWN_ADDRESSEE),
    ):
        # once without and once with the optional neutral contract URI.
        verdicts.append(cr.route_capability(pair))
        verdicts.append(
            cr.route_capability(pair, contract_uri=cr.DEFAULT_CONTRACT_URI)
        )
    return [v for v in verdicts if v is not None]


def test_case8_no_provider_or_product_token_in_any_verdict():
    verdicts = _emit_all_verdicts()
    assert verdicts, "expected at least one emitted verdict to scan"
    for verdict in verdicts:
        blob = json.dumps(verdict).lower()
        for token in _VENDOR_TOKENS:
            assert token not in blob, (
                "vendor/product token " + repr(token) + " leaked into a verdict"
            )
        # any URI present must be the neutral Hotato spec host, never a
        # product/provider URL.
        uri = verdict.get("contract_uri")
        if uri is not None:
            assert uri.startswith("https://hotato.dev/"), uri


def test_optional_contract_uri_is_neutral_and_schema_valid():
    plain = cr.route_capability(_pair(ADDRESSED_MISS, NON_ADDRESSED))
    assert "contract_uri" not in plain  # omitted by default

    with_uri = cr.route_capability(
        _pair(ADDRESSED_MISS, NON_ADDRESSED),
        contract_uri=cr.DEFAULT_CONTRACT_URI,
    )
    assert with_uri["contract_uri"] == cr.DEFAULT_CONTRACT_URI
    assert with_uri["contract_uri"].startswith("https://hotato.dev/")
    _assert_schema_valid(with_uri)


def test_every_emitted_requirement_validates_against_schema():
    for verdict in _emit_all_verdicts():
        _assert_schema_valid(verdict)


def test_model_generated_label_cannot_satisfy_gate_eligibility():
    """A non-addressed false trigger whose label authority is not human /
    trusted-source / fixture (i.e. a model-generated advisory reading) must NOT
    resolve utterance_addressee_gate; it degrades to engagement_control."""
    b = _fixture(NON_ADDRESSED)
    # perfect gate-B shape (addressed=false, floor=none) but untrusted authority.
    b["interaction"]["label_authority"] = "unknown"
    b["interaction"]["label_ref"] = "model advisory reading, not a trusted label"

    req = cr.route_capability([_fixture(ADDRESSED_MISS), b])
    assert req is not None
    assert req["required_capability"] == "engagement_control"
    assert req["trigger"] == "insufficient_labels"
    assert "label_authority" in req["missing_evidence"]
    _assert_schema_valid(req)


def test_model_generated_addressed_miss_also_degrades():
    """The untrusted-authority rule guards BOTH paired events, not just B."""
    a = _fixture(ADDRESSED_MISS)
    a["interaction"]["label_authority"] = "unknown"
    a["interaction"]["label_ref"] = "model advisory reading"
    req = cr.route_capability([a, _fixture(NON_ADDRESSED)])
    assert req["required_capability"] == "engagement_control"
    assert "label_authority" in req["missing_evidence"]


def test_malformed_event_fails_loud():
    bad = _fixture(ADDRESSED_MISS)
    del bad["interaction"]
    with pytest.raises(cr.RoutingInputError):
        cr.route_capability([bad, _fixture(NON_ADDRESSED)])


def test_router_does_not_mutate_input_fixtures():
    a, b = _fixture(ADDRESSED_MISS), _fixture(NON_ADDRESSED)
    before = json.dumps([a, b], sort_keys=True)
    cr.route_capability([a, b], contract_uri=cr.DEFAULT_CONTRACT_URI)
    assert json.dumps([a, b], sort_keys=True) == before
