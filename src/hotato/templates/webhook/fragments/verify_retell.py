def verify_webhook(request: Request, raw_body: bytes) -> None:
    """Verify Retell's signed webhook header BEFORE any parse, fetch, or scan.

    Retell sends ``X-Retell-Signature`` as ``v=<unix_timestamp>,d=<hex_digest>``,
    where the digest is HMAC-SHA256(api_key, raw_body + timestamp) and a delivery
    is accepted only inside a five-minute freshness window
    (docs.retellai.com/features/webhook). A header that is missing, malformed,
    carries a duplicate or unknown field, is older than five minutes, or is dated
    in the future is rejected 401 and never processed. The digest is decoded and
    compared to the expected value in constant time on raw bytes.
    """
    import time

    FRESHNESS_WINDOW_SEC = 300
    CLOCK_SKEW_SEC = 60

    api_key = os.environ.get("RETELL_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="RETELL_API_KEY is not set")

    header = request.headers.get("x-retell-signature", "")
    if not header:
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    # Strictly parse ``v=<timestamp>,d=<digest>``: exactly two ``key=value``
    # fields, no duplicate keys, no unknown keys.
    fields = {}
    parts = header.split(",")
    if len(parts) != 2:
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep or not key or not value:
            raise HTTPException(status_code=401, detail="invalid webhook signature")
        if key in fields:
            raise HTTPException(status_code=401, detail="invalid webhook signature")
        fields[key] = value
    if set(fields) != {"v", "d"}:
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    ts_str = fields["v"]
    digest_hex = fields["d"]

    # Freshness: the timestamp is ASCII-digit Unix seconds inside the window and
    # not dated in the future (beyond a small clock-skew allowance). Require
    # isascii() as well: str.isdigit() is True for non-ASCII digits (e.g. the
    # superscript "²") that int() then rejects, which would surface as an
    # uncaught 500 instead of a clean 401 rejection.
    if not (ts_str.isascii() and ts_str.isdigit()):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    timestamp = int(ts_str)
    now = int(time.time())
    if timestamp > now + CLOCK_SKEW_SEC:
        raise HTTPException(status_code=401, detail="webhook timestamp is in the future")
    if now - timestamp > FRESHNESS_WINDOW_SEC:
        raise HTTPException(status_code=401, detail="webhook signature has expired")

    # Decode the presented hex digest; a non-hex value is a rejection, not a crash.
    try:
        presented = bytes.fromhex(digest_hex)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    # Sign the raw body concatenated with the exact timestamp string, then
    # compare decoded bytes in constant time.
    expected = hmac.new(
        api_key.encode("utf-8"),
        raw_body + ts_str.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, presented):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
