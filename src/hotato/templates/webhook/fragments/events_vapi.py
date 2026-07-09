def parse_payload(raw_body: bytes) -> dict:
    """Vapi posts JSON. The payload is untrusted DATA; it is only read, never run."""
    try:
        data = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"payload is not JSON: {exc}")
    return data if isinstance(data, dict) else {}


def is_target_event(payload: dict) -> bool:
    """True only for Vapi's end-of-call-report, the event that carries a
    finished stereo recording; every other event (status-update, transcript,
    ...) is acknowledged 200 and ignored, so the read-only fetch runs once per
    completed call. See docs.vapi.ai/server-url/events."""
    message = payload.get("message")
    if isinstance(message, dict):
        return message.get("type") == "end-of-call-report"
    return payload.get("type") == "end-of-call-report"
