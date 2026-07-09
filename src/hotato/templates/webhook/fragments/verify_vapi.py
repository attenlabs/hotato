def verify_webhook(request: Request, raw_body: bytes) -> None:
    """Constant-time shared-secret check, run BEFORE any parse, fetch, or scan.

    Vapi sends the server-URL secret in the ``X-Vapi-Secret`` header
    (docs.vapi.ai/server-url). A request whose secret is missing or does not
    match ``VAPI_WEBHOOK_SECRET`` is rejected 401 and never processed. This is
    the only gate: nothing below runs until it passes.
    """
    expected = os.environ.get("VAPI_WEBHOOK_SECRET", "")
    presented = request.headers.get("x-vapi-secret", "")
    if not expected:
        raise HTTPException(status_code=503, detail="VAPI_WEBHOOK_SECRET is not set")
    if not hmac.compare_digest(expected, presented):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
