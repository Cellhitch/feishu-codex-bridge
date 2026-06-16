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


def test_settings_expands_hermes_env_path(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True)
    hermes_env = hermes_dir / ".env"
    hermes_env.write_text("FEISHU_APP_ID=cli_from_home\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BRIDGE_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)

    settings = Settings(_env_file=None, hermes_env_path="~/.hermes/.env")

    assert settings.hermes_env_path == hermes_env
    assert settings.feishu_app_id == "cli_from_home"


def test_settings_default_codex_model_and_effort() -> None:
    settings = Settings(_env_file=None, use_hermes_feishu_env=False)

    assert settings.codex_model == "gpt-5.5"
    assert settings.codex_reasoning_effort == "medium"
    assert settings.codex_stream_limit_bytes == 16 * 1024 * 1024
    assert settings.codex_thread_map_path.is_absolute()
    assert settings.codex_thread_map_path.name == ".codex-thread-map.json"
    assert settings.codex_fixed_thread_id == ""
    assert settings.image_codex_model == ""
    assert settings.image_codex_reasoning_effort == ""


def test_blank_codex_app_socket_is_none() -> None:
    settings = Settings(_env_file=None, use_hermes_feishu_env=False, codex_app_socket="")

    assert settings.codex_app_socket is None


def test_stream_updates_are_opt_in_by_default() -> None:
    settings = Settings(_env_file=None, use_hermes_feishu_env=False)

    assert not settings.feishu_stream_updates_enabled
    assert not settings.feishu_show_reasoning
    assert not settings.feishu_show_history
    assert not settings.feishu_stream_assistant_deltas
    assert settings.feishu_progress_seconds == 0
    assert not settings.feishu_seed_history_to_codex
    assert settings.feishu_history_max_messages == 20
    assert settings.feishu_history_lookback_seconds == 24 * 60 * 60
    assert settings.feishu_history_max_chars == 8000
