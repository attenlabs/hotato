def verify_webhook(request: Request, raw_body: bytes) -> None:
    """Verify Twilio's ``X-Twilio-Signature`` BEFORE any parse, fetch, or scan.

    Twilio signs each request as base64(HMAC-SHA1(auth_token, url + sorted
    param concatenation)) and sends it in ``X-Twilio-Signature``
    (twilio.com/docs/usage/security). The signed url is the public url Twilio
    posted to; set ``TWILIO_WEBHOOK_URL`` to that exact url (scheme + host +
    path). A missing or non-matching signature is rejected 401 and never
    processed.
    """
    import base64
    from urllib.parse import parse_qs

    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    public_url = os.environ.get("TWILIO_WEBHOOK_URL", "")
    if not token or not public_url:
        raise HTTPException(
            status_code=503,
            detail="TWILIO_AUTH_TOKEN and TWILIO_WEBHOOK_URL must be set",
        )
    presented = request.headers.get("x-twilio-signature", "")
    params = {
        k: v[-1]
        for k, v in parse_qs(
            raw_body.decode("utf-8", "replace"), keep_blank_values=True
        ).items()
    }
    signed = public_url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    if not presented or not hmac.compare_digest(expected, presented):
        raise HTTPException(status_code=401, detail="invalid Twilio signature")
