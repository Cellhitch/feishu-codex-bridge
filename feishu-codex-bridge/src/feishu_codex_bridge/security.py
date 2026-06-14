from __future__ import annotations

import base64
import hashlib
import hmac


def verify_verification_token(payload_token: str | None, expected_token: str) -> bool:
    if not expected_token:
        return True
    if payload_token is None:
        return False
    return hmac.compare_digest(payload_token, expected_token)


def verify_feishu_signature(
    *,
    timestamp: str | None,
    nonce: str | None,
    body: bytes,
    signature: str | None,
    encrypt_key: str,
) -> bool:
    if not encrypt_key:
        return True
    if not timestamp or not nonce or not signature:
        return False
    digest = hmac.new(
        encrypt_key.encode("utf-8"),
        f"{timestamp}{nonce}".encode("utf-8") + body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)

