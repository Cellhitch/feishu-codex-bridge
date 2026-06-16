import asyncio
from types import SimpleNamespace

import pytest

from feishu_codex_bridge.codex_client import CodexClient, CodexStreamEvent
from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.feishu_client import FeishuClient, FeishuHistoryMessage
from feishu_codex_bridge.feishu_ws import FeishuWebsocketBridge


class FailingCodex(CodexClient):
    async def ask(self, message, conversation_id: str) -> str:
        raise RuntimeError("codex unavailable")


class SlowCodex(CodexClient):
    async def ask(self, message, conversation_id: str) -> str:
        await asyncio.sleep(0.02)
        return "done"


class SlowerCodex(CodexClient):
    async def ask(self, message, conversation_id: str) -> str:
        await asyncio.sleep(0.035)
        return "done"


class StreamingCodex(CodexClient):
    def __init__(self) -> None:
        self.loaded_conversations: list[str] = []

    async def load_conversation(self, conversation_id: str) -> list[CodexStreamEvent]:
        self.loaded_conversations.append(conversation_id)
        return [CodexStreamEvent("history", "Codex: previous")]

    async def ask(self, message, conversation_id: str) -> str:
        return await self.ask_stream(message, conversation_id)

    async def ask_stream(self, message, conversation_id: str, event_handler=None) -> str:
        if event_handler:
            await event_handler(CodexStreamEvent("status", "Codex started working."))
            await event_handler(CodexStreamEvent("reasoning", "Checking the repo."))
            await event_handler(CodexStreamEvent("tool", "Running command: npm test"))
            await event_handler(CodexStreamEvent("assistant", "I found the relevant path."))
        return "done"


class ContextCodex(CodexClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def ask(self, message, conversation_id: str) -> str:
        self.calls.append(("ask", conversation_id, str(message)))
        return "done"


class FakeFeishu(FeishuClient):
    def __init__(self) -> None:
        super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
        self.texts: list[tuple[str, str, str]] = []
        self.text_targets: list[tuple[str, str, str, str]] = []
        self.history_messages: list[FeishuHistoryMessage] = []
        self.history_requests: list[tuple[str, str, int, int]] = []

    async def deliver_text(self, *, message_id: str, chat_id: str, text: str, open_id: str = "") -> None:
        self.texts.append((message_id, chat_id, text))
        self.text_targets.append((message_id, chat_id, open_id, text))

    async def list_recent_text_messages(
        self,
        *,
        chat_id: str,
        before_message_id: str = "",
        limit: int = 20,
        lookback_seconds: int = 24 * 60 * 60,
    ) -> list[FeishuHistoryMessage]:
        self.history_requests.append((chat_id, before_message_id, limit, lookback_seconds))
        return self.history_messages


class StreamFailingFeishu(FakeFeishu):
    async def deliver_text(self, *, message_id: str, chat_id: str, text: str, open_id: str = "") -> None:
        if text.startswith("Action:\n"):
            raise RuntimeError("transient Feishu send failure")
        await super().deliver_text(message_id=message_id, chat_id=chat_id, open_id=open_id, text=text)


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


@pytest.mark.anyio
async def test_empty_error_messages_use_exception_name() -> None:
    feishu = FakeFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(_env_file=None, use_hermes_feishu_env=False),
        FailingCodex(),  # type: ignore[abstract]
        feishu,
    )

    await bridge._send_error_to_feishu("om_1", "oc_1", "", TimeoutError())

    assert feishu.texts == [("om_1", "oc_1", "Codex bridge error: TimeoutError")]


@pytest.mark.anyio
async def test_slow_messages_get_progress_reply() -> None:
    feishu = FakeFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(
            _env_file=None,
            use_hermes_feishu_env=False,
            feishu_progress_seconds=0.01,
            feishu_progress_text="Working...",
        ),
        SlowCodex(),  # type: ignore[abstract]
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

    assert feishu.texts == [("om_1", "oc_1", "Working..."), ("om_1", "oc_1", "done")]


@pytest.mark.anyio
async def test_slow_messages_get_repeated_progress_replies() -> None:
    feishu = FakeFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(
            _env_file=None,
            use_hermes_feishu_env=False,
            feishu_progress_seconds=0.01,
            feishu_progress_text="Working...",
        ),
        SlowerCodex(),  # type: ignore[abstract]
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

    assert feishu.texts[:2] == [("om_1", "oc_1", "Working..."), ("om_1", "oc_1", "Working...")]
    assert feishu.texts[-1] == ("om_1", "oc_1", "done")


@pytest.mark.anyio
async def test_stream_updates_are_not_forwarded_by_default() -> None:
    feishu = FakeFeishu()
    codex = StreamingCodex()
    bridge = FeishuWebsocketBridge(
        Settings(_env_file=None, use_hermes_feishu_env=False),
        codex,
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

    assert codex.loaded_conversations == []
    assert feishu.texts == [("om_1", "oc_1", "done")]


@pytest.mark.anyio
async def test_opt_in_stream_updates_are_forwarded_to_feishu() -> None:
    feishu = FakeFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(
            _env_file=None,
            use_hermes_feishu_env=False,
            feishu_stream_updates_enabled=True,
            feishu_show_reasoning=True,
            feishu_stream_flush_seconds=0,
        ),
        StreamingCodex(),
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

    assert feishu.texts == [
        ("om_1", "oc_1", "Thinking:\nChecking the repo."),
        ("om_1", "oc_1", "Action:\nRunning command: npm test"),
        ("om_1", "oc_1", "Update:\nI found the relevant path."),
        ("om_1", "oc_1", "done"),
    ]


@pytest.mark.anyio
async def test_p2p_stream_updates_use_sender_open_id_for_normal_messages() -> None:
    feishu = FakeFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(
            _env_file=None,
            use_hermes_feishu_env=False,
            feishu_stream_updates_enabled=True,
            feishu_show_reasoning=True,
            feishu_stream_flush_seconds=0,
        ),
        StreamingCodex(),
        feishu,
    )
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
            message=SimpleNamespace(
                message_id="om_1",
                chat_id="oc_1",
                chat_type="p2p",
                message_type=SimpleNamespace(value="text"),
                content='{"text":"hello"}',
            ),
        ),
    )

    await bridge._handle_message_event(data)

    assert feishu.text_targets == [
        ("om_1", "oc_1", "ou_1", "Thinking:\nChecking the repo."),
        ("om_1", "oc_1", "ou_1", "Action:\nRunning command: npm test"),
        ("om_1", "oc_1", "ou_1", "Update:\nI found the relevant path."),
        ("om_1", "oc_1", "ou_1", "done"),
    ]


@pytest.mark.anyio
async def test_failed_stream_update_does_not_fail_final_reply() -> None:
    feishu = StreamFailingFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(
            _env_file=None,
            use_hermes_feishu_env=False,
            feishu_stream_updates_enabled=True,
            feishu_show_reasoning=True,
            feishu_stream_flush_seconds=0,
        ),
        StreamingCodex(),
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

    assert ("om_1", "oc_1", "done") in feishu.texts
    assert all(not text.startswith("Action:\n") for _, _, text in feishu.texts)


@pytest.mark.anyio
async def test_opt_in_history_replay_is_forwarded_to_feishu() -> None:
    feishu = FakeFeishu()
    bridge = FeishuWebsocketBridge(
        Settings(
            _env_file=None,
            use_hermes_feishu_env=False,
            feishu_stream_updates_enabled=True,
            feishu_show_history=True,
            feishu_stream_flush_seconds=0,
        ),
        StreamingCodex(),
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

    assert feishu.texts[0] == ("om_1", "oc_1", "History:\nCodex: previous")
    assert feishu.texts[-1] == ("om_1", "oc_1", "done")


@pytest.mark.anyio
async def test_opt_in_feishu_history_is_attached_to_next_codex_message() -> None:
    feishu = FakeFeishu()
    feishu.history_messages = [
        FeishuHistoryMessage(
            message_id="om_old",
            create_time=1,
            sender="User",
            text="previous Feishu message",
        )
    ]
    codex = ContextCodex()
    bridge = FeishuWebsocketBridge(
        Settings(
            _env_file=None,
            use_hermes_feishu_env=False,
            codex_load_history_on_start=False,
            feishu_seed_history_to_codex=True,
            feishu_history_max_messages=12,
            feishu_history_lookback_seconds=3600,
        ),
        codex,
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
                content='{"text":"current"}',
            ),
        ),
    )

    await bridge._handle_message_event(data)

    assert feishu.history_requests == [("oc_1", "om_1", 12, 3600)]
    assert len(codex.calls) == 1
    assert codex.calls[0][0:2] == ("ask", "oc_1")
    assert "Feishu chat history before the current request." in codex.calls[0][2]
    assert "User: previous Feishu message" in codex.calls[0][2]
    assert "Current Feishu message to answer:\ncurrent" in codex.calls[0][2]
    assert feishu.texts == [("om_1", "oc_1", "done")]
