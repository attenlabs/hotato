def verify_webhook(request: Request, raw_body: bytes) -> None:
    """Verify Retell's HMAC signature BEFORE any parse, fetch, or scan.

    Retell signs each webhook with an HMAC-SHA256 of the raw request body keyed
    by your API key and sends it in the ``X-Retell-Signature`` header
    (docs.retellai.com/features/webhook). A missing or non-matching signature is
    rejected 401 and never processed. Confirm the exact construction against
    your Retell dashboard version before you rely on it in production.
    """
    api_key = os.environ.get("RETELL_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="RETELL_API_KEY is not set")
    presented = request.headers.get("x-retell-signature", "")
    expected = hmac.new(api_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not presented or not hmac.compare_digest(expected, presented):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
