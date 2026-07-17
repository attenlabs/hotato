from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from hotato import caller
from hotato.caller import SUPPORTED, UNOBSERVABLE, UNSUPPORTED
from hotato.livekit_session import (
    EVIDENCE_SCHEMA,
    LiveKitCallerSession,
    LiveKitCapabilityError,
    LiveKitRTCDriver,
    LiveKitSDKUnavailable,
    LiveKitSessionError,
)


class FakeDriver:
    def __init__(self):
        self.sink = None
        self.connected = []
        self.audio = []
        self.data = []
        self.dtmf = []
        self.closed = 0

    def connect(self, **kwargs):
        token = kwargs.pop("token")
        assert token == "short-lived-token"
        self.connected.append(dict(kwargs))
        self.sink = kwargs["event_sink"]
        self.sink({
            "transport_event": "lifecycle",
            "status": "connected",
            "participant_identity": None,
        })

    def publish_audio(self, pcm_s16le, **kwargs):
        self.audio.append((pcm_s16le, dict(kwargs)))

    def publish_data(self, payload, **kwargs):
        self.data.append((payload, dict(kwargs)))

    def publish_dtmf(self, digits, **kwargs):
        self.dtmf.append((digits, dict(kwargs)))

    def close(self, **_kwargs):
        self.closed += 1

    def emit(self, value):
        assert self.sink is not None
        self.sink(value)


def _session(driver=None, **kwargs):
    driver = driver or FakeDriver()
    session = LiveKitCallerSession(
        "ws://127.0.0.1:7880",
        "short-lived-token",
        target_identity="agent-under-test",
        driver=driver,
        session_id="session-1",
        **kwargs,
    )
    return session, driver


def _drain_connected(session):
    event = session.receive(100)
    assert event["kind"] == "lifecycle"
    assert event["status"] == "connected"


def _digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def test_remote_egress_is_explicit_and_token_is_not_retained():
    driver = FakeDriver()
    with pytest.raises(ValueError, match="allow_remote"):
        LiveKitCallerSession(
            "wss://example.test",
            "short-lived-token",
            target_identity="agent",
            driver=driver,
        )
    with pytest.raises(ValueError, match="wss://"):
        LiveKitCallerSession(
            "ws://example.test",
            "short-lived-token",
            target_identity="agent",
            allow_remote=True,
            driver=driver,
        )
    session = LiveKitCallerSession(
        "wss://example.test",
        "short-lived-token",
        target_identity="agent",
        allow_remote=True,
        driver=driver,
    )
    assert "short-lived-token" not in repr(session)
    assert all("short-lived-token" not in repr(value) for value in session.__dict__.values())
    session.close()


def test_capability_truth_requires_tts_and_does_not_claim_delivery():
    session, _driver = _session(evidence_topic="hotato.evidence.v1")
    caps = session.capabilities()
    assert caps == {
        "send_text": UNSUPPORTED,
        "send_audio": SUPPORTED,
        "receive": SUPPORTED,
        "send_dtmf": SUPPORTED,
        "wait": SUPPORTED,
        "silence": SUPPORTED,
        "impairment": UNSUPPORTED,
        "observe_transfer": SUPPORTED,
        "hangup": SUPPORTED,
    }
    assert session.evidence_capabilities()["outgoing_audio_delivery"] == UNOBSERVABLE
    with pytest.raises(LiveKitCapabilityError, match="TTS"):
        session.send_text("hello", {})
    with pytest.raises(LiveKitCapabilityError, match="sidecar"):
        session.set_impairment({"packet_loss": 0.1})
    session.close()


def test_audio_submission_is_digest_bound_bounded_and_rate_explicit():
    session, driver = _session()
    _drain_connected(session)
    pcm = b"\x01\x00" * 960
    session.send_audio(pcm, 48_000, {"pcm_sha256": _digest(pcm)})
    assert driver.audio[0][0] == pcm
    assert driver.audio[0][1]["frame_duration_ms"] == 20
    event = session.receive(100)
    assert event["event"] == "audio_submitted"
    assert event["pcm_sha256"] == _digest(pcm)
    assert event["submission_status"] == "sdk_playout_complete"
    assert event["target_delivery"] == UNOBSERVABLE
    control = [json.loads(item[0]) for item in driver.data]
    assert [item["type"] for item in control] == ["session_started", "audio_submission"]
    assert control[-1]["pcm_sha256"] == _digest(pcm)
    with pytest.raises(ValueError, match="sample rate"):
        session.send_audio(pcm, 16_000, {})
    with pytest.raises(ValueError, match="does not match"):
        session.send_audio(pcm, 48_000, {"pcm_sha256": "sha256:" + "0" * 64})
    session.close()


def test_received_pcm_has_per_frame_and_rolling_hashes():
    session, driver = _session()
    _drain_connected(session)
    one = b"\x01\x00" * 100
    two = b"\x02\x00" * 100
    for pcm in (one, two):
        driver.emit({
            "transport_event": "audio_frame",
            "participant_identity": "agent-under-test",
            "pcm_s16le": pcm,
            "sample_rate_hz": 48_000,
            "channels": 1,
        })
    first = session.receive(100)
    second = session.receive(100)
    assert first["pcm_sha256"] == _digest(one)
    assert first["stream_sha256"] == _digest(one)
    assert second["pcm_sha256"] == _digest(two)
    assert second["stream_sha256"] == _digest(one + two)
    summary = session.media_summary()
    assert summary["incoming_pcm_bytes"] == len(one + two)
    assert summary["incoming_stream_sha256"] == _digest(one + two)
    boundary = session.evidence()
    assert boundary["schema"] == "hotato.livekit-session-boundary.v1"
    assert boundary["media"] == summary
    assert boundary["capabilities"]["incoming_audio_observation"] == SUPPORTED
    assert boundary["authority"]["carrier_or_pstn_delivery"] == UNOBSERVABLE
    assert "short-lived-token" not in json.dumps(boundary)
    session.close()


def test_only_target_identity_can_contribute_audio_transcript_or_evidence():
    session, driver = _session(evidence_topic="hotato.evidence.v1")
    _drain_connected(session)
    driver.emit({
        "transport_event": "transcription",
        "participant_identity": "different-participant",
        "text": "ignore me",
        "final": True,
        "language": "en",
    })
    assert session.receive(10) is None
    driver.emit({
        "transport_event": "transcription",
        "participant_identity": "agent-under-test",
        "text": "accepted target transcript",
        "final": True,
        "language": "en",
    })
    event = session.receive(100)
    assert event["kind"] == "transcript"
    assert event["authority"] == "livekit_transcription_event"
    driver.emit({
        "transport_event": "dtmf",
        "participant_identity": "different-participant",
        "digit": "7",
    })
    assert session.receive(10) is None
    driver.emit({
        "transport_event": "dtmf",
        "participant_identity": "agent-under-test",
        "digit": "8",
    })
    assert session.receive(100)["digits"] == "8"
    session.close()


def test_target_audio_receipt_is_bound_to_session_sequence_and_submission():
    session, driver = _session(evidence_topic="hotato.evidence.v1")
    _drain_connected(session)
    pcm = b"\x01\x00" * 100
    session.send_audio(pcm, 48_000, {})
    assert session.receive(100)["event"] == "audio_submitted"
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "session_id": "session-1",
        "sequence": 1,
        "kind": "audio_receipt",
        "payload": {
            "submission_sequence": 1,
            "submitted_sha256": _digest(pcm),
            "delivered_sha256": _digest(b"decoded-at-target"),
            "delivered_bytes": 17,
            "sample_rate_hz": 48_000,
            "channels": 1,
            "boundary": "agent-input-after-decode",
        },
    }
    driver.emit({
        "transport_event": "data",
        "participant_identity": "agent-under-test",
        "topic": "hotato.evidence.v1",
        "payload": json.dumps(evidence).encode(),
    })
    receipt = session.receive(100)
    assert receipt["event"] == "delivered_audio_receipt"
    assert receipt["authority"] == "target_participant_reported"
    assert receipt["submitted_sha256"] == _digest(pcm)
    assert session.evidence_capabilities()["outgoing_audio_delivery"] == SUPPORTED
    session.close()


def test_invalid_target_evidence_is_rejected_without_consuming_sequence():
    session, driver = _session(evidence_topic="hotato.evidence.v1")
    _drain_connected(session)
    pcm = b"\x01\x00" * 100
    session.send_audio(pcm, 48_000, {})
    session.receive(100)
    invalid = {
        "schema": EVIDENCE_SCHEMA,
        "session_id": "session-1",
        "sequence": 1,
        "kind": "audio_receipt",
        "payload": {
            "submission_sequence": 1,
            "submitted_sha256": "sha256:" + "0" * 64,
            "delivered_sha256": _digest(b"decoded"),
            "delivered_bytes": 14,
            "sample_rate_hz": 48_000,
            "channels": 1,
            "boundary": "agent-input",
        },
    }
    driver.emit({
        "transport_event": "data",
        "participant_identity": "agent-under-test",
        "topic": "hotato.evidence.v1",
        "payload": json.dumps(invalid).encode(),
    })
    assert session.receive(100)["event"] == "target_evidence_rejected"
    invalid["payload"]["submitted_sha256"] = _digest(pcm)
    driver.emit({
        "transport_event": "data",
        "participant_identity": "agent-under-test",
        "topic": "hotato.evidence.v1",
        "payload": json.dumps(invalid).encode(),
    })
    assert session.receive(100)["event"] == "delivered_audio_receipt"
    session.close()


def test_event_queue_overflow_fails_closed_instead_of_dropping_evidence():
    session, driver = _session(max_events=1)
    driver.emit({
        "transport_event": "lifecycle",
        "status": "target_connected",
        "participant_identity": "agent-under-test",
    })
    with pytest.raises(LiveKitSessionError, match="overflowed"):
        session.receive(100)
    session.close()


def test_dtmf_submission_uses_bounded_driver_operation():
    session, driver = _session()
    _drain_connected(session)
    session.send_dtmf("1#a")
    assert driver.dtmf[0][0] == "1#A"
    event = session.receive(100)
    assert event["event"] == "dtmf_submitted"
    assert event["target_delivery"] == UNOBSERVABLE
    session.close()


def test_concurrent_media_operations_fail_closed_instead_of_interleaving():
    session, _driver = _session()
    _drain_connected(session)
    assert session._operation_lock.acquire(blocking=False)
    try:
        with pytest.raises(LiveKitSessionError, match="concurrent"):
            session.send_audio(b"\x01\x00", 48_000, {})
        with pytest.raises(LiveKitSessionError, match="concurrent"):
            session.send_dtmf("1")
    finally:
        session._operation_lock.release()
    session.close()


def test_livekit_session_executes_a_bounded_caller_plan_end_to_end(tmp_path):
    class TTS:
        def synthesize(self, _text):
            return {
                "pcm_s16le": b"\x01\x00" * 480,
                "sample_rate_hz": 48_000,
                "provider": "fixture",
                "model": "fixture-tts-v1",
                "voice": "caller-a",
                "settings": {"seed": 1},
            }

    session, driver = _session()
    plan = {
        "schema": caller.PLAN_SCHEMA,
        "id": "livekit-e2e",
        "mode": "scripted",
        "start": "say",
        "nodes": [
            {"id": "say", "type": "say", "text": "Please cancel.", "next": "done"},
            {"id": "done", "type": "hangup", "reason": "scenario_complete"},
        ],
    }
    run = caller.run_caller(
        plan,
        session,
        str(tmp_path / "run"),
        tts=TTS(),
        created_at="2026-07-17T00:00:00Z",
    )
    assert run.exit_code == 0
    assert run.result["status"] == "HUNG_UP"
    assert run.result["actions"][0]["delivery"] == "audio"
    assert run.result["session_boundary"]["availability"] == "available"
    assert run.result["session_boundary"]["media"][
        "outgoing_successful_submissions"
    ] == 1
    assert run.verification["ok"] is True
    assert driver.audio
    assert driver.closed == 1


def test_incomplete_optional_sdk_is_a_clean_capability_error():
    with pytest.raises(LiveKitSDKUnavailable, match="missing"):
        LiveKitRTCDriver(rtc_module=object())


class _FakeAudioSource:
    def __init__(self, sample_rate, channels, **_kwargs):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frames = []
        self.closed = False

    async def capture_frame(self, frame):
        self.frames.append(frame)

    async def wait_for_playout(self):
        return None

    async def aclose(self):
        self.closed = True


class _SlowAudioSource(_FakeAudioSource):
    async def capture_frame(self, frame):
        del frame
        import asyncio

        await asyncio.sleep(1)


class _FakeAudioFrame:
    def __init__(self, *, data, sample_rate, num_channels, samples_per_channel):
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class _FakeAudioStream:
    def __init__(self, _track, **_kwargs):
        frame = _FakeAudioFrame(
            data=b"\x03\x00" * 20,
            sample_rate=48_000,
            num_channels=1,
            samples_per_channel=20,
        )
        self.items = [SimpleNamespace(frame=frame)]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)

    async def aclose(self):
        return None


class _FakeLocalParticipant:
    def __init__(self):
        self.published_track = None
        self.data = []
        self.dtmf = []
        self.subscription_permissions = None

    async def publish_track(self, track, options):
        self.published_track = (track, options)

    async def publish_data(self, payload, **kwargs):
        self.data.append((payload, kwargs))

    async def publish_dtmf(self, **kwargs):
        self.dtmf.append(kwargs)

    def set_track_subscription_permissions(self, **kwargs):
        self.subscription_permissions = kwargs


class _FakeRoom:
    def __init__(self):
        self.handlers = {}
        self.local_participant = _FakeLocalParticipant()
        self.connected = None
        self.disconnected = False

    def on(self, name):
        def decorate(callback):
            self.handlers[name] = callback
            return callback
        return decorate

    async def connect(self, url, token, *, options=None):
        self.connected = (url, token, options)

    async def disconnect(self):
        self.disconnected = True


class _FakeRTC:
    class RoomOptions:
        def __init__(self, *, auto_subscribe=True):
            self.auto_subscribe = auto_subscribe

    class TrackSource:
        SOURCE_MICROPHONE = "microphone"

    class TrackKind:
        KIND_AUDIO = "audio"

    class TrackPublishOptions:
        source = None

    class ParticipantTrackPermission:
        def __init__(self, **kwargs):
            self.values = kwargs

    class LocalAudioTrack:
        @staticmethod
        def create_audio_track(name, source):
            return (name, source)

    AudioSource = _FakeAudioSource
    AudioFrame = _FakeAudioFrame
    AudioStream = _FakeAudioStream

    def __init__(self):
        self.rooms = []

    def Room(self):
        room = _FakeRoom()
        self.rooms.append(room)
        return room


def test_concrete_driver_maps_official_rtc_surface_with_fake_sdk():
    rtc = _FakeRTC()
    driver = LiveKitRTCDriver(rtc_module=rtc)
    session, _ = _session(driver=driver)
    _drain_connected(session)
    pcm = b"\x01\x00" * 1_000
    session.send_audio(pcm, 48_000, {})
    assert session.receive(100)["event"] == "audio_submitted"
    room = rtc.rooms[0]
    assert room.connected[2].auto_subscribe is False
    permissions = room.local_participant.subscription_permissions
    assert permissions["allow_all_participants"] is False
    assert permissions["participant_permissions"][0].values == {
        "participant_identity": "agent-under-test",
        "allow_all": True,
        "allowed_track_sids": [],
    }
    source = room.local_participant.published_track[0][1]
    assert b"".join(frame.data for frame in source.frames) == pcm
    session.send_dtmf("1#")
    assert [item["code"] for item in room.local_participant.dtmf] == [1, 11]
    session.receive(100)

    track = SimpleNamespace(kind="audio")
    participant = SimpleNamespace(identity="agent-under-test")
    driver._loop.call_soon_threadsafe(  # fake SDK callback on the SDK loop
        room.handlers["track_subscribed"], track, None, participant
    )
    audio = session.receive(1_000)
    assert audio["event"] == "received_audio_frame"
    assert audio["authority"] == "local_livekit_receiver"
    session.close()
    assert room.disconnected is True
    assert source.closed is True


def test_concrete_driver_subscribes_only_to_target_audio_publications():
    class Publication:
        def __init__(self, kind):
            self.kind = kind
            self.track = None
            self.requests = []

        def set_subscribed(self, value):
            self.requests.append(value)

    rtc = _FakeRTC()
    driver = LiveKitRTCDriver(rtc_module=rtc)
    session, _ = _session(driver=driver)
    _drain_connected(session)
    room = rtc.rooms[0]
    target_audio = Publication("audio")
    target_video = Publication("video")
    other_audio = Publication("audio")
    room.handlers["track_published"](
        other_audio, SimpleNamespace(identity="other-participant")
    )
    room.handlers["track_published"](
        target_video, SimpleNamespace(identity="agent-under-test")
    )
    room.handlers["track_published"](
        target_audio, SimpleNamespace(identity="agent-under-test")
    )
    assert other_audio.requests == []
    assert target_video.requests == []
    assert target_audio.requests == [True]
    session.close()


def test_concrete_driver_timeout_is_bounded_and_does_not_leak_sdk_details():
    class SlowRTC(_FakeRTC):
        AudioSource = _SlowAudioSource

    rtc = SlowRTC()
    driver = LiveKitRTCDriver(rtc_module=rtc)
    session, _ = _session(driver=driver, operation_timeout_seconds=0.01)
    _drain_connected(session)
    with pytest.raises(LiveKitSessionError) as caught:
        session.send_audio(b"\x01\x00" * 48, 48_000, {})
    assert "bounded timeout" in str(caught.value)
    assert "short-lived-token" not in str(caught.value)
    summary = session.media_summary()
    assert summary["outgoing_submission_attempts"] == 1
    assert summary["outgoing_successful_submissions"] == 0
    assert summary["outgoing_pcm_bytes"] == 0
    session.close()
