from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import time

import pytest

import hotato.production as production_module
from hotato.production import (
    EventConflict,
    ProductionError,
    ProductionGateway,
    ProductionStore,
    normalize_otlp_json,
    validate_event,
    verify_regression_candidate,
)


def event(
    event_id="event-1",
    event_type="session.started",
    *,
    subject="call-1",
    source="adapter-a",
    sequence=0,
    data=None,
    authority="adapter_reported",
):
    value = {
        "specversion": "1.0",
        "id": event_id,
        "source": source,
        "type": event_type,
        "subject": subject,
        "time": "2026-07-17T12:00:00Z",
        "data": {} if data is None else data,
        "authority": {
            "kind": authority,
            "eligible_for_execution_claim": authority
            in ("measured", "signed_attestation"),
        },
    }
    if sequence is not None:
        value["sequence"] = sequence
    return value


def complete_session(store, *, subject="call-1", source="adapter-a", start=0):
    types = [
        "session.started",
        "media.asset.available",
        "transcript.segment",
        "model.operation",
        "tool.result",
        "state.snapshot",
        "session.ended",
    ]
    for offset, event_type in enumerate(types):
        store.ingest(
            event(
                f"event-{start + offset}",
                event_type,
                subject=subject,
                source=source,
                sequence=start + offset,
                data={"text": "secret"} if event_type == "transcript.segment" else {},
                authority="measured" if event_type == "media.asset.available" else "adapter_reported",
            )
        )


def test_event_validation_refuses_ambiguous_authority_and_bad_time():
    assert validate_event(event())["id"] == "event-1"
    broken = event()
    broken["authority"]["eligible_for_execution_claim"] = True
    with pytest.raises(ValueError, match="contradicts"):
        validate_event(broken)
    broken = event()
    broken["time"] = "2026-07-17 12:00:00"
    with pytest.raises(ValueError, match="timezone"):
        validate_event(broken)
    broken = event(data={"availability": "maybe"})
    with pytest.raises(ValueError, match="availability"):
        validate_event(broken)
    broken = event(data={"latency": float("nan")})
    with pytest.raises(ValueError, match="finite JSON"):
        validate_event(broken)
    nested = {}
    cursor = nested
    for _ in range(66):
        cursor["next"] = {}
        cursor = cursor["next"]
    with pytest.raises(ValueError, match="nesting depth"):
        validate_event(event(data=nested))


def test_commit_duplicate_conflict_and_source_scoped_ordering(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 1000.0)
    first = event()
    assert store.ingest(first)["durability"] == "committed"
    assert store.ingest(first)["status"] == "duplicate"

    # A second source owns a separate sequence cursor; sequence zero is ordered.
    assert (
        store.ingest(event("other-0", source="adapter-b", sequence=0))["status"]
        == "stored"
    )
    assert (
        store.ingest(event("late", event_type="turn.started", sequence=0))["status"]
        == "out_of_order"
    )

    conflicting = event()
    conflicting["data"] = {"different": True}
    conflicting["subject"] = "forged-subject"
    with pytest.raises(EventConflict):
        store.ingest(conflicting)
    with pytest.raises(KeyError):
        store.manifest("forged-subject")
    manifest = store.manifest("call-1")
    assert manifest["event_count"] == 3
    assert manifest["duplicate_count"] == 1
    assert manifest["conflict_count"] == 1
    assert manifest["out_of_order_count"] == 1
    store.close()


def test_explicit_evidence_authority_finalization_and_late_event(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    complete_session(store)
    finalized = store.finalize(quiescence_seconds=0, now=101.0)
    assert len(finalized) == 1
    manifest = finalized[0]
    assert manifest["status"] == "COMPLETE"
    assert manifest["evidence"]["participant_audio"] == {
        "availability": "available",
        "authority": "measured",
        "eligible_for_execution_claim": True,
        "event_ids": ["event-1"],
    }
    assert manifest["evidence"]["transcript"]["authority"] == "adapter_reported"
    # Finalization is stable until a late event arrives; late evidence is retained
    # and the session is explicitly degraded rather than silently re-sorted.
    assert store.finalize(quiescence_seconds=0, now=102.0) == []
    result = store.ingest(event("late-after-finalize", "turn.ended", sequence=99))
    assert result["status"] == "stored"
    late_manifest = store.manifest("call-1")
    assert late_manifest["status"] == "DEGRADED"
    assert late_manifest["finalization_reason"] == "late_event_after_finalization"
    store.close()


def test_unavailable_evidence_cannot_finalize_complete(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event())
    store.ingest(
        event(
            "audio-1",
            "media.asset.available",
            sequence=1,
            data={"availability": "unsupported"},
        )
    )
    store.ingest(event("end-1", "session.ended", sequence=2))
    manifest = store.finalize(quiescence_seconds=0, now=101.0)[0]
    assert manifest["status"] == "DEGRADED"
    assert manifest["evidence"]["participant_audio"]["availability"] == "unsupported"
    assert manifest["evidence"]["transcript"]["availability"] == "missing"
    store.close()


def test_finalization_persists_declared_required_lane_subset(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event())
    store.ingest(event("audio", "media.asset.available", sequence=1))
    store.ingest(event("end", "session.ended", sequence=2))
    manifest = store.finalize(
        quiescence_seconds=0,
        now=101.0,
        required_lanes=("participant_audio",),
    )[0]
    assert manifest["status"] == "COMPLETE"
    assert manifest["required_evidence_lanes"] == ["participant_audio"]
    assert manifest["evidence"]["transcript"]["availability"] == "missing"
    with pytest.raises(ValueError, match="required_lanes"):
        store.finalize(required_lanes=())
    store.close()


def test_finalization_refuses_complete_lifecycle_when_start_is_missing(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event("audio", "media.asset.available", sequence=0))
    store.ingest(event("end", "session.ended", sequence=1))
    manifest = store.finalize(
        quiescence_seconds=0,
        now=101.0,
        required_lanes=("participant_audio",),
    )[0]
    assert manifest["status"] == "DEGRADED"
    assert manifest["lifecycle"] == {
        "session_started_events": 0,
        "session_ended_events": 1,
        "unambiguous": False,
    }
    store.close()


def test_unsequenced_event_keeps_ordering_ambiguity_explicit(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event(sequence=None))
    store.ingest(event("audio", "media.asset.available", sequence=1))
    store.ingest(event("end", "session.ended", sequence=2))
    manifest = store.finalize(
        quiescence_seconds=0,
        now=101.0,
        required_lanes=("participant_audio",),
    )[0]
    assert manifest["status"] == "DEGRADED"
    assert manifest["unsequenced_count"] == 1
    store.close()


def test_alert_transitions_are_durable_and_metrics_have_bounded_labels(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event())
    assert store.evaluate_alerts([{"id": "conflict-only", "condition": "conflict"}]) == []
    first = store.evaluate_alerts([{"id": "audio-required", "condition": "missing_audio"}])
    assert first[0]["state"] == "FIRING"
    assert first[0]["generation"] == 1
    assert store.evaluate_alerts([{"id": "audio-required", "condition": "missing_audio"}]) == []
    store.ingest(event("audio", "media.asset.available", sequence=1))
    resolved = store.evaluate_alerts([{"id": "audio-required", "condition": "missing_audio"}])
    assert resolved[0]["state"] == "RESOLVED"
    store.ingest(
        event(
            "audio-unavailable",
            "media.asset.available",
            sequence=2,
            data={"availability": "unavailable"},
        )
    )
    reopened = store.evaluate_alerts([{"id": "audio-required", "condition": "missing_audio"}])
    assert reopened[0]["generation"] == 2
    metrics = store.metrics()
    assert "hotato_production_events_total 3" in metrics
    assert "hotato_production_duplicates_total 0" in metrics
    assert "call-1" not in metrics
    assert "audio-required" not in metrics
    store.close()


def test_audit_chain_detects_database_tamper(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event())
    assert store.verify_audit_chain()["valid"] is True
    store.db.execute("UPDATE audit SET result='tampered' WHERE sequence=1")
    verification = store.verify_audit_chain()
    assert verification["valid"] is False
    assert verification["first_invalid_sequence"] == 1
    store.close()


def test_audit_checkpoint_detects_tail_truncation(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event())
    store.ingest(event("event-2", "turn.started", sequence=1))
    store.db.execute(
        "DELETE FROM audit WHERE sequence=(SELECT MAX(sequence) FROM audit)"
    )
    verification = store.verify_audit_chain()
    assert verification["valid"] is False
    assert verification["checkpoint_matches"] is False
    store.close()


def test_portable_regression_candidate_verifies_offline_and_detects_tamper(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    complete_session(store)
    store.finalize(quiescence_seconds=0, now=101.0)
    target = tmp_path / "candidate"
    result = store.export_regression_candidate("call-1", str(target))
    assert result["verification"]["valid"] is True
    candidate = json.loads((target / "candidate.json").read_text())
    assert candidate["promotion"]["status"] == "CANDIDATE"
    assert "secret" not in (target / "events.jsonl").read_text()
    with pytest.raises(FileExistsError):
        store.export_regression_candidate("call-1", str(target))
    with open(target / "events.jsonl", "ab") as handle:
        handle.write(b"tamper")
    assert verify_regression_candidate(str(target))["valid"] is False
    store.close()


def test_default_redaction_denies_unknown_scalars_containers_and_lists(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    payload = {
        "availability": "available",
        "message": "SSN 123-45-6789",
        "customer_utterance": "card 4111111111111111",
        "api_token": "sk-production-super-secret",
        "nested": {
            "authorization": "Bearer nested-secret-token",
            "customer": {"ssn": "987-65-4321"},
        },
        "items": ["safe-looking", {"card": "5555555555554444"}],
    }
    store.ingest(event(data=payload))

    stored = json.loads(
        store.db.execute("SELECT payload_json FROM events").fetchone()[0]
    )
    assert stored["data"]["availability"] == "available"
    for key in (
        "message",
        "customer_utterance",
        "api_token",
        "nested",
        "items",
    ):
        raw = production_module._canonical(payload[key])
        assert stored["data"][key] == {
            "redacted": True,
            "byte_count": len(raw),
        }

    serialized = json.dumps(stored, sort_keys=True)
    for secret in (
        "123-45-6789",
        "4111111111111111",
        "sk-production-super-secret",
        "nested-secret-token",
        "987-65-4321",
        "5555555555554444",
    ):
        assert secret not in serialized
    store.close()


def test_redaction_preserves_only_typed_event_specific_structural_fields(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    digest = "sha256:" + "a" * 64
    payload = {
        "availability": "available",
        "sha256": digest,
        "bytes": 32000,
        "sample_rate_hz": 16000,
        "channels": 2,
        "duration_ms": 1000.25,
        "media_type": "audio/wav",
        "codec": "pcm_s16le",
        # The field is allowlisted, but this value has the wrong safe type.
        "frame_count": "4111111111111111",
        # The same technical-looking field is not safe for this event type.
        "status": "success",
    }
    store.ingest(event("audio", "media.asset.available", data=payload))
    stored = json.loads(
        store.db.execute("SELECT payload_json FROM events").fetchone()[0]
    )["data"]

    for key in (
        "availability",
        "sha256",
        "bytes",
        "sample_rate_hz",
        "channels",
        "duration_ms",
        "media_type",
        "codec",
    ):
        assert stored[key] == payload[key]
    assert stored["frame_count"]["redacted"] is True
    assert stored["status"]["redacted"] is True
    assert "4111111111111111" not in json.dumps(stored, sort_keys=True)
    store.close()


def test_redacted_regression_export_contains_no_nested_payload_secrets(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(
        event(
            data={
                "message": "SSN 123-45-6789",
                "nested": {"token": "tok-export-secret"},
                "cards": ["4111111111111111", "5555555555554444"],
            }
        )
    )
    store.ingest(
        event(
            "audio",
            "media.asset.available",
            sequence=1,
            data={"availability": "available", "audio": "secret-audio-value"},
            authority="measured",
        )
    )
    store.ingest(event("end", "session.ended", sequence=2))
    store.finalize(
        quiescence_seconds=0,
        now=101.0,
        required_lanes=("participant_audio",),
    )
    target = tmp_path / "candidate"
    store.export_regression_candidate("call-1", str(target))

    exported = (target / "events.jsonl").read_text()
    assert verify_regression_candidate(str(target))["valid"] is True
    for secret in (
        "123-45-6789",
        "tok-export-secret",
        "4111111111111111",
        "5555555555554444",
        "secret-audio-value",
    ):
        assert secret not in exported
    store.close()


def test_regression_candidate_refuses_racing_empty_destination(tmp_path, monkeypatch):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    complete_session(store)
    store.finalize(quiescence_seconds=0, now=101.0)
    target = tmp_path / "candidate"
    original_verify = production_module.verify_regression_candidate

    def create_racing_destination(path):
        result = original_verify(path)
        target.mkdir()
        return result

    monkeypatch.setattr(
        production_module, "verify_regression_candidate", create_racing_destination
    )
    with pytest.raises(FileExistsError):
        store.export_regression_candidate("call-1", str(target))
    assert list(target.iterdir()) == []
    store.close()


def test_unredacted_ingest_is_declared_in_manifest_and_candidate(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event(data={"text": "operator-kept-payload"}), redact_payloads=False)
    store.ingest(
        event("end", "session.ended", sequence=1), redact_payloads=False
    )
    manifest = store.finalize(quiescence_seconds=0, now=101.0)[0]
    assert manifest["payload_storage"] == "unredacted"
    target = tmp_path / "candidate"
    store.export_regression_candidate("call-1", str(target))
    candidate = json.loads((target / "candidate.json").read_text())
    assert candidate["privacy"]["payloads"] == "unredacted"
    assert "operator-kept-payload" in (target / "events.jsonl").read_text()
    store.close()


def test_candidate_verifier_refuses_symlink_artifact(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    complete_session(store)
    store.finalize(quiescence_seconds=0, now=101.0)
    target = tmp_path / "candidate"
    store.export_regression_candidate("call-1", str(target))
    external = tmp_path / "external.jsonl"
    external.write_bytes((target / "events.jsonl").read_bytes())
    (target / "events.jsonl").unlink()
    try:
        (target / "events.jsonl").symlink_to(external)
    except OSError:
        pytest.skip("symlinks unavailable")
    verification = verify_regression_candidate(str(target))
    assert verification["valid"] is False
    assert any("regular file" in error for error in verification["errors"])
    store.close()


def test_candidate_verifier_refuses_unlisted_file_and_symlink_root(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    complete_session(store)
    store.finalize(quiescence_seconds=0, now=101.0)
    target = tmp_path / "candidate"
    store.export_regression_candidate("call-1", str(target))
    (target / "unlisted-secret.txt").write_text("must not be camouflaged")
    verification = verify_regression_candidate(str(target))
    assert verification["valid"] is False
    assert any("unlisted candidate" in item for item in verification["errors"])
    (target / "unlisted-secret.txt").unlink()
    link = tmp_path / "candidate-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    verification = verify_regression_candidate(str(link))
    assert verification["valid"] is False
    assert verification["errors"] == [
        "candidate root must be a non-symlink directory"
    ]
    store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes required")
def test_production_database_and_wal_sidecars_are_private_under_public_umask(
    tmp_path,
):
    path = tmp_path / "production.sqlite"
    previous = os.umask(0o022)
    try:
        store = ProductionStore(str(path), clock=lambda: 100.0)
        store.ingest(event())
    finally:
        os.umask(previous)
    try:
        for candidate in (path, tmp_path / "production.sqlite-wal", tmp_path / "production.sqlite-shm"):
            if candidate.exists():
                assert candidate.stat().st_mode & 0o077 == 0
    finally:
        store.close()


def test_production_store_refuses_symlink_database(tmp_path):
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"")
    link = tmp_path / "production.sqlite"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ProductionError, match="regular non-symlink"):
        ProductionStore(str(link))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_candidate_reader_refuses_writerless_fifo(tmp_path):
    fifo = tmp_path / "candidate.json"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        production_module._read_regular_bytes_no_follow(
            str(fifo), max_bytes=1024
        )


def test_candidate_reader_detects_path_replacement_between_check_and_open(
    tmp_path, monkeypatch
):
    target = tmp_path / "candidate.json"
    replacement = tmp_path / "replacement.json"
    target.write_bytes(b'{"version":1}')
    replacement.write_bytes(b'{"version":2}')
    real_open = production_module.os.open
    swapped = False

    def swap_then_open(path, flags):
        nonlocal swapped
        if not swapped and os.fspath(path) == os.fspath(target):
            swapped = True
            target.unlink()
            replacement.rename(target)
        return real_open(path, flags)

    monkeypatch.setattr(production_module.os, "open", swap_then_open)
    with pytest.raises(ValueError, match="changed while it was being opened"):
        production_module._read_regular_bytes_no_follow(
            str(target), max_bytes=1024
        )


def test_retention_removes_subject_and_keeps_pseudonymous_receipt(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    complete_session(store)
    store.finalize(quiescence_seconds=0, now=100.0)
    receipts = store.enforce_retention(retention_seconds=10, now=200.0)
    assert len(receipts) == 1
    assert receipts[0]["deleted_event_count"] == 7
    assert receipts[0]["subject_sha256"].startswith("sha256:")
    with pytest.raises(KeyError):
        store.manifest("call-1")
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM deletion_receipts").fetchone()[0] == 1
    assert "hotato_production_events_total 7" in store.metrics()
    # Audit targets are digests, so retention leaves no raw subject there.
    audit_targets = [row[0] for row in store.db.execute("SELECT target FROM audit")]
    assert all("call-1" not in target for target in audit_targets)
    store.close()


def otlp_payload():
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "agent"}},
                        {"key": "call.id", "value": {"stringValue": "call-otel"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "voice-agent", "version": "1"},
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "name": "tool-call",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {
                                        "key": "hotato.event_type",
                                        "value": {"stringValue": "tool.result"},
                                    },
                                    {"key": "hotato.sequence", "value": {"intValue": "2"}},
                                ],
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_otlp_trace_normalization_preserves_correlation_and_authority(tmp_path):
    events = normalize_otlp_json(otlp_payload(), source="otel-sidecar")
    assert len(events) == 1
    normalized = events[0]
    assert normalized["subject"] == "call-otel"
    assert normalized["type"] == "tool.result"
    assert normalized["sequence"] == 2
    assert normalized["traceparent"] == f"00-{'a' * 32}-{'b' * 16}-01"
    assert normalized["authority"] == {
        "kind": "adapter_reported",
        "eligible_for_execution_claim": False,
    }
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    receipts = store.ingest_otlp(otlp_payload(), source="otel-sidecar")
    assert receipts[0]["durability"] == "committed"
    assert store.manifest("call-otel")["evidence"]["tool_calls"]["availability"] == "available"
    store.close()


def test_otlp_normalizer_refuses_malformed_entries_instead_of_silent_drop():
    malformed = otlp_payload()
    malformed["resourceSpans"].append(None)
    with pytest.raises(ValueError, match="resourceSpans entry"):
        normalize_otlp_json(malformed, source="otel-sidecar")

    duplicated = otlp_payload()
    attributes = duplicated["resourceSpans"][0]["resource"]["attributes"]
    attributes.append(dict(attributes[0]))
    with pytest.raises(ValueError, match="duplicate OTLP attribute"):
        normalize_otlp_json(duplicated, source="otel-sidecar")


def test_event_batch_rolls_back_on_storage_failure(tmp_path, monkeypatch):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    original = store._ingest_prepared
    calls = {"count": 0}

    def fail_second(prepared):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected storage boundary failure")
        return original(prepared)

    monkeypatch.setattr(store, "_ingest_prepared", fail_second)
    with pytest.raises(OSError, match="injected"):
        store.ingest_many(
            [event("batch-1", sequence=0), event("batch-2", sequence=1)]
        )
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
    store.close()


def request(gateway, method, path, body=b"", headers=None):
    host, port = gateway.address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def saturated_request(gateway, method, path, body=b"", headers=None):
    """Issue a request that a saturated gateway is expected to refuse.

    The shed path writes the 503 and closes without reading the request
    bytes, which forces a TCP RST. POSIX loopback stacks deliver the
    buffered 503 before surfacing the reset, so callers keep the strict
    status assertion there. Windows discards undelivered receive-buffer
    data when it processes the RST and aborts the client's read instead
    (WinError 10053/10054), so the same refusal can surface as a
    connection abort rather than a readable 503. Returns ``(None, b"")``
    for that abort shape; both shapes are the gateway refusing the
    request, never accepting it.
    """
    try:
        return request(gateway, method, path, body, headers)
    except (ConnectionAbortedError, ConnectionResetError):
        if os.name != "nt":
            raise
        return None, b""


def test_gateway_bearer_auth_persists_before_ack(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 1000.0)
    gateway = ProductionGateway(store, "0123456789abcdef", max_workers=2)
    try:
        raw = json.dumps(event(), separators=(",", ":")).encode()
        assert request(gateway, "POST", "/v1/events", raw)[0] == 401
        status, body = request(
            gateway,
            "POST",
            "/v1/events",
            raw,
            {"Authorization": "Bearer 0123456789abcdef", "Content-Length": str(len(raw))},
        )
        assert status == 200
        assert json.loads(body)["durability"] == "committed"
        # A fresh connection can observe the committed row immediately.
        check = ProductionStore(str(tmp_path / "production.sqlite"))
        assert check.manifest("call-1")["event_count"] == 1
        check.close()
    finally:
        gateway.close()
        store.close()


def test_gateway_hmac_authenticates_exact_bytes_before_parse(tmp_path):
    secret = "s" * 32
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 1000.0)
    gateway = ProductionGateway(store, hmac_secret=secret)
    try:
        raw = json.dumps(event(), separators=(",", ":")).encode()
        timestamp = "1000"
        signature = hmac.new(
            secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        headers = {
            "X-Hotato-Timestamp": timestamp,
            "X-Hotato-Signature": "v1=" + signature,
            "Content-Length": str(len(raw)),
        }
        assert request(gateway, "POST", "/v1/events", raw, headers)[0] == 200
        tampered = raw + b" "
        tampered_headers = dict(headers)
        tampered_headers["Content-Length"] = str(len(tampered))
        assert request(gateway, "POST", "/v1/events", tampered, tampered_headers)[0] == 401
        stale_headers = dict(headers)
        stale_headers["X-Hotato-Timestamp"] = "1"
        stale_headers["X-Hotato-Signature"] = "v1=" + hmac.new(
            secret.encode(), b"1." + raw, hashlib.sha256
        ).hexdigest()
        assert request(gateway, "POST", "/v1/events", raw, stale_headers)[0] == 401
        assert production_module._verify_hmac(
            raw,
            timestamp="١٠٠٠",
            signature="v1=" + "a" * 64,
            secret=secret,
            now=1000.0,
            max_skew_seconds=300,
        ) is False
    finally:
        gateway.close()
        store.close()


def test_gateway_otlp_endpoint_and_metrics_auth(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 1000.0)
    gateway = ProductionGateway(store, "0123456789abcdef")
    try:
        raw = json.dumps(otlp_payload()).encode()
        headers = {
            "Authorization": "Bearer 0123456789abcdef",
            "X-Hotato-Source": "otel-sidecar",
            "Content-Length": str(len(raw)),
        }
        status, body = request(gateway, "POST", "/v1/otlp/traces", raw, headers)
        assert status == 200
        assert json.loads(body)["durability"] == "committed"
        assert request(gateway, "GET", "/metrics")[0] == 401
        status, metrics = request(
            gateway,
            "GET",
            "/metrics",
            headers={"Authorization": "Bearer 0123456789abcdef"},
        )
        assert status == 200
        assert b"hotato_production_events_total" in metrics
    finally:
        gateway.close()
        store.close()


def test_gateway_accepts_standard_otlp_http_json_trace_path(tmp_path):
    store = ProductionStore(str(tmp_path / "standard-otlp.sqlite"), clock=lambda: 1000.0)
    gateway = ProductionGateway(store, "0123456789abcdef")
    try:
        raw = json.dumps(otlp_payload()).encode()
        headers = {
            "Authorization": "Bearer 0123456789abcdef",
            "X-Hotato-Source": "otel-sdk",
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
        }
        status, body = request(gateway, "POST", "/v1/traces", raw, headers)
        assert status == 200
        assert json.loads(body) == {}
        assert store.manifest("call-otel")["event_count"] == 1

        conflict = otlp_payload()
        conflict["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = "changed"
        conflict_raw = json.dumps(conflict).encode()
        conflict_headers = dict(headers)
        conflict_headers["Content-Length"] = str(len(conflict_raw))
        status, body = request(
            gateway, "POST", "/v1/traces", conflict_raw, conflict_headers
        )
        assert status == 200
        assert json.loads(body) == {
            "partialSuccess": {
                "errorMessage": "Hotato refused conflicting event identities",
                "rejectedSpans": "1",
            }
        }
    finally:
        gateway.close()
        store.close()


def test_gateway_refuses_plaintext_non_loopback_bind_and_duplicate_json(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 1000.0)
    with pytest.raises(ValueError, match="loopback binds only"):
        ProductionGateway(store, "0123456789abcdef", host="0.0.0.0")
    gateway = ProductionGateway(store, "0123456789abcdef")
    try:
        raw = json.dumps(event(), separators=(",", ":")).encode()
        duplicate = raw.replace(
            b'"id":"event-1"', b'"id":"event-1","id":"event-1"', 1
        )
        status, body = request(
            gateway,
            "POST",
            "/v1/events",
            duplicate,
            {
                "Authorization": "Bearer 0123456789abcdef",
                "Content-Length": str(len(duplicate)),
            },
        )
        assert status == 400
        assert json.loads(body) == {"error": "invalid_event"}
    finally:
        gateway.close()
        store.close()


def test_gateway_returns_backpressure_when_worker_capacity_is_full(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 1000.0)
    gateway = ProductionGateway(store, "0123456789abcdef", max_workers=1)
    # Occupy the single worker permit without starting a request. The accept
    # loop must refuse the next connection instead of creating an unbounded
    # thread or queueing it in memory.
    assert gateway.server._capacity.acquire(blocking=False)
    permit_held = True
    try:
        status, body = saturated_request(gateway, "GET", "/healthz")
        if status is not None:
            assert status == 503
            assert json.loads(body) == {"error": "backpressure"}
        raw = json.dumps(event(), separators=(",", ":")).encode()
        status, body = saturated_request(
            gateway,
            "POST",
            "/v1/events",
            raw,
            {
                "Authorization": "Bearer 0123456789abcdef",
                "Content-Length": str(len(raw)),
            },
        )
        if status is not None:
            assert status == 503
            assert json.loads(body) == {"error": "backpressure"}
        # Refusal is observable from gateway state on every platform,
        # including when Windows surfaces it as a connection abort: the
        # authenticated POST enqueued no work, and the sole worker permit
        # is still exhausted because shedding spawned no worker and
        # released nothing.
        assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert gateway.server._capacity.acquire(blocking=False) is False
        gateway.server._capacity.release()
        permit_held = False
        # With the permit back, the same gateway serves again: the refusals
        # above were load shedding, not a wedged or crashed accept loop.
        status, body = request(gateway, "GET", "/healthz")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}
    finally:
        if permit_held:
            gateway.server._capacity.release()
        gateway.close()
        store.close()


def test_gateway_times_out_partial_request_body_and_releases_worker(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 1000.0)
    gateway = ProductionGateway(
        store,
        "0123456789abcdef",
        max_workers=1,
        request_timeout_seconds=0.1,
    )
    client = socket.create_connection(gateway.address, timeout=2)
    try:
        client.sendall(
            b"POST /v1/events HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer 0123456789abcdef\r\n"
            b"Content-Length: 10\r\n\r\n{}"
        )
        response = client.recv(4096)
        assert b" 408 " in response
    finally:
        client.close()
    # The timed-out handler relinquishes the sole semaphore permit. Polls
    # that land before the release are shed, which Windows can surface as a
    # connection abort instead of a readable 503; both shapes mean "still
    # saturated, poll again".
    for _ in range(50):
        status, _body = saturated_request(gateway, "GET", "/healthz")
        if status == 200:
            break
        time.sleep(0.01)
    assert status == 200
    gateway.close()
    store.close()


def test_schemas_validate_representative_outputs(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema_dir = os.path.join(os.path.dirname(__file__), "..", "src", "hotato", "schema")
    with open(os.path.join(schema_dir, "production-event.v1.json")) as handle:
        jsonschema.validate(event(), json.load(handle))
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    complete_session(store)
    manifest = store.finalize(quiescence_seconds=0, now=101.0)[0]
    with open(os.path.join(schema_dir, "production-session.v1.json")) as handle:
        jsonschema.validate(manifest, json.load(handle))
    target = tmp_path / "candidate"
    store.export_regression_candidate("call-1", str(target))
    with open(os.path.join(schema_dir, "production-regression-candidate.v1.json")) as handle:
        jsonschema.validate(json.loads((target / "candidate.json").read_text()), json.load(handle))
    store.close()


def test_candidate_requires_finalization(tmp_path):
    store = ProductionStore(str(tmp_path / "production.sqlite"), clock=lambda: 100.0)
    store.ingest(event())
    with pytest.raises(ProductionError, match="finalized"):
        store.export_regression_candidate("call-1", str(tmp_path / "candidate"))
    store.close()
