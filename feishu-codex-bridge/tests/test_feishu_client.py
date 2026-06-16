import json
from pathlib import Path

import httpx
import pytest

from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.feishu_client import (
    FeishuClient,
    _extract_existing_local_paths,
    _extension_from_bytes,
    _extension_from_content_type,
    _feishu_json_response,
    _history_message_from_api_item,
    _strip_local_path_markup,
    _safe_filename,
)


def test_reply_payload_uses_feishu_content_string() -> None:
    settings = Settings(_env_file=None, use_hermes_feishu_env=False, reply_prefix="[Codex] ")
    client = FeishuClient(settings)

    payload = client._text_reply_payload("hello")

    assert payload["msg_type"] == "text"
    assert isinstance(payload["content"], str)
    assert json.loads(payload["content"]) == {"text": "[Codex] hello"}


def test_default_delivery_mode_sends_chat_messages() -> None:
    settings = Settings(_env_file=None, use_hermes_feishu_env=False)

    assert settings.feishu_delivery_mode == "send"


def test_media_filename_helpers() -> None:
    assert _safe_filename("../bad:name?.pdf") == "_bad_name_.pdf"
    assert _extension_from_content_type("image/png; charset=utf-8", "image") == ".png"
    assert _extension_from_content_type("application/octet-stream", "file") == ".bin"
    assert _extension_from_bytes(b"\xff\xd8\xff\xe0rest") == ".jpg"


def test_extracts_and_strips_existing_local_paths(tmp_path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    text = f"Here it is:\n\n![desktop screenshot]({image})"

    paths = _extract_existing_local_paths(text)

    assert paths == [image.resolve()]
    assert _strip_local_path_markup(text, paths) == "Here it is:"


def test_extracts_markdown_local_paths_with_spaces(tmp_path) -> None:
    folder = tmp_path / "New project 2"
    folder.mkdir()
    image = folder / "screen shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    text = f"Here it is:\n\n![desktop screenshot]({image})"

    paths = _extract_existing_local_paths(text)

    assert paths == [image.resolve()]
    assert _strip_local_path_markup(text, paths) == "Here it is:"


def test_history_message_from_api_item_parses_text_and_skips_current() -> None:
    item = {
        "message_id": "om_old",
        "create_time": 123,
        "msg_type": "text",
        "sender": {"sender_name": "User"},
        "body": {"content": '{"text":"hello from Feishu"}'},
    }

    parsed = _history_message_from_api_item(item, before_message_id="om_current")

    assert parsed is not None
    assert parsed.message_id == "om_old"
    assert parsed.sender == "User"
    assert parsed.text == "hello from Feishu"
    assert _history_message_from_api_item(item, before_message_id="om_old") is None


@pytest.mark.anyio
async def test_deliver_response_sends_text_and_image(tmp_path) -> None:
    class FakeFeishu(FeishuClient):
        def __init__(self) -> None:
            super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
            self.texts: list[tuple[str, str]] = []
            self.images: list[tuple[str, str, dict[str, str]]] = []

        async def send_text_to_chat(self, chat_id: str, text: str) -> None:
            self.texts.append((chat_id, text))

        async def upload_image(self, image_path: Path) -> str:
            return "img_key"

        async def _send_chat_content(self, *, chat_id: str, msg_type: str, content: dict[str, str]) -> None:
            self.images.append((chat_id, msg_type, content))

    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    client = FakeFeishu()

    await client.deliver_response(message_id="om_1", chat_id="oc_1", text=f"Here:\n![shot]({image})")

    assert client.texts == [("oc_1", "Here:")]
    assert client.images == [("oc_1", "image", {"image_key": "img_key"})]


def test_feishu_json_response_raises_on_application_error() -> None:
    response = httpx.Response(
        400,
        json={"code": 230001, "msg": "invalid receive_id"},
        request=httpx.Request("POST", "https://open.feishu.cn/open-apis/im/v1/messages/id/reply"),
    )

    with pytest.raises(RuntimeError, match="code=230001"):
        _feishu_json_response(response, "Feishu reply")


@pytest.mark.anyio
async def test_deliver_text_prefers_open_id_for_send_mode() -> None:
    class FakeFeishu(FeishuClient):
        def __init__(self) -> None:
            super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
            self.open_id_sends: list[tuple[str, str]] = []
            self.chat_sends: list[tuple[str, str]] = []
            self.replies: list[tuple[str, str]] = []

        async def send_text_to_open_id(self, open_id: str, text: str) -> None:
            self.open_id_sends.append((open_id, text))

        async def send_text_to_chat(self, chat_id: str, text: str) -> None:
            self.chat_sends.append((chat_id, text))

        async def reply_to_message(self, message_id: str, text: str) -> None:
            self.replies.append((message_id, text))

    client = FakeFeishu()

    await client.deliver_text(message_id="om_1", chat_id="oc_1", open_id="ou_1", text="normal")

    assert client.open_id_sends == [("ou_1", "normal")]
    assert client.chat_sends == []
    assert client.replies == []


@pytest.mark.anyio
async def test_deliver_text_falls_back_from_open_id_to_chat_id() -> None:
    class FakeFeishu(FeishuClient):
        def __init__(self) -> None:
            super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
            self.chat_sends: list[tuple[str, str]] = []

        async def send_text_to_open_id(self, open_id: str, text: str) -> None:
            raise RuntimeError("Feishu send message failed: code=230001 message=invalid receive_id")

        async def send_text_to_chat(self, chat_id: str, text: str) -> None:
            self.chat_sends.append((chat_id, text))

    client = FakeFeishu()

    await client.deliver_text(message_id="om_1", chat_id="oc_1", open_id="ou_bad", text="normal")

    assert client.chat_sends == [("oc_1", "normal")]


@pytest.mark.anyio
async def test_deliver_text_falls_back_to_reply_on_invalid_receive_id() -> None:
    class FakeFeishu(FeishuClient):
        def __init__(self) -> None:
            super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
            self.replies: list[tuple[str, str]] = []

        async def send_text_to_chat(self, chat_id: str, text: str) -> None:
            raise RuntimeError("Feishu send message failed: code=230001 message=invalid receive_id")

        async def reply_to_message(self, message_id: str, text: str) -> None:
            self.replies.append((message_id, text))

    client = FakeFeishu()

    await client.deliver_text(message_id="om_1", chat_id="oc_bad", text="fallback")

    assert client.replies == [("om_1", "fallback")]


@pytest.mark.anyio
async def test_deliver_text_remembers_invalid_chat_id() -> None:
    class FakeFeishu(FeishuClient):
        def __init__(self) -> None:
            super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
            self.send_attempts = 0
            self.replies: list[tuple[str, str]] = []

        async def send_text_to_chat(self, chat_id: str, text: str) -> None:
            self.send_attempts += 1
            raise RuntimeError("Feishu send message failed: code=230001 message=invalid receive_id")

        async def reply_to_message(self, message_id: str, text: str) -> None:
            self.replies.append((message_id, text))

    client = FakeFeishu()

    await client.deliver_text(message_id="om_1", chat_id="oc_bad", text="first")
    await client.deliver_text(message_id="om_2", chat_id="oc_bad", text="second")

    assert client.send_attempts == 1
    assert client.replies == [("om_1", "first"), ("om_2", "second")]


@pytest.mark.anyio
async def test_deliver_response_image_falls_back_to_reply(tmp_path) -> None:
    class FakeFeishu(FeishuClient):
        def __init__(self) -> None:
            super().__init__(Settings(_env_file=None, use_hermes_feishu_env=False, dry_run_replies=False))
            self.replies: list[tuple[str, str, dict[str, str]]] = []

        async def upload_image(self, image_path: Path) -> str:
            return "img_key"

        async def _send_chat_content(self, *, chat_id: str, msg_type: str, content: dict[str, str]) -> None:
            raise RuntimeError("Feishu send message failed: code=230001 message=invalid receive_id")

        async def reply_content_to_message(self, message_id: str, msg_type: str, content: dict[str, str]) -> None:
            self.replies.append((message_id, msg_type, content))

    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    client = FakeFeishu()

    await client.deliver_response(message_id="om_1", chat_id="oc_bad", text=f"![shot]({image})")

    assert client.replies == [("om_1", "image", {"image_key": "img_key"})]
