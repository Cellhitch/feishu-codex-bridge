from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _bridge_project_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_thread_map_path() -> Path:
    return _bridge_project_dir() / ".codex-thread-map.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BRIDGE_",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8788

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_domain: str = ""
    feishu_delivery_mode: str = "send"
    feishu_approval_enabled: bool = True
    feishu_approval_timeout_seconds: float = 180.0
    feishu_progress_seconds: float = 0.0
    feishu_progress_text: str = "Working..."
    feishu_stream_updates_enabled: bool = False
    feishu_stream_flush_seconds: float = 2.0
    feishu_stream_max_chars: int = 3500
    feishu_stream_assistant_deltas: bool = False
    feishu_show_reasoning: bool = False
    feishu_show_history: bool = False
    feishu_seed_history_to_codex: bool = False
    feishu_history_max_messages: int = 20
    feishu_history_lookback_seconds: int = 24 * 60 * 60
    feishu_history_max_chars: int = 8000
    use_hermes_feishu_env: bool = True
    hermes_env_path: Path = Path.home() / ".hermes/.env"

    codex_binary: Path = Path("/Applications/Codex.app/Contents/Resources/codex")
    codex_cwd: Path = Field(default_factory=Path.cwd)
    codex_model: str = "gpt-5.5"
    codex_reasoning_effort: str = "medium"
    codex_use_app_server: bool = False
    codex_app_socket: Path | None = None
    codex_timeout_seconds: float = 120.0
    codex_stream_limit_bytes: int = 16 * 1024 * 1024
    codex_thread_map_path: Path = Field(default_factory=_default_thread_map_path)
    codex_fixed_thread_id: str = ""
    codex_load_history_on_start: bool = True
    codex_approval_policy: str = "on-request"
    codex_sandbox: str = "read-only"
    codex_thread_name_prefix: str = "Feishu"

    reply_prefix: str = ""
    dry_run_replies: bool = True
    media_dir: Path = Path("feishu-media")
    max_media_bytes: int = 25 * 1024 * 1024
    image_use_exec_fallback: bool = True
    image_codex_model: str = ""
    image_codex_reasoning_effort: str = ""

    @field_validator("codex_app_socket", mode="before")
    @classmethod
    def blank_socket_path_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def load_hermes_feishu_defaults(self) -> "Settings":
        self.hermes_env_path = self.hermes_env_path.expanduser()
        hermes_env = _read_env_file(self.hermes_env_path) if self.use_hermes_feishu_env else {}

        self.feishu_app_id = self.feishu_app_id or os.getenv("FEISHU_APP_ID", "") or hermes_env.get("FEISHU_APP_ID", "")
        self.feishu_app_secret = (
            self.feishu_app_secret
            or os.getenv("FEISHU_APP_SECRET", "")
            or hermes_env.get("FEISHU_APP_SECRET", "")
        )
        self.feishu_verification_token = (
            self.feishu_verification_token
            or os.getenv("FEISHU_VERIFICATION_TOKEN", "")
            or hermes_env.get("FEISHU_VERIFICATION_TOKEN", "")
        )
        self.feishu_encrypt_key = (
            self.feishu_encrypt_key
            or os.getenv("FEISHU_ENCRYPT_KEY", "")
            or hermes_env.get("FEISHU_ENCRYPT_KEY", "")
        )
        self.feishu_domain = normalize_feishu_domain(
            self.feishu_domain
            or os.getenv("FEISHU_DOMAIN", "")
            or hermes_env.get("FEISHU_DOMAIN", "")
        )
        return self


def normalize_feishu_domain(value: str) -> str:
    normalized = (value or "feishu").strip().lower()
    if normalized == "feishu":
        return "https://open.feishu.cn"
    if normalized == "lark":
        return "https://open.larksuite.com"
    if normalized.startswith("http://") or normalized.startswith("https://"):
        return normalized.rstrip("/")
    return value.rstrip("/")


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
