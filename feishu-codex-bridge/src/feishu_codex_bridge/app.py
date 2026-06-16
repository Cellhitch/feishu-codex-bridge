from __future__ import annotations

import json

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from .codex_client import create_codex_client
from .codex_client import CodexClient
from .config import Settings
from .feishu_client import FeishuClient
from .feishu_events import challenge_response, parse_feishu_event
from .security import verify_feishu_signature, verify_verification_token


class SimulateRequest(BaseModel):
    text: str
    conversation_id: str = "local-test"


def create_app(
    settings: Settings | None = None,
    codex_client: CodexClient | None = None,
    feishu_client: FeishuClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    codex = codex_client or create_codex_client(settings)
    feishu = feishu_client or FeishuClient(settings)
    app = FastAPI(title="Feishu Codex Bridge", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "codex_binary": str(settings.codex_binary),
            "codex_use_app_server": settings.codex_use_app_server,
            "dry_run_replies": settings.dry_run_replies,
        }

    @app.post("/simulate")
    async def simulate(request: SimulateRequest) -> dict[str, str]:
        response = await codex.ask(request.text, request.conversation_id)
        return {"response": response}

    @app.post("/feishu/events")
    async def feishu_events(
        request: Request,
        x_lark_request_timestamp: str | None = Header(default=None),
        x_lark_request_nonce: str | None = Header(default=None),
        x_lark_signature: str | None = Header(default=None),
    ) -> dict[str, object]:
        body = await request.body()
        if not verify_feishu_signature(
            timestamp=x_lark_request_timestamp,
            nonce=x_lark_request_nonce,
            body=body,
            signature=x_lark_signature,
            encrypt_key=settings.feishu_encrypt_key,
        ):
            raise HTTPException(status_code=401, detail="Invalid Feishu signature")

        payload = json.loads(body.decode("utf-8") or "{}")
        challenge = challenge_response(payload)
        if challenge:
            return challenge

        if not verify_verification_token(payload.get("token"), settings.feishu_verification_token):
            raise HTTPException(status_code=401, detail="Invalid Feishu verification token")

        message = parse_feishu_event(payload)
        if message is None:
            return {"ok": True, "ignored": True}

        response = await codex.ask(message.text, message.chat_id or message.sender_id)
        recipient_open_id = message.sender_id if _is_p2p_chat(message.chat_type) else ""
        await feishu.deliver_response(
            message_id=message.message_id,
            chat_id=message.chat_id,
            open_id=recipient_open_id,
            text=response,
        )
        return {"ok": True, "message_id": message.message_id, "dry_run": settings.dry_run_replies}

    return app


def _is_p2p_chat(chat_type: str) -> bool:
    return "p2p" in chat_type.strip().lower()
