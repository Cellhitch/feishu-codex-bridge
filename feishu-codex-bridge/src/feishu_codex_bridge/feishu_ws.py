from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import suppress
from concurrent.futures import Future
from typing import Any

from .approval import FeishuApprovalCoordinator
from .codex_client import CodexClient, CodexStreamEvent, CodexStreamHandler
from .config import Settings
from .feishu_client import FeishuClient, FeishuHistoryMessage
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
        self._seeded_feishu_history: set[str] = set()

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
        await self.codex.prewarm()
        logger.info("Codex client prewarmed")
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
            recipient_open_id = message.sender_id if _is_p2p_chat(message.chat_type) else ""
            stream_relay = _FeishuStreamRelay(
                settings=self.settings,
                feishu=self.feishu,
                message_id=message.message_id,
                chat_id=message.chat_id,
                open_id=recipient_open_id,
            )
            if self.settings.codex_load_history_on_start:
                history = await self.codex.load_conversation(conversation_id)
                if history:
                    logger.info("Loaded %s Codex history item(s) for %s", len(history), conversation_id)
                    await stream_relay.handle_history(history)
            feishu_context = await self._feishu_history_context_for_codex(message, conversation_id)
            progress_task = asyncio.create_task(
                self._send_progress_after_delay(message.message_id, message.chat_id, recipient_open_id)
            )
            try:
                started_at = time.perf_counter()
                response = await self._ask_codex_for_message(
                    message,
                    conversation_id,
                    stream_relay.handle,
                    feishu_context=feishu_context,
                )
                elapsed = time.perf_counter() - started_at
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task
                await stream_relay.flush()
                logger.info(
                    "Codex response ready for %s; replying to %s; seconds=%.2f length=%s preview=%r",
                    conversation_id,
                    message.message_id,
                    elapsed,
                    len(response),
                    response[:160],
                )
                await self.feishu.deliver_response(
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    open_id=recipient_open_id,
                    text=response,
                )
                logger.info(
                    "Feishu %s sent for %s",
                    self.settings.feishu_delivery_mode,
                    message.message_id,
                )
            except Exception as error:
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task
                await stream_relay.flush()
                logger.exception("Failed to process Feishu message %s", message.message_id)
                await self._send_error_to_feishu(message.message_id, message.chat_id, recipient_open_id, error)

    async def _ask_codex_for_message(
        self,
        message: Any,
        conversation_id: str,
        stream_handler: CodexStreamHandler | None = None,
        *,
        feishu_context: str = "",
    ) -> str:
        prompt = await self._message_to_codex_prompt(message)
        if isinstance(prompt, tuple):
            text, image_path = prompt
            if feishu_context:
                text = _with_feishu_context(feishu_context, text)
            return await self.codex.ask_with_image(text, image_path, conversation_id)
        if feishu_context:
            prompt = _with_feishu_context(feishu_context, prompt)
        return await self.codex.ask_stream(prompt, conversation_id, stream_handler)

    async def _feishu_history_context_for_codex(self, message: Any, conversation_id: str) -> str:
        if not self.settings.feishu_seed_history_to_codex:
            return ""
        if conversation_id in self._seeded_feishu_history:
            return ""
        if not message.chat_id:
            return ""
        try:
            history = await self.feishu.list_recent_text_messages(
                chat_id=message.chat_id,
                before_message_id=message.message_id,
                limit=self.settings.feishu_history_max_messages,
                lookback_seconds=self.settings.feishu_history_lookback_seconds,
            )
            text = _format_feishu_history_for_codex(history, max_chars=self.settings.feishu_history_max_chars)
            self._seeded_feishu_history.add(conversation_id)
            if text:
                logger.info("Attached %s Feishu history message(s) to next Codex turn for %s", len(history), conversation_id)
            return text
        except Exception:
            logger.warning("Failed to attach Feishu history to Codex prompt for %s", conversation_id, exc_info=True)
            return ""

    async def _send_progress_after_delay(self, message_id: str, chat_id: str, open_id: str) -> None:
        delay = self.settings.feishu_progress_seconds
        if delay <= 0:
            return
        text = self.settings.feishu_progress_text.strip()
        if not text:
            return
        while True:
            await asyncio.sleep(delay)
            try:
                await self.feishu.deliver_text(message_id=message_id, chat_id=chat_id, open_id=open_id, text=text)
            except Exception:
                logger.exception("Failed to deliver Feishu progress reply for %s", message_id)

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

    async def _send_error_to_feishu(self, message_id: str, chat_id: str, open_id: str, error: Exception) -> None:
        try:
            detail = str(error).strip() or error.__class__.__name__
            await self.feishu.deliver_text(
                message_id=message_id,
                chat_id=chat_id,
                open_id=open_id,
                text=f"Codex bridge error: {detail}",
            )
        except Exception:
            logger.exception("Failed to deliver Feishu error reply for %s", message_id)

    @staticmethod
    def _log_failure(future: Future[None]) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("Failed to process Feishu websocket event")


class _FeishuStreamRelay:
    def __init__(
        self,
        *,
        settings: Settings,
        feishu: FeishuClient,
        message_id: str,
        chat_id: str,
        open_id: str,
    ) -> None:
        self.settings = settings
        self.feishu = feishu
        self.message_id = message_id
        self.chat_id = chat_id
        self.open_id = open_id
        self._reasoning_parts: list[str] = []
        self._assistant_parts: list[str] = []
        self._last_flush = 0.0

    async def handle(self, event: CodexStreamEvent) -> None:
        if not self.settings.feishu_stream_updates_enabled:
            return
        if event.kind == "history":
            if self.settings.feishu_show_history:
                await self._send("History", event.text)
            return
        if event.kind == "reasoning":
            if not self.settings.feishu_show_reasoning:
                return
            self._reasoning_parts.append(event.text)
            await self._flush_if_due()
            return
        if event.kind == "assistant_delta":
            if not self.settings.feishu_stream_assistant_deltas:
                return
            self._assistant_parts.append(event.text)
            await self._flush_if_due()
            return
        if event.kind == "assistant":
            await self._send("Update", event.text)
            return
        if event.kind == "plan":
            await self._send("Plan", event.text)
            return
        if event.kind == "tool":
            await self._send("Action", event.text)
            return
        if event.kind == "status":
            if event.text.strip() == "Codex started working.":
                return
            await self._send("Status", event.text)

    async def handle_history(self, history: list[CodexStreamEvent]) -> None:
        if not self.settings.feishu_stream_updates_enabled or not self.settings.feishu_show_history:
            return
        visible = [event.text.strip() for event in history if event.kind == "history" and event.text.strip()]
        if visible:
            await self._send("History", "\n\n".join(visible))

    async def flush(self) -> None:
        await self._flush_buffer("Thinking", self._reasoning_parts)
        await self._flush_buffer("Update", self._assistant_parts)

    async def _flush_if_due(self) -> None:
        now = time.monotonic()
        delay = self.settings.feishu_stream_flush_seconds
        if delay > 0 and now - self._last_flush < delay:
            return
        await self.flush()
        self._last_flush = now

    async def _flush_buffer(self, title: str, parts: list[str]) -> None:
        text = "".join(parts).strip()
        if not text:
            return
        parts.clear()
        await self._send(title, text)

    async def _send(self, title: str, text: str) -> None:
        visible = text.strip()
        if not visible:
            return
        limit = max(100, self.settings.feishu_stream_max_chars)
        if len(visible) > limit:
            visible = f"{visible[: limit - 20].rstrip()}\n...[truncated]"
        try:
            await self.feishu.deliver_text(
                message_id=self.message_id,
                chat_id=self.chat_id,
                open_id=self.open_id,
                text=f"{title}:\n{visible}",
            )
        except Exception:
            logger.warning("Failed to deliver Feishu stream update for %s", self.message_id, exc_info=True)


def _is_p2p_chat(chat_type: str) -> bool:
    return "p2p" in chat_type.strip().lower()


def _format_feishu_history_for_codex(history: list[FeishuHistoryMessage], *, max_chars: int) -> str:
    if not history:
        return ""
    lines = [
        "Feishu chat history before the current request.",
        "Use this as prior context. The next Feishu message is the one to answer.",
        "",
    ]
    for message in history:
        text = " ".join(message.text.split())
        if text:
            lines.append(f"{message.sender}: {text}")
    visible = "\n".join(lines).strip()
    limit = max(500, max_chars)
    if len(visible) <= limit:
        return visible
    return "...[older Feishu history omitted]\n" + visible[-limit:].lstrip()


def _with_feishu_context(context: str, prompt: str) -> str:
    visible_context = context.strip()
    if not visible_context:
        return prompt
    return f"{visible_context}\n\nCurrent Feishu message to answer:\n{prompt}"
