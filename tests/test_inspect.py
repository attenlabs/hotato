"""`hotato inspect` (Level 1): normalization of mocked Vapi/Retell payloads,
static parsing of LiveKit/Pipecat config snippets, absent options staying null,
missing credentials exiting 2, and the read-only guarantees."""

import json

import pytest

from hotato import cli, inspectcfg

# --- mocked HTTP -------------------------------------------------------------

VAPI_PAYLOAD = {
    "id": "asst_123",
    "startSpeakingPlan": {
        "waitSeconds": 0.4,
        "smartEndpointingPlan": {"provider": "livekit"},
    },
    "stopSpeakingPlan": {
        "numWords": 2,
        "voiceSeconds": 0.2,
        "backoffSeconds": 1.0,
        "acknowledgementPhrases": ["mhm", "okay"],
    },
}

RETELL_PAYLOAD = {
    "agent_id": "agent_9",
    "responsiveness": 1,
    "interruption_sensitivity": 0.8,
    "enable_backchannel": True,
    "backchannel_frequency": 0.8,
    "backchannel_words": ["yeah", "uh-huh"],
    "begin_message_delay_ms": 0,
}


@pytest.fixture
def http_mock(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=30):
        calls.append({"url": url, "headers": headers or {}})
        if "api.vapi.ai" in url:
            return json.loads(json.dumps(VAPI_PAYLOAD))
        if "api.retellai.com" in url:
            return json.loads(json.dumps(RETELL_PAYLOAD))
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(inspectcfg, "_http_get_json", fake_get)
    return calls


def test_vapi_normalizes(http_mock):
    res = inspectcfg.inspect_vapi(assistant_id="asst_123", api_key="k")
    tt = res["turn_taking"]
    assert tt["interrupt_min_words"] == 2
    assert tt["interrupt_voice_seconds"] == 0.2
    assert tt["resume_backoff_seconds"] == 1.0
    assert tt["endpointing_wait_seconds"] == 0.4
    assert tt["backchannel_aware"] is True  # acknowledgementPhrases nonempty
    assert tt["raw"]["stopSpeakingPlan"]["numWords"] == 2
    assert res["stack"] == "vapi"
    assert res["target"] == {"assistant_id": "asst_123"}
    prov = res["fetched_at_provenance"]
    assert prov["method"] == "GET https://api.vapi.ai/assistant/asst_123"
    assert prov["read_only"] is True
    assert "docs.vapi.ai" in prov["field_basis"]
    # Exactly one read-only GET with Bearer auth.
    assert len(http_mock) == 1
    assert http_mock[0]["headers"]["Authorization"] == "Bearer k"


def test_vapi_absent_plans_normalize_to_null(monkeypatch):
    monkeypatch.setattr(inspectcfg, "_http_get_json",
                        lambda url, headers=None, timeout=30: {"id": "x"})
    res = inspectcfg.inspect_vapi(assistant_id="x", api_key="k")
    tt = res["turn_taking"]
    for field in ("interrupt_min_words", "interrupt_voice_seconds",
                  "resume_backoff_seconds", "endpointing_wait_seconds",
                  "backchannel_aware"):
        assert tt[field] is None
    assert any("never guessed" in n for n in res["notes"])


def test_retell_normalizes(http_mock):
    res = inspectcfg.inspect_retell(agent_id="agent_9", api_key="k")
    tt = res["turn_taking"]
    # Retell exposes unitless scales: word/second fields are null with a note,
    # the actual values live untouched in raw.
    assert tt["interrupt_min_words"] is None
    assert tt["interrupt_voice_seconds"] is None
    assert tt["endpointing_wait_seconds"] is None
    assert tt["backchannel_aware"] is True
    assert tt["raw"]["interruption_sensitivity"] == 0.8
    assert tt["raw"]["responsiveness"] == 1
    assert tt["raw"]["backchannel_words"] == ["yeah", "uh-huh"]
    assert any("unitless" in n for n in res["notes"])
    assert res["fetched_at_provenance"]["method"] == (
        "GET https://api.retellai.com/get-agent/agent_9"
    )
    assert http_mock[0]["headers"]["Authorization"] == "Bearer k"


def test_retell_absent_fields_are_null_with_note(monkeypatch):
    monkeypatch.setattr(inspectcfg, "_http_get_json",
                        lambda url, headers=None, timeout=30: {"agent_id": "a"})
    res = inspectcfg.inspect_retell(agent_id="a", api_key="k")
    assert res["turn_taking"]["backchannel_aware"] is None
    assert any("enable_backchannel absent" in n for n in res["notes"])


# --- credentials: clean exit 2, never a traceback -----------------------------

def test_missing_vapi_creds_exit_2(monkeypatch, capsys):
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    assert cli.main(["inspect", "--stack", "vapi",
                     "--assistant-id", "asst_1"]) == 2
    assert "VAPI_API_KEY" in capsys.readouterr().err


def test_missing_retell_creds_exit_2(monkeypatch, capsys):
    monkeypatch.delenv("RETELL_API_KEY", raising=False)
    assert cli.main(["inspect", "--stack", "retell", "--agent-id", "a_1"]) == 2
    assert "RETELL_API_KEY" in capsys.readouterr().err


def test_missing_target_flags_exit_2():
    assert cli.main(["inspect", "--stack", "vapi"]) == 2
    assert cli.main(["inspect", "--stack", "livekit"]) == 2


def test_missing_config_file_exit_2():
    assert cli.main(["inspect", "--stack", "livekit",
                     "--config", "/nonexistent/agent.py"]) == 2


# --- LiveKit static parse -------------------------------------------------------

LIVEKIT_SNIPPET = '''
from livekit.agents import AgentSession
from livekit.agents.voice import (
    EndpointingOptions,
    InterruptionOptions,
    TurnHandlingOptions,
)

session = AgentSession(
    turn_handling=TurnHandlingOptions(
        interruption=InterruptionOptions(
            min_duration=0.6,
            min_words=2,
            false_interruption_timeout=2.0,
            mode="adaptive",
        ),
        endpointing=EndpointingOptions(min_delay=0.4, max_delay=3.0),
    ),
)
'''

LIVEKIT_LEGACY_SNIPPET = '''
session = AgentSession(
    min_interruption_duration=0.3,
    min_interruption_words=1,
    min_endpointing_delay=0.2,
)
'''

LIVEKIT_DYNAMIC_SNIPPET = '''
import os
opts = InterruptionOptions(min_words=int(os.environ["MW"]))
'''


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_livekit_parses_current_nested_options(tmp_path):
    res = inspectcfg.inspect_livekit_file(
        _write(tmp_path, "agent.py", LIVEKIT_SNIPPET))
    tt = res["turn_taking"]
    assert tt["interrupt_min_words"] == 2
    assert tt["interrupt_voice_seconds"] == 0.6
    assert tt["resume_backoff_seconds"] == 2.0
    assert tt["endpointing_wait_seconds"] == 0.4
    assert tt["backchannel_aware"] is True  # mode="adaptive"
    assert tt["raw"]["EndpointingOptions.max_delay"] == 3.0
    assert "static parse" in res["fetched_at_provenance"]["method"]
    assert "no code executed" in res["fetched_at_provenance"]["method"]


def test_livekit_parses_legacy_flat_kwargs(tmp_path):
    res = inspectcfg.inspect_livekit_file(
        _write(tmp_path, "legacy.py", LIVEKIT_LEGACY_SNIPPET))
    tt = res["turn_taking"]
    assert tt["interrupt_min_words"] == 1
    assert tt["interrupt_voice_seconds"] == 0.3
    assert tt["endpointing_wait_seconds"] == 0.2


def test_livekit_absent_options_are_null_with_notes(tmp_path):
    res = inspectcfg.inspect_livekit_file(
        _write(tmp_path, "empty.py", "session = AgentSession()\n"))
    tt = res["turn_taking"]
    for field in ("interrupt_min_words", "interrupt_voice_seconds",
                  "resume_backoff_seconds", "endpointing_wait_seconds",
                  "backchannel_aware"):
        assert tt[field] is None
    assert any("not guessed" in n for n in res["notes"])


def test_livekit_non_literal_value_is_never_guessed(tmp_path):
    res = inspectcfg.inspect_livekit_file(
        _write(tmp_path, "dyn.py", LIVEKIT_DYNAMIC_SNIPPET))
    assert res["turn_taking"]["interrupt_min_words"] is None
    assert any("not a literal" in n for n in res["notes"])


def test_livekit_syntax_error_exits_2(tmp_path):
    path = _write(tmp_path, "broken.py", "def broken(:\n")
    assert cli.main(["inspect", "--stack", "livekit", "--config", path]) == 2


# --- Pipecat static parse --------------------------------------------------------

PIPECAT_SNIPPET = '''
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.turns.user_turn_strategies import (
    MinWordsUserTurnStartStrategy,
    SpeechTimeoutUserTurnStopStrategy,
    UserTurnStrategies,
    VADUserTurnStartStrategy,
)

vad = VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.8)
strategies = UserTurnStrategies(
    start=[MinWordsUserTurnStartStrategy(min_words=3),
           VADUserTurnStartStrategy()],
    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
)
'''

PIPECAT_KRISP_SNIPPET = '''
strategies = UserTurnStrategies(
    start=[KrispVivaIPUserTurnStartStrategy(model_path="m", threshold=0.5)],
)
'''


def test_pipecat_parses_strategies_and_vad(tmp_path):
    res = inspectcfg.inspect_pipecat_file(
        _write(tmp_path, "bot.py", PIPECAT_SNIPPET))
    tt = res["turn_taking"]
    assert tt["interrupt_min_words"] == 3
    assert tt["endpointing_wait_seconds"] == 0.6  # user_speech_timeout wins
    assert tt["interrupt_voice_seconds"] == 0.2   # VADParams.start_secs
    assert tt["backchannel_aware"] is False       # no classifier strategy
    assert tt["raw"]["VADParams.confidence"] == 0.7
    assert "MinWordsUserTurnStartStrategy" in tt["raw"]["turn_strategy_classes"]


def test_pipecat_krisp_strategy_marks_backchannel_aware(tmp_path):
    res = inspectcfg.inspect_pipecat_file(
        _write(tmp_path, "krisp.py", PIPECAT_KRISP_SNIPPET))
    tt = res["turn_taking"]
    assert tt["backchannel_aware"] is True
    assert tt["raw"]["classifier_strategy"] == "KrispVivaIPUserTurnStartStrategy"


def test_pipecat_empty_file_all_null(tmp_path):
    res = inspectcfg.inspect_pipecat_file(_write(tmp_path, "e.py", "x = 1\n"))
    tt = res["turn_taking"]
    assert tt["interrupt_min_words"] is None
    assert tt["endpointing_wait_seconds"] is None
    assert tt["backchannel_aware"] is None
    assert any("defaults apply" in n for n in res["notes"])


# --- observations: surfaced, never judgments -------------------------------------

def test_high_min_words_becomes_an_observation(monkeypatch):
    payload = json.loads(json.dumps(VAPI_PAYLOAD))
    payload["stopSpeakingPlan"]["numWords"] = 4
    monkeypatch.setattr(inspectcfg, "_http_get_json",
                        lambda url, headers=None, timeout=30: payload)
    res = inspectcfg.inspect_vapi(assistant_id="x", api_key="k")
    assert any("interrupt_min_words is 4" in o for o in res["observations"])
    assert all("Observation" in o for o in res["observations"])


def test_default_values_produce_no_observations(http_mock):
    res = inspectcfg.inspect_vapi(assistant_id="asst_123", api_key="k")
    assert res["observations"] == []


# --- CLI surface --------------------------------------------------------------------

def test_cli_inspect_livekit_text_and_json(tmp_path, capsys):
    path = _write(tmp_path, "agent.py", LIVEKIT_SNIPPET)
    assert cli.main(["inspect", "--stack", "livekit", "--config", path]) == 0
    text = capsys.readouterr().out
    assert "interrupt_min_words = 2" in text
    assert cli.main(["inspect", "--stack", "livekit", "--config", path,
                     "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "inspect"
    assert set(doc["turn_taking"]) >= {
        "interrupt_min_words", "interrupt_voice_seconds",
        "resume_backoff_seconds", "endpointing_wait_seconds",
        "backchannel_aware", "raw",
    }


def test_cli_inspect_vapi_uses_env_key(monkeypatch, capsys, http_mock):
    monkeypatch.setenv("VAPI_API_KEY", "env-key")
    assert cli.main(["inspect", "--stack", "vapi",
                     "--assistant-id", "asst_123", "--format", "json"]) == 0
    assert http_mock[0]["headers"]["Authorization"] == "Bearer env-key"
    doc = json.loads(capsys.readouterr().out)
    assert doc["turn_taking"]["interrupt_min_words"] == 2


def test_inspect_http_get_installs_hardened_opener_before_request(monkeypatch):
    """The credentialed inspect path (``inspect_vapi``/``inspect_retell`` send
    ``Authorization: Bearer <vendor key>``) must install the credential-safe
    opener BEFORE issuing the request, so a cross-host 30x from the vendor host
    cannot exfiltrate the API key. This path previously missed the opener that
    rubric.py / state_adapter.py already install. Pin the ordering."""
    import urllib.error
    import urllib.request

    import hotato.capture as capture
    import hotato.inspectcfg as inspectcfg

    order = []
    monkeypatch.setattr(capture, "_ensure_safe_opener",
                        lambda: order.append("opener"))

    def _fake_urlopen(req, timeout=None):
        order.append("urlopen")
        raise urllib.error.URLError("no network in test")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    with pytest.raises(ValueError):
        inspectcfg._http_get_json(
            "https://api.vapi.ai/assistant/x",
            headers={"Authorization": "Bearer secret-key"})

    assert order and order[0] == "opener", (
        "inspect must install the hardened opener before any request; "
        f"got order={order}")
    assert "urlopen" in order
