"""Config identity auto-attach: ``hotato investigate --stack`` runs the
read-only ``hotato inspect`` config snapshot into the contract bundle's
``source/stack_config_snapshot.json`` slot by default, instead of leaving it a
bare placeholder ({}). Silent config drift is a confirmed cross-provider
failure class, so a live-pulled contract carries the turn-taking baseline it
would drift from.

Pins:
  * a bare local WAV (no stack) still writes the honest placeholder -- nothing
    is fabricated and no inspect runs;
  * a provided snapshot is written verbatim into the bundle;
  * the capture helper is fail-closed: an unsupported stack, a static-config
    stack, a missing --agent-id, or an inspect error all yield an honest
    "not captured" note (never a network call it cannot make, never a guess);
  * a live vapi/retell pull with an --agent-id captures the real config and
    threads it end to end (investigate -> state -> label -> bundle), without
    leaking the assistant/agent id into the share-safe bundle.
"""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources

import pytest

from hotato import capture as _capture
from hotato import contract as _contract
from hotato import inspectcfg as _inspect
from hotato import investigate as _investigate


def _hard_wav(dst: str) -> str:
    src = resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav")
    with resources.as_file(src) as p:
        shutil.copyfile(str(p), dst)
    return dst


def _read_snapshot(bundle_dir: str) -> dict:
    with open(os.path.join(bundle_dir, "source", "stack_config_snapshot.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


# --- contract.create_contract: placeholder vs provided snapshot ------------

def test_placeholder_written_when_no_snapshot(tmp_path):
    wav = _hard_wav(str(tmp_path / "call.wav"))
    result = _contract.create_contract(
        stereo=wav, onset_sec=2.40, expect="yield", contract_id="ph-1",
        out_dir=str(tmp_path / "out"), human_review_attested=True)
    snap = _read_snapshot(result["dir"])
    assert snap["config"] == {}
    assert "placeholder" in snap["note"]


def test_provided_snapshot_written_verbatim(tmp_path):
    wav = _hard_wav(str(tmp_path / "call.wav"))
    provided = {"stack": "vapi", "captured": True,
                "config": {"interrupt_min_words": 3},
                "note": "captured live"}
    result = _contract.create_contract(
        stereo=wav, onset_sec=2.40, expect="yield", contract_id="cap-1",
        out_dir=str(tmp_path / "out"), human_review_attested=True,
        config_snapshot=provided)
    assert _read_snapshot(result["dir"]) == provided


# --- _capture_config_snapshot: fail-closed, never a network call it can't ---

def test_capture_unsupported_stack_is_not_captured():
    snap = _investigate._capture_config_snapshot(
        "twilio", agent_id="a1", api_key="k")
    assert snap["captured"] is False
    assert snap["config"] == {}
    assert "no live config reader" in snap["note"]


def test_capture_static_config_stack_points_at_inspect_config():
    snap = _investigate._capture_config_snapshot(
        "livekit", agent_id="a1", api_key="k")
    assert snap["captured"] is False
    assert "--config" in snap["note"]


def test_capture_missing_agent_id_is_not_captured():
    snap = _investigate._capture_config_snapshot(
        "vapi", agent_id=None, api_key="k")
    assert snap["captured"] is False
    assert "--agent-id" in snap["note"]


def test_capture_inspect_error_is_swallowed_not_raised(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(_inspect, "run_inspect", _boom)
    snap = _investigate._capture_config_snapshot(
        "vapi", agent_id="asst_1", api_key="k")
    assert snap["captured"] is False
    assert "could not run" in snap["note"]


def test_capture_success_keeps_config_and_hides_the_id(monkeypatch):
    def _fake(**kw):
        assert kw["assistant_id"] == "asst_SECRET"
        return {
            "stack": "vapi", "target": "asst_SECRET",
            # mirror real inspect_vapi: the id is embedded in the request URL
            "fetched_at_provenance": {
                "method": "GET https://api.vapi.ai/assistant/asst_SECRET"},
            "turn_taking": {"interrupt_min_words": 3,
                            "interrupt_voice_seconds": 0.2, "raw": {"numWords": 3}},
            "observations": ["waitSeconds is at its floor"],
            "notes": [],
        }
    monkeypatch.setattr(_inspect, "run_inspect", _fake)
    snap = _investigate._capture_config_snapshot(
        "vapi", agent_id="asst_SECRET", api_key="k")
    assert snap["captured"] is True
    assert snap["config"]["interrupt_min_words"] == 3
    assert snap["observations"] == ["waitSeconds is at its floor"]
    # the assistant id is never carried into the share-safe snapshot, even
    # though the real inspect provenance embeds it in the request URL
    assert "asst_SECRET" not in json.dumps(snap)
    # the endpoint shape is still kept as provenance (id redacted)
    assert "assistant" in snap["inspect_method"]
    assert "<agent-id>" in snap["inspect_method"]


# --- end to end: live pull -> state -> label -> bundle snapshot -------------

def test_live_pull_threads_config_into_the_bundle(tmp_path, monkeypatch):
    wav = _hard_wav(str(tmp_path / "pulled.wav"))
    monkeypatch.setattr(_capture, "resolve_creds", lambda stack, overrides=None: {})
    monkeypatch.setattr(_capture, "fetch_one",
                        lambda *a, **k: wav)

    def _fake_inspect(**kw):
        return {
            "stack": "vapi", "target": "asst_1",
            # mirror real inspect_vapi: the id is embedded in the request URL
            "fetched_at_provenance": {
                "method": "GET https://api.vapi.ai/assistant/asst_1"},
            "turn_taking": {"interrupt_min_words": 5, "raw": {"numWords": 5}},
            "observations": [],
            "notes": [],
        }
    monkeypatch.setattr(_inspect, "run_inspect", _fake_inspect)

    state = str(tmp_path / "state.json")
    result, code = _investigate.run_investigate(
        stack="vapi", call_id="call-1", agent_id="asst_1", state_path=state)
    assert code == 0
    # state carries the captured baseline
    st = json.loads(open(state, encoding="utf-8").read())
    assert st["stack_config_snapshot"]["captured"] is True
    assert st["stack_config_snapshot"]["config"]["interrupt_min_words"] == 5

    label = _investigate.run_investigate_label(
        f"{state}#1", expect="yield", out_dir=str(tmp_path / "contracts"),
        reviewer="qa-dana")
    snap = _read_snapshot(label["dir"])
    assert snap["captured"] is True
    assert snap["config"]["interrupt_min_words"] == 5
    assert "asst_1" not in json.dumps(snap)


def test_local_wav_pull_still_writes_placeholder(tmp_path):
    wav = _hard_wav(str(tmp_path / "call.wav"))
    state = str(tmp_path / "state.json")
    _investigate.run_investigate(wav, state_path=state)
    label = _investigate.run_investigate_label(
        f"{state}#1", expect="yield", out_dir=str(tmp_path / "contracts"),
        reviewer="qa-dana")
    snap = _read_snapshot(label["dir"])
    assert snap["config"] == {}
    assert "placeholder" in snap["note"]
