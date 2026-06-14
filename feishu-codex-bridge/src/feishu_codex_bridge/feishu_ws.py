from __future__ import annotations

import asyncio
import logging
import signal
from concurrent.futures import Future
from typing import Any

from .approval import FeishuApprovalCoordinator
from .codex_client import CodexClient
from .config import Settings
from .feishu_client import FeishuClient
from .feishu_events import parse_lark_message_event

logger = logging.getLogger(__name__)


class FeishuWebsocketBridge:
    def __init__(
        self,
        settings: Settings,
        codex: CodexClient,
        feishu: FeishuClient,
        approvals: FeishuApprovalCoordinator | None = None,
    ) -> None:
        self.settings = settings
        self.codex = codex
        self.feishu = feishu
        self.approvals = approvals
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        future = self._loop.run_in_executor(None, self._start_client)
        future_waiter = asyncio.wrap_future(future)
        stop_waiter = asyncio.create_task(stop_event.wait())
        logger.info("Feishu websocket bridge starting; waiting for messages")
        done, pending = await asyncio.wait(
            {future_waiter, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if future_waiter in done:
            future_waiter.result()
        future.cancel()

    def _start_client(self) -> None:
        self._build_ws_client().start()

    def _build_ws_client(self) -> Any:
        try:
            import lark_oapi as lark
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
            from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
        except ImportError as error:
            raise RuntimeError("Install Feishu websocket support: uv sync --extra feishu --extra test") from error

        domain = LARK_DOMAIN if "larksuite" in self.settings.feishu_domain else FEISHU_DOMAIN
        event_handler = (
            EventDispatcherHandler.builder(
                self.settings.feishu_encrypt_key,
                self.settings.feishu_verification_token,
            )
            .register_p2_im_message_receive_v1(self._on_message_event)
            .register_p2_im_message_message_read_v1(self._on_message_read_event)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._on_bot_p2p_chat_entered_event)
            .build()
        )
        return FeishuWSClient(
            app_id=self.settings.feishu_app_id,
            app_secret=self.settings.feishu_app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=event_handler,
            domain=domain,
        )

    def _on_message_event(self, data: Any) -> None:
        if self._loop is None:
            logger.warning("Dropping Feishu event before loop is ready")
            return
        future: Future[None] = asyncio.run_coroutine_threadsafe(self._handle_message_event(data), self._loop)
        future.add_done_callback(self._log_failure)

    async def _handle_message_event(self, data: Any) -> None:
        message = parse_lark_message_event(data)
        if message is None:
            return
        conversation_id = message.chat_id or message.sender_id
        if message.message_type == "text" and self.approvals:
            if await self.approvals.resolve_from_text(conversation_id, message.message_id, message.text):
                return
        lock = self._conversation_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            if self.approvals:
                self.approvals.set_current_message(conversation_id, message.message_id)
            logger.info("Feishu %s message %s from %s", message.message_type, message.message_id, conversation_id)
            try:
                response = await self._ask_codex_for_message(message, conversation_id)
                logger.info(
                    "Codex response ready for %s; replying to %s; length=%s preview=%r",
                    conversation_id,
                    message.message_id,
                    len(response),
                    response[:160],
                )
                await self.feishu.deliver_response(message_id=message.message_id, chat_id=message.chat_id, text=response)
                logger.info(
                    "Feishu %s sent for %s",
                    self.settings.feishu_delivery_mode,
                    message.message_id,
                )
            except Exception as error:
                logger.exception("Failed to process Feishu message %s", message.message_id)
                await self._send_error_to_feishu(message.message_id, message.chat_id, error)

    async def _ask_codex_for_message(self, message: Any, conversation_id: str) -> str:
        prompt = await self._message_to_codex_prompt(message)
        if isinstance(prompt, tuple):
            text, image_path = prompt
            return await self.codex.ask_with_image(text, image_path, conversation_id)
        return await self.codex.ask(prompt, conversation_id)

    async def _message_to_codex_prompt(self, message: Any) -> str | tuple[str, Any]:
        if message.message_type == "text":
            return message.text
        if message.message_type == "image":
            image_key = str(message.content.get("image_key") or "")
            if not image_key:
                raise RuntimeError("Feishu image message missing image_key")
            path = await self.feishu.download_message_resource(
                message_id=message.message_id,
                file_key=image_key,
                resource_type="image",
                suggested_name=message.message_id,
            )
            return "The user sent this image from Feishu. Describe or analyze it concisely.", path
        if message.message_type == "file":
            file_key = str(message.content.get("file_key") or "")
            if not file_key:
                raise RuntimeError("Feishu file message missing file_key")
            file_name = str(message.content.get("file_name") or message.content.get("name") or "")
            path = await self.feishu.download_message_resource(
                message_id=message.message_id,
                file_key=file_key,
                resource_type="file",
                suggested_name=file_name,
            )
            return (
                "The user sent a file from Feishu.\n"
                f"Original filename: {file_name or 'unknown'}\n"
                f"Local file path: {path}\n"
                "Please inspect or reason about this file if your current tools can read it, "
                "then reply concisely."
            )
        raise RuntimeError(f"Unsupported Feishu message type: {message.message_type}")

    @staticmethod
    def _on_message_read_event(data: Any) -> None:
        logger.debug("Ignoring Feishu message-read event: %s", type(data).__name__)

    @staticmethod
    def _on_bot_p2p_chat_entered_event(data: Any) -> None:
        logger.debug("Ignoring Feishu bot-p2p-chat-entered event: %s", type(data).__name__)

    async def _send_error_to_feishu(self, message_id: str, chat_id: str, error: Exception) -> None:
        try:
            await self.feishu.deliver_text(
                message_id=message_id,
                chat_id=chat_id,
                text=f"Codex bridge error: {error}",
            )
        except Exception:
            logger.exception("Failed to deliver Feishu error reply for %s", message_id)

    @staticmethod
    def _log_failure(future: Future[None]) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("Failed to process Feishu websocket event")
