import asyncio

import pytest

from feishu_codex_bridge.approval import ApprovalRequest, FeishuApprovalCoordinator
from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.feishu_client import FeishuClient


class FakeFeishu(FeishuClient):
    def __init__(self) -> None:
        super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
        self.messages: list[tuple[str, str, str]] = []

    async def deliver_text(self, *, message_id: str, chat_id: str, text: str) -> None:
        self.messages.append((message_id, chat_id, text))


@pytest.mark.anyio
async def test_feishu_approval_resolves_with_plain_approve() -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        feishu_approval_timeout_seconds=5,
    )
    feishu = FakeFeishu()
    coordinator = FeishuApprovalCoordinator(settings, feishu)
    coordinator.set_current_message("chat", "om_original")

    request_task = asyncio.create_task(
        coordinator.request_approval(
            ApprovalRequest(
                conversation_id="chat",
                kind="command",
                method="execCommandApproval",
                title="open -a Word",
                details="{}",
                params={},
            )
        )
    )
    await asyncio.sleep(0)

    consumed = await coordinator.resolve_from_text("chat", "om_reply", "approve")

    assert consumed is True
    assert await request_task is True
    assert feishu.messages[-1] == ("om_reply", "chat", "Approved Codex request.")


@pytest.mark.anyio
async def test_feishu_approval_resolves_with_plain_deny() -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        feishu_approval_timeout_seconds=0.01,
    )
    feishu = FakeFeishu()
    coordinator = FeishuApprovalCoordinator(settings, feishu)
    coordinator.set_current_message("chat", "om_original")

    request_task = asyncio.create_task(
        coordinator.request_approval(
            ApprovalRequest("chat", "command", "execCommandApproval", "cmd", "{}", {})
        )
    )
    await asyncio.sleep(0)

    consumed = await coordinator.resolve_from_text("chat", "om_reply", "deny")

    assert consumed is True
    assert await request_task is False
    assert feishu.messages[-1] == ("om_reply", "chat", "Denied Codex request.")
