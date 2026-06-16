from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FeishuMessage:
    event_id: str
    chat_id: str
    chat_type: str
    message_id: str
    sender_id: str
    message_type: str
    text: str
    content: dict[str, Any]


def parse_feishu_event(payload: dict[str, Any]) -> FeishuMessage | None:
    if payload.get("type") == "url_verification":
        return None

    header = payload.get("header") or {}
    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}

    message_type = message.get("message_type")
    if message_type not in {"text", "image", "file"}:
        return None

    content = message.get("content") or "{}"
    try:
        content_data = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        content_data = {"text": str(content)}

    text = _clean_feishu_text(content_data.get("text") or "")
    if message_type == "text" and not text:
        return None

    return FeishuMessage(
        event_id=str(header.get("event_id") or message.get("message_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
        message_id=str(message.get("message_id") or ""),
        sender_id=str(
            (sender.get("sender_id") or {}).get("open_id")
            or (sender.get("sender_id") or {}).get("user_id")
            or ""
        ),
        message_type=str(message_type),
        text=text,
        content=content_data if isinstance(content_data, dict) else {},
    )


def parse_lark_message_event(data: Any) -> FeishuMessage | None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    if message is None or sender_id is None:
        return None

    message_type = getattr(message, "message_type", "")
    message_type_value = getattr(message_type, "value", message_type)
    message_type_name = str(message_type_value)
    if message_type_name not in {"text", "image", "file"}:
        return None

    content = getattr(message, "content", "") or "{}"
    try:
        content_data = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        content_data = {"text": str(content)}

    text = _clean_feishu_text(content_data.get("text") or "")
    if message_type_name == "text" and not text:
        return None

    header = getattr(data, "header", None)
    chat_type = getattr(message, "chat_type", "")
    chat_type_value = getattr(chat_type, "value", chat_type)
    return FeishuMessage(
        event_id=str(getattr(header, "event_id", "") or getattr(message, "message_id", "") or ""),
        chat_id=str(getattr(message, "chat_id", "") or ""),
        chat_type=str(chat_type_value or ""),
        message_id=str(getattr(message, "message_id", "") or ""),
        sender_id=str(
            getattr(sender_id, "open_id", None)
            or getattr(sender_id, "user_id", None)
            or getattr(sender_id, "union_id", None)
            or ""
        ),
        message_type=message_type_name,
        text=text,
        content=content_data if isinstance(content_data, dict) else {},
    )


def challenge_response(payload: dict[str, Any]) -> dict[str, str] | None:
    if payload.get("type") == "url_verification" and payload.get("challenge"):
        return {"challenge": str(payload["challenge"])}
    return None


def _clean_feishu_text(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split()).strip()
