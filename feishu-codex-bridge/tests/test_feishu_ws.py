from types import SimpleNamespace

import pytest

from feishu_codex_bridge.codex_client import CodexClient
from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.feishu_client import FeishuClient
from feishu_codex_bridge.feishu_ws import FeishuWebsocketBridge


class FailingCodex(CodexClient):
    async def ask(self, message, conversation_id: str) -> str:
        raise RuntimeError("codex unavailable")


class FakeFeishu(FeishuClient):
    def __init__(self) -> None:
        super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
        self.texts: list[tuple[str, str, str]] = []

    async def deliver_text(self, *, message_id: str, chat_id: str, text: str) -> None:
        self.texts.append((message_id, chat_id, text))


@pytest.mark.anyio
async def test_websocket_message_errors_are_reported_to_feishu() -> None:
    feishu = FakeFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(_env_file=None, use_hermes_feishu_env=False),
        FailingCodex(),  # type: ignore[abstract]
        feishu,
    )
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
            message=SimpleNamespace(
                message_id="om_1",
                chat_id="oc_1",
                message_type=SimpleNamespace(value="text"),
                content='{"text":"hello"}',
            ),
        ),
    )

    await bridge._handle_message_event(data)

    assert feishu.texts == [("om_1", "oc_1", "Codex bridge error: codex unavailable")]
