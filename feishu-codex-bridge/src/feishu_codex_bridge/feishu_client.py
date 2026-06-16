from __future__ import annotations

import json
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


MARKDOWN_LOCAL_PATH_PATTERN = re.compile(r"!?\[[^\]]*]\((/[^)\n\r]+)\)")
BACKTICK_LOCAL_PATH_PATTERN = re.compile(r"`(/[^`\n\r]+)`")
PLAIN_LOCAL_PATH_PATTERN = re.compile(r"(?<![\w`])(/[^\s)\n\r\t]+)")


@dataclass(frozen=True)
class FeishuHistoryMessage:
    message_id: str
    create_time: int
    sender: str
    text: str


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tenant_access_token: str | None = None
        self._token_expires_at = 0.0
        self._reply_only_chat_ids: set[str] = set()

    async def reply_to_message(self, message_id: str, text: str) -> None:
        await self.reply_content_to_message(message_id, "text", {"text": f"{self.settings.reply_prefix}{text}"})

    async def reply_content_to_message(self, message_id: str, msg_type: str, content: dict[str, Any]) -> None:
        if self.settings.dry_run_replies:
            return
        token = await self._tenant_token()
        url = f"{self.settings.feishu_domain}/open-apis/im/v1/messages/{message_id}/reply"
        payload = _message_payload(msg_type, content)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            _feishu_json_response(response, "Feishu reply")

    async def send_text_to_chat(self, chat_id: str, text: str) -> None:
        if self.settings.dry_run_replies:
            return
        token = await self._tenant_token()
        url = f"{self.settings.feishu_domain}/open-apis/im/v1/messages"
        await self._send_message_to_chat(token=token, url=url, chat_id=chat_id, payload=self._text_reply_payload(text))

    async def send_image_to_chat(self, chat_id: str, image_path: Path) -> None:
        image_key = await self.upload_image(image_path)
        await self._send_chat_content(chat_id=chat_id, msg_type="image", content={"image_key": image_key})

    async def send_file_to_chat(self, chat_id: str, file_path: Path) -> None:
        file_key = await self.upload_file(file_path)
        await self._send_chat_content(chat_id=chat_id, msg_type="file", content={"file_key": file_key})

    async def list_recent_text_messages(
        self,
        *,
        chat_id: str,
        before_message_id: str = "",
        limit: int = 20,
        lookback_seconds: int = 24 * 60 * 60,
    ) -> list[FeishuHistoryMessage]:
        if self.settings.dry_run_replies or not chat_id:
            return []
        page_size = min(max(limit + 1, 1), 50)
        now = int(time.time())
        start_time = max(0, now - max(1, int(lookback_seconds)))
        token = await self._tenant_token()
        url = f"{self.settings.feishu_domain}/open-apis/im/v1/messages"
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(start_time),
            "end_time": str(now),
            "sort_type": "ByCreateTimeDesc",
            "page_size": page_size,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        data = _feishu_json_response(response, "Feishu message history")
        items = (data.get("data") or {}).get("items") or []
        messages: list[FeishuHistoryMessage] = []
        for item in items:
            parsed = _history_message_from_api_item(item, before_message_id=before_message_id)
            if parsed is not None:
                messages.append(parsed)
            if len(messages) >= limit:
                break
        messages.sort(key=lambda message: message.create_time)
        return messages

    async def upload_image(self, image_path: Path) -> str:
        token = await self._tenant_token()
        url = f"{self.settings.feishu_domain}/open-apis/im/v1/images"
        with image_path.open("rb") as image_file:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (image_path.name, image_file, _mime_type(image_path))},
                )
        data = _feishu_json_response(response, "Feishu upload image")
        image_key = (data.get("data") or {}).get("image_key") or data.get("image_key")
        if not image_key:
            raise RuntimeError(f"Feishu upload image response missing image_key: {data}")
        return str(image_key)

    async def upload_file(self, file_path: Path) -> str:
        token = await self._tenant_token()
        url = f"{self.settings.feishu_domain}/open-apis/im/v1/files"
        with file_path.open("rb") as file_handle:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "file_type": "stream",
                        "file_name": file_path.name,
                    },
                    files={"file": (file_path.name, file_handle, _mime_type(file_path))},
                )
        data = _feishu_json_response(response, "Feishu upload file")
        file_key = (data.get("data") or {}).get("file_key") or data.get("file_key")
        if not file_key:
            raise RuntimeError(f"Feishu upload file response missing file_key: {data}")
        return str(file_key)

    async def _send_chat_content(self, *, chat_id: str, msg_type: str, content: dict[str, Any]) -> None:
        if self.settings.dry_run_replies:
            return
        token = await self._tenant_token()
        url = f"{self.settings.feishu_domain}/open-apis/im/v1/messages"
        payload = {
            "receive_id": chat_id,
            **_message_payload(msg_type, content),
        }
        await self._send_message_to_chat(token=token, url=url, chat_id=chat_id, payload=payload)

    async def _send_message_to_chat(
        self,
        *,
        token: str,
        url: str,
        chat_id: str,
        payload: dict[str, str],
    ) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": "chat_id"},
                json=payload,
            )
            _feishu_json_response(response, "Feishu send message")

    async def deliver_text(self, *, message_id: str, chat_id: str, text: str) -> None:
        delivery_mode = self.settings.feishu_delivery_mode.strip().lower()
        if delivery_mode == "reply" or not chat_id or chat_id in self._reply_only_chat_ids:
            await self.reply_to_message(message_id, text)
            return
        if delivery_mode == "send":
            try:
                await self.send_text_to_chat(chat_id, text)
            except RuntimeError as error:
                if not _is_invalid_receive_id_error(error):
                    raise
                self._reply_only_chat_ids.add(chat_id)
                await self.reply_to_message(message_id, text)
            return
        raise RuntimeError(f"Unsupported Feishu delivery mode: {self.settings.feishu_delivery_mode}")

    async def deliver_response(self, *, message_id: str, chat_id: str, text: str) -> None:
        paths = _extract_existing_local_paths(text)
        visible_text = _strip_local_path_markup(text, paths).strip()
        if visible_text:
            await self.deliver_text(message_id=message_id, chat_id=chat_id, text=visible_text)
        elif not paths:
            await self.deliver_text(message_id=message_id, chat_id=chat_id, text=text)

        for path in paths:
            if not chat_id:
                await self.deliver_text(
                    message_id=message_id,
                    chat_id=chat_id,
                    text=f"Created local file: {path}",
                )
            elif _is_image_path(path):
                image_key = await self.upload_image(path)
                if chat_id in self._reply_only_chat_ids:
                    await self.reply_content_to_message(message_id, "image", {"image_key": image_key})
                    continue
                try:
                    await self._send_chat_content(chat_id=chat_id, msg_type="image", content={"image_key": image_key})
                except RuntimeError as error:
                    if not _is_invalid_receive_id_error(error):
                        raise
                    self._reply_only_chat_ids.add(chat_id)
                    await self.reply_content_to_message(message_id, "image", {"image_key": image_key})
            else:
                file_key = await self.upload_file(path)
                if chat_id in self._reply_only_chat_ids:
                    await self.reply_content_to_message(message_id, "file", {"file_key": file_key})
                    continue
                try:
                    await self._send_chat_content(chat_id=chat_id, msg_type="file", content={"file_key": file_key})
                except RuntimeError as error:
                    if not _is_invalid_receive_id_error(error):
                        raise
                    self._reply_only_chat_ids.add(chat_id)
                    await self.reply_content_to_message(message_id, "file", {"file_key": file_key})

    async def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        suggested_name: str = "",
    ) -> Path:
        token = await self._tenant_token()
        safe_message_id = _safe_filename(message_id) or "message"
        encoded_message_id = quote(message_id, safe="")
        safe_file_key = quote(file_key, safe="")
        url = (
            f"{self.settings.feishu_domain}/open-apis/im/v1/messages/"
            f"{encoded_message_id}/resources/{safe_file_key}"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"type": resource_type},
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise RuntimeError(
                    f"Feishu media download failed: {response.status_code} {response.text}"
                ) from error
        if len(response.content) > self.settings.max_media_bytes:
            raise RuntimeError(
                f"Feishu media exceeds limit: {len(response.content)} > {self.settings.max_media_bytes} bytes"
            )
        media_dir = self.settings.media_dir
        if not media_dir.is_absolute():
            media_dir = Path.cwd() / media_dir
        media_dir.mkdir(parents=True, exist_ok=True)
        extension = _extension_from_content_type(response.headers.get("content-type", ""), resource_type)
        if resource_type == "image":
            extension = _extension_from_bytes(response.content) or extension
        filename = _safe_filename(suggested_name) or f"{safe_message_id}-{_safe_filename(file_key)}{extension}"
        if not Path(filename).suffix and extension:
            filename = f"{filename}{extension}"
        path = _unique_path(media_dir / filename)
        path.write_bytes(response.content)
        return path

    def _text_reply_payload(self, text: str) -> dict[str, str]:
        return _message_payload("text", {"text": f"{self.settings.reply_prefix}{text}"})

    async def _tenant_token(self) -> str:
        if self._tenant_access_token and time.time() < self._token_expires_at:
            return self._tenant_access_token
        url = f"{self.settings.feishu_domain}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.settings.feishu_app_id,
            "app_secret": self.settings.feishu_app_secret,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            data = _feishu_json_response(response, "Feishu tenant token")
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError(f"Feishu token response missing token: {data}")
        self._tenant_access_token = token
        self._token_expires_at = time.time() + max(60, int(data.get("expire", 7200)) - 120)
        return token


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._- " else "_" for char in value).strip()
    return safe[:160].strip(". ") or ""


def _history_message_from_api_item(
    item: dict[str, Any],
    *,
    before_message_id: str = "",
) -> FeishuHistoryMessage | None:
    if item.get("deleted"):
        return None
    message_id = str(item.get("message_id") or "")
    if before_message_id and message_id == before_message_id:
        return None
    msg_type = str(item.get("msg_type") or "")
    body = item.get("body") or {}
    text = _history_content_text(msg_type, str(body.get("content") or "")).strip()
    if not text:
        return None
    sender = item.get("sender") or {}
    sender_name = str(sender.get("sender_name") or sender.get("sender_type") or sender.get("id") or "unknown")
    return FeishuHistoryMessage(
        message_id=message_id,
        create_time=int(item.get("create_time") or 0),
        sender=sender_name,
        text=text,
    )


def _history_content_text(msg_type: str, raw_content: str) -> str:
    if not raw_content:
        return ""
    try:
        content = json.loads(raw_content)
    except ValueError:
        content = {}
    if msg_type == "text":
        return str(content.get("text") or "")
    if msg_type == "post":
        return _extract_nested_text(content)
    if msg_type == "file":
        return f"[file: {content.get('file_name') or content.get('name') or 'unknown'}]"
    if msg_type == "image":
        return "[image]"
    if msg_type:
        return f"[{msg_type} message]"
    return ""


def _extract_nested_text(value: Any) -> str:
    parts: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("text", "un_escape"):
                text = node.get(key)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return " ".join(parts)


def _message_payload(msg_type: str, content: dict[str, Any]) -> dict[str, str]:
    return {
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }


def _is_invalid_receive_id_error(error: RuntimeError) -> bool:
    text = str(error)
    return "code=230001" in text or "invalid receive_id" in text


def _feishu_json_response(response: httpx.Response, action: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as error:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(f"{action} failed: HTTP {response.status_code} {response.text}") from error
        raise RuntimeError(f"{action} returned non-JSON response: {response.text}") from error
    code = data.get("code")
    if code not in (None, 0):
        message = data.get("msg") or data.get("message") or "unknown Feishu error"
        raise RuntimeError(f"{action} failed: code={code} message={message} response={data}")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise RuntimeError(f"{action} failed: HTTP {response.status_code} {response.text}") from error
    return data


def _extract_existing_local_paths(text: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_path in _candidate_local_paths(text):
        raw_path = raw_path.strip().strip("`'\"<>")
        path = Path(raw_path)
        if not path.is_absolute() or not path.exists() or not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)
    return paths


def _candidate_local_paths(text: str) -> list[str]:
    candidates: list[str] = []
    for pattern in (MARKDOWN_LOCAL_PATH_PATTERN, BACKTICK_LOCAL_PATH_PATTERN, PLAIN_LOCAL_PATH_PATTERN):
        candidates.extend(match.group(1) for match in pattern.finditer(text))
    return candidates


def _strip_local_path_markup(text: str, paths: list[Path]) -> str:
    stripped = text
    for path in paths:
        escaped = re.escape(str(path))
        stripped = re.sub(rf"!\[[^\]]*]\({escaped}\)", "", stripped)
        stripped = stripped.replace(str(path), "")
    return "\n".join(line.rstrip() for line in stripped.splitlines()).strip()


def _is_image_path(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _extension_from_content_type(content_type: str, resource_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/csv": ".csv",
    }
    if normalized in extensions:
        return extensions[normalized]
    return ".bin" if resource_type == "file" else ".img"


def _extension_from_bytes(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return ""


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
