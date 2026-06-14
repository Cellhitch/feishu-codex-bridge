from feishu_codex_bridge.security import verify_feishu_signature, verify_verification_token


def test_verify_token_allows_empty_expected() -> None:
    assert verify_verification_token(None, "")


def test_verify_token_compares_expected() -> None:
    assert verify_verification_token("abc", "abc")
    assert not verify_verification_token("abc", "def")


def test_signature_disabled_without_encrypt_key() -> None:
    assert verify_feishu_signature(
        timestamp=None,
        nonce=None,
        body=b"{}",
        signature=None,
        encrypt_key="",
    )

