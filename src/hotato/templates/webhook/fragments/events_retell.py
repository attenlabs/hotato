def parse_payload(raw_body: bytes) -> dict:
    """Retell posts JSON. The payload is untrusted DATA; it is only read, never run."""
    try:
        data = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"payload is not JSON: {exc}")
    return data if isinstance(data, dict) else {}


def is_target_event(payload: dict) -> bool:
    """True only for a terminal Retell event that carries a finished
    multi-channel recording. Retell posts ``event`` at top level; the recording
    url is available once the call ends/analyzes
    (docs.retellai.com/features/webhook). Non-terminal events are acknowledged
    200 and ignored."""
    return payload.get("event") in ("call_ended", "call_analyzed")
