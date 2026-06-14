from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .config import Settings
from .feishu_client import FeishuClient


ApprovalHandler = Callable[["ApprovalRequest"], Awaitable[bool]]


@dataclass(slots=True)
class ApprovalRequest:
    conversation_id: str
    kind: str
    method: str
    title: str
    details: str
    params: dict[str, Any]


@dataclass(slots=True)
class PendingApproval:
    conversation_id: str
    kind: str
    title: str
    details: str
    future: asyncio.Future[bool]


class FeishuApprovalCoordinator:
    def __init__(self, settings: Settings, feishu: FeishuClient) -> None:
        self.settings = settings
        self.feishu = feishu
        self._pending: dict[str, PendingApproval] = {}
        self._message_ids: dict[str, str] = {}

    def set_current_message(self, conversation_id: str, message_id: str) -> None:
        self._message_ids[conversation_id] = message_id

    async def request_approval(self, request: ApprovalRequest) -> bool:
        if not self.settings.feishu_approval_enabled:
            return False
        existing = self._pending.get(request.conversation_id)
        if existing and not existing.future.done():
            existing.future.set_result(False)

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        pending = PendingApproval(
            conversation_id=request.conversation_id,
            kind=request.kind,
            title=request.title,
            details=request.details,
            future=future,
        )
        self._pending[request.conversation_id] = pending
        await self._send_approval_prompt(pending)
        try:
            return await asyncio.wait_for(future, timeout=self.settings.feishu_approval_timeout_seconds)
        except asyncio.TimeoutError:
            return False
        finally:
            if self._pending.get(request.conversation_id) is pending:
                del self._pending[request.conversation_id]

    async def resolve_from_text(self, conversation_id: str, message_id: str, text: str) -> bool:
        parts = text.strip().lower().split()
        if not parts or parts[0] not in {"approve", "deny", "reject"}:
            return False

        pending = self._pending.get(conversation_id)
        if pending is None or pending.future.done():
            await self.feishu.deliver_text(
                message_id=message_id,
                chat_id=conversation_id,
                text="No pending Codex approval request.",
            )
            return True

        approved = parts[0] == "approve"
        pending.future.set_result(approved)
        await self.feishu.deliver_text(
            message_id=message_id,
            chat_id=conversation_id,
            text=f"{'Approved' if approved else 'Denied'} Codex request.",
        )
        return True

    async def _send_approval_prompt(self, pending: PendingApproval) -> None:
        message_id = self._message_ids.get(pending.conversation_id, "")
        text = (
            "Codex approval required.\n\n"
            f"Type: {pending.kind}\n"
            f"Request: {pending.title}\n\n"
            f"{pending.details}\n\n"
            "Reply `approve` to allow once, or `deny` to reject.\n"
            f"Timeout: {int(self.settings.feishu_approval_timeout_seconds)} seconds."
        )
        await self.feishu.deliver_text(message_id=message_id, chat_id=pending.conversation_id, text=text)


def approval_request_from_server(
    *,
    conversation_id: str,
    method: str,
    params: dict[str, Any],
) -> ApprovalRequest:
    return ApprovalRequest(
        conversation_id=conversation_id,
        kind=_approval_kind(method),
        method=method,
        title=_approval_title(method, params),
        details=_approval_details(method, params),
        params=params,
    )


def _approval_kind(method: str) -> str:
    if method in {"item/commandExecution/requestApproval", "execCommandApproval"}:
        return "command"
    if method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
        return "file change"
    if method == "mcpServer/elicitation/request":
        return "tool access"
    return method


def _approval_title(method: str, params: dict[str, Any]) -> str:
    command = params.get("command") or params.get("cmd")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    prompt = params.get("prompt") or params.get("message")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()[:500]
    server = params.get("server") or params.get("serverName") or params.get("mcpServerName")
    tool = params.get("tool") or params.get("toolName") or params.get("name")
    if server or tool:
        return " ".join(str(part) for part in (server, tool) if part)
    return method


def _approval_details(method: str, params: dict[str, Any]) -> str:
    display: dict[str, Any] = {}
    for key in (
        "command",
        "cmd",
        "cwd",
        "reason",
        "justification",
        "server",
        "serverName",
        "mcpServerName",
        "tool",
        "toolName",
        "name",
        "prompt",
        "message",
    ):
        if key in params:
            display[key] = params[key]
    if not display:
        display = params
    serialized = json.dumps(display, ensure_ascii=False, indent=2, default=str)
    return serialized[:3000]
