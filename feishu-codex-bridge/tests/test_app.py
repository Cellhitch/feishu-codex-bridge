from fastapi.testclient import TestClient

from feishu_codex_bridge.app import create_app
from feishu_codex_bridge.codex_client import CodexClient
from feishu_codex_bridge.codex_client import CodexAppServerClient, CodexExecClient, create_codex_client
from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.feishu_client import FeishuClient


class FakeCodex(CodexClient):
    async def ask(self, message: str, conversation_id: str) -> str:
        return f"Codex says: {message} ({conversation_id})"


class FakeFeishu(FeishuClient):
    def __init__(self) -> None:
        self.deliveries: list[tuple[str, str, str, str]] = []

    async def deliver_response(self, *, message_id: str, chat_id: str, text: str, open_id: str = "") -> None:
        self.deliveries.append((message_id, chat_id, open_id, text))


def test_health() -> None:
    app = create_app(Settings(dry_run_replies=True), codex_client=FakeCodex())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_simulate_with_override() -> None:
    app = create_app(Settings(dry_run_replies=True), codex_client=FakeCodex())
    client = TestClient(app)

    response = client.post("/simulate", json={"text": "hello", "conversation_id": "c1"})

    assert response.status_code == 200
    assert response.json() == {"response": "Codex says: hello (c1)"}


def test_feishu_event_replies_with_codex_response() -> None:
    fake_feishu = FakeFeishu()
    app = create_app(
        Settings(dry_run_replies=False, feishu_verification_token="token"),
        codex_client=FakeCodex(),
        feishu_client=fake_feishu,
    )
    client = TestClient(app)

    response = client.post(
        "/feishu/events",
        json={
            "token": "token",
            "header": {"event_id": "evt-1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "message_type": "text",
                    "content": "{\"text\":\"hello codex\"}",
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert fake_feishu.deliveries == [("om_1", "oc_1", "", "Codex says: hello codex (oc_1)")]


def test_create_codex_client_selects_app_server() -> None:
    settings = Settings(codex_use_app_server=True)

    assert isinstance(create_codex_client(settings), CodexAppServerClient)


def test_create_codex_client_defaults_to_exec() -> None:
    settings = Settings(codex_use_app_server=False)

    assert isinstance(create_codex_client(settings), CodexExecClient)
