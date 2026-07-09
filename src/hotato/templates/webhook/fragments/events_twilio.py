def parse_payload(raw_body: bytes) -> dict:
    """Twilio posts application/x-www-form-urlencoded. Parsed as DATA only."""
    from urllib.parse import parse_qs

    parsed = parse_qs(raw_body.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: (v[-1] if v else "") for k, v in parsed.items()}


def is_target_event(payload: dict) -> bool:
    """True only when the Twilio recording is complete. The
    recordingStatusCallback posts ``RecordingStatus``; only ``completed`` means
    the recording is ready to fetch (twilio.com/docs/voice/api/recording). Other
    statuses are ignored."""
    return payload.get("RecordingStatus") == "completed"
