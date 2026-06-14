from feishu_codex_bridge.config import Settings, normalize_feishu_domain


def test_normalize_feishu_domain_aliases() -> None:
    assert normalize_feishu_domain("feishu") == "https://open.feishu.cn"
    assert normalize_feishu_domain("lark") == "https://open.larksuite.com"
    assert normalize_feishu_domain("https://example.com/") == "https://example.com"


def test_settings_loads_hermes_feishu_env(tmp_path, monkeypatch) -> None:
    hermes_env = tmp_path / ".env"
    hermes_env.write_text(
        "\n".join(
            [
                "FEISHU_APP_ID=cli_app_id",
                "FEISHU_APP_SECRET='cli_secret'",
                "FEISHU_VERIFICATION_TOKEN=\"cli_token\"",
                "FEISHU_DOMAIN=lark",
            ]
        ),
        encoding="utf-8",
    )
    for key in (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_DOMAIN",
        "BRIDGE_FEISHU_APP_ID",
        "BRIDGE_FEISHU_APP_SECRET",
        "BRIDGE_FEISHU_VERIFICATION_TOKEN",
        "BRIDGE_FEISHU_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None, hermes_env_path=hermes_env)

    assert settings.feishu_app_id == "cli_app_id"
    assert settings.feishu_app_secret == "cli_secret"
    assert settings.feishu_verification_token == "cli_token"
    assert settings.feishu_domain == "https://open.larksuite.com"


def test_settings_default_codex_model_and_effort() -> None:
    settings = Settings(_env_file=None, use_hermes_feishu_env=False)

    assert settings.codex_model == "gpt-5.5"
    assert settings.codex_reasoning_effort == "medium"
    assert settings.codex_stream_limit_bytes == 16 * 1024 * 1024
    assert settings.codex_thread_map_path.is_absolute()
    assert settings.codex_thread_map_path.name == ".codex-thread-map.json"
    assert settings.image_codex_model == ""
    assert settings.image_codex_reasoning_effort == ""
