from __future__ import annotations

import asyncio
import inspect
import json
import shlex
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .approval import ApprovalHandler, approval_request_from_server
from .config import Settings
from .thread_store import ThreadStore


@dataclass(frozen=True)
class CodexStreamEvent:
    kind: str
    text: str
    item_id: str | None = None


CodexStreamHandler = Callable[[CodexStreamEvent], Awaitable[None] | None]


class CodexClient(ABC):
    async def prewarm(self) -> None:
        return None

    async def load_conversation(self, conversation_id: str) -> list[CodexStreamEvent]:
        return []

    async def seed_history(self, conversation_id: str, history_text: str) -> bool:
        return False

    @abstractmethod
    async def ask(self, message: str | list[dict[str, Any]], conversation_id: str) -> str:
        raise NotImplementedError

    async def ask_stream(
        self,
        message: str | list[dict[str, Any]],
        conversation_id: str,
        event_handler: CodexStreamHandler | None = None,
    ) -> str:
        return await self.ask(message, conversation_id)

    async def ask_with_image(self, prompt: str, image_path: Path, conversation_id: str) -> str:
        return await self.ask(
            [
                {"type": "text", "text": prompt},
                {"type": "localImage", "path": str(image_path), "detail": "high"},
            ],
            conversation_id,
        )


class CodexExecClient(CodexClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def ask(self, message: str | list[dict[str, Any]], conversation_id: str) -> str:
        if not self.settings.codex_binary.exists():
            raise FileNotFoundError(f"Codex binary not found: {self.settings.codex_binary}")

        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as output_file:
            output_path = Path(output_file.name)

        prompt_text = _input_items_to_text(message) if isinstance(message, list) else message
        prompt = (
            "You are Codex responding through a Feishu chat bridge. "
            "Be concise. If the user asks for local computer/browser/file actions, "
            "explain that this standalone bridge currently routes text through "
            "codex exec; app-server tool execution requires BRIDGE_CODEX_USE_APP_SERVER=true.\n\n"
            f"Feishu conversation: {conversation_id}\n"
            f"User: {prompt_text}"
        )
        command = [
            str(self.settings.codex_binary),
            "exec",
            "--ephemeral",
            "--sandbox",
            self.settings.codex_sandbox,
        ]
        command.extend(_codex_exec_model_args(self.settings.codex_model, self.settings.codex_reasoning_effort))
        command.extend(
            [
                "--output-last-message",
                str(output_path),
                "-",
            ]
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.codex_cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.settings.codex_stream_limit_bytes,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self.settings.codex_timeout_seconds,
            )
            if process.returncode:
                error = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(error or f"codex exec failed with code {process.returncode}")
            response = output_path.read_text(encoding="utf-8").strip()
            return response or stdout.decode("utf-8", errors="replace").strip()
        finally:
            output_path.unlink(missing_ok=True)

    async def ask_with_image(self, prompt: str, image_path: Path, conversation_id: str) -> str:
        if not self.settings.codex_binary.exists():
            raise FileNotFoundError(f"Codex binary not found: {self.settings.codex_binary}")

        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as output_file:
            output_path = Path(output_file.name)

        full_prompt = (
            "You are Codex responding through a Feishu chat bridge. "
            "The user sent an image. Analyze it directly and answer concisely.\n\n"
            f"Feishu conversation: {conversation_id}\n"
            f"User: {prompt}"
        )
        image_model = self.settings.image_codex_model or self.settings.codex_model
        image_reasoning_effort = self.settings.image_codex_reasoning_effort or self.settings.codex_reasoning_effort

        command = [
            str(self.settings.codex_binary),
            "exec",
            "--ephemeral",
            "--sandbox",
            self.settings.codex_sandbox,
        ]
        command.extend(_codex_exec_model_args(image_model, image_reasoning_effort))
        command.extend(
            [
                "--image",
                str(image_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.codex_cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.settings.codex_stream_limit_bytes,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(full_prompt.encode("utf-8")),
                timeout=self.settings.codex_timeout_seconds,
            )
            if process.returncode:
                error = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(error or f"codex exec image failed with code {process.returncode}")
            response = output_path.read_text(encoding="utf-8").strip()
            return response or stdout.decode("utf-8", errors="replace").strip()
        finally:
            output_path.unlink(missing_ok=True)


class CodexAppServerClient(CodexClient):
    def __init__(self, settings: Settings, approval_handler: ApprovalHandler | None = None) -> None:
        self.settings = settings
        self.approval_handler = approval_handler
        self.thread_store = ThreadStore(settings.codex_thread_map_path)
        self._request_id = 0
        self._process: asyncio.subprocess.Process | None = None
        self._process_lock = asyncio.Lock()
        self._initialized = False
        self._active_threads: dict[str, str] = {}
        self._loaded_history: set[str] = set()
        self._seeded_history: set[str] = set()

    async def prewarm(self) -> None:
        async with self._process_lock:
            await self._get_process()

    async def ask(self, message: str | list[dict[str, Any]], conversation_id: str) -> str:
        return await self.ask_stream(message, conversation_id)

    async def ask_stream(
        self,
        message: str | list[dict[str, Any]],
        conversation_id: str,
        event_handler: CodexStreamHandler | None = None,
    ) -> str:
        async with self._process_lock:
            process = await self._get_process()
            try:
                return await self._ask_with_process(process, message, conversation_id, event_handler)
            except Exception as error:
                if process.returncode is not None or _caused_by_timeout(error):
                    await self._reset_process(process)
                    try:
                        await self._get_process()
                    except Exception:
                        pass
                raise

    async def load_conversation(self, conversation_id: str) -> list[CodexStreamEvent]:
        async with self._process_lock:
            if conversation_id in self._loaded_history:
                return []
            process = await self._get_process()
            thread_id = await self._load_or_create_thread(process, conversation_id)
            response = await self._request(
                process,
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            self._loaded_history.add(conversation_id)
            return _thread_read_response_to_stream_events(response)

    async def seed_history(self, conversation_id: str, history_text: str) -> bool:
        visible = history_text.strip()
        if not visible:
            return False
        async with self._process_lock:
            if conversation_id in self._seeded_history:
                return False
            process = await self._get_process()
            thread_id = await self._load_or_create_thread(process, conversation_id)
            marker = _feishu_history_marker(conversation_id)
            response = await self._request(
                process,
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            if _thread_contains_text(response, marker):
                self._seeded_history.add(conversation_id)
                return False
            await self._request(
                process,
                "thread/inject_items",
                {
                    "threadId": thread_id,
                    "items": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"{marker}\n\n{visible}",
                                }
                            ],
                        }
                    ],
                },
            )
            self._seeded_history.add(conversation_id)
            return True

    async def ask_with_image(self, prompt: str, image_path: Path, conversation_id: str) -> str:
        if self.settings.image_use_exec_fallback:
            return await CodexExecClient(self.settings).ask_with_image(prompt, image_path, conversation_id)
        return await self.ask_stream(
            [
                {"type": "text", "text": prompt},
                {"type": "localImage", "path": str(image_path), "detail": "high"},
            ],
            conversation_id,
        )

    async def _start_process(self) -> asyncio.subprocess.Process:
        command = [str(self.settings.codex_binary), "app-server"]
        if self.settings.codex_app_socket:
            command.extend(["proxy", "--sock", str(self.settings.codex_app_socket)])
        else:
            command.append("--stdio")
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.settings.codex_stream_limit_bytes,
        )

    async def _get_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.returncode is not None:
            self._process = await self._start_process()
            self._initialized = False
        if not self._initialized:
            await self._initialize(self._process)
            self._initialized = True
        return self._process

    async def _reset_process(self, process: asyncio.subprocess.Process | None = None) -> None:
        await self._stop_process(process or self._process)
        self._process = None
        self._initialized = False
        self._active_threads.clear()

    async def _stop_process(self, process: asyncio.subprocess.Process | None) -> None:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _ask_with_process(
        self,
        process: asyncio.subprocess.Process,
        message: str | list[dict[str, Any]],
        conversation_id: str,
        event_handler: CodexStreamHandler | None = None,
    ) -> str:
        answer = ""
        active_turn_id: str | None = None
        turn_events = _CodexTurnEventMapper(event_handler)
        thread_id = await self._load_or_create_thread(process, conversation_id)
        turn_params = {
            "threadId": thread_id,
            "input": message if isinstance(message, list) else [{"type": "text", "text": message}],
            "approvalPolicy": self.settings.codex_approval_policy,
            "cwd": str(self.settings.codex_cwd),
        }
        _set_app_server_turn_model_params(
            turn_params,
            self.settings.codex_model,
            self.settings.codex_reasoning_effort,
        )
        turn_response = await self._request(process, "turn/start", turn_params)
        active_turn_id = ((turn_response.get("turn") or {}).get("id"))

        deadline = asyncio.get_running_loop().time() + self.settings.codex_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                incoming = await asyncio.wait_for(
                    self._read_message(process),
                    timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
                )
            except TimeoutError as error:
                raise TimeoutError(
                    "Timed out waiting "
                    f"{int(self.settings.codex_timeout_seconds)} seconds "
                    "for Codex app-server final answer"
                ) from error
            if "method" not in incoming:
                continue
            method = incoming["method"]
            params = incoming.get("params") or {}
            if "id" in incoming:
                await self._respond_to_server_request(process, incoming, conversation_id)
                continue
            if method == "turn/started" and _matches_thread(params, thread_id):
                active_turn_id = _read_turn_id(params) or active_turn_id
            await turn_events.handle(method, params, thread_id, active_turn_id)
            if method == "item/agentMessage/delta" and _matches_turn(params, active_turn_id):
                answer += str(params.get("delta") or "")
            elif method == "item/completed" and _matches_turn(params, active_turn_id):
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    answer = str(item.get("text") or answer).strip()
            elif method == "error" and _matches_turn(params, active_turn_id):
                raise RuntimeError(f"Codex app-server error: {params.get('error')}")
            elif method == "thread/status/changed" and params.get("threadId") == thread_id:
                if (params.get("status") or {}).get("type") == "idle" and answer.strip():
                    return answer.strip()
            elif method == "turn/completed" and params.get("threadId") == thread_id:
                if answer.strip():
                    return answer.strip()
        raise TimeoutError("Timed out waiting for Codex app-server final answer")

    async def _initialize(self, process: asyncio.subprocess.Process) -> None:
        await self._request(
            process,
            "initialize",
            {
                "clientInfo": {
                    "name": "feishu-codex-bridge",
                    "version": "0.1.0",
                    "title": "Feishu Codex Bridge",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._notify(process, "initialized")

    async def _load_or_create_thread(
        self,
        process: asyncio.subprocess.Process,
        conversation_id: str,
    ) -> str:
        fixed_thread_id = self.settings.codex_fixed_thread_id.strip()
        if fixed_thread_id:
            active_thread_id = self._active_threads.get(conversation_id)
            if active_thread_id == fixed_thread_id:
                return active_thread_id
            thread_id = await self._resume_thread(process, fixed_thread_id)
            self._active_threads[conversation_id] = thread_id
            return thread_id

        active_thread_id = self._active_threads.get(conversation_id)
        if active_thread_id:
            return active_thread_id

        existing = self.thread_store.get(conversation_id)
        if existing:
            thread_id = await self._resume_thread(process, existing)
            self._active_threads[conversation_id] = thread_id
            return thread_id

        response = await self._request(process, "thread/start", self._thread_params({"ephemeral": False}))
        thread_id = str(response["thread"]["id"])
        self.thread_store.set(conversation_id, thread_id)
        self._active_threads[conversation_id] = thread_id
        await self._request(
            process,
            "thread/name/set",
            {"threadId": thread_id, "name": f"{self.settings.codex_thread_name_prefix}: {conversation_id}"},
        )
        return thread_id

    async def _resume_thread(self, process: asyncio.subprocess.Process, thread_id: str) -> str:
        loaded = await self._loaded_thread_ids(process)
        if thread_id in loaded:
            return thread_id
        response = await self._request(
            process,
            "thread/resume",
            self._thread_params({"threadId": thread_id}),
        )
        return str(response["thread"]["id"])

    async def _loaded_thread_ids(self, process: asyncio.subprocess.Process) -> set[str]:
        try:
            response = await self._request(process, "thread/loaded/list", {})
        except Exception:
            return set()
        data = response.get("data")
        if not isinstance(data, list):
            return set()
        return {str(item) for item in data if isinstance(item, str)}

    def _thread_params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self.settings.codex_cwd),
            "sandbox": self.settings.codex_sandbox,
            "approvalPolicy": self.settings.codex_approval_policy,
            "approvalsReviewer": "user",
            "threadSource": "feishu-codex-bridge",
            "developerInstructions": (
                "You are responding to a user from Feishu. Keep final answers concise. "
                "If you need local approvals, explain what approval is needed; do not assume "
                "Feishu approval is enough for dangerous local actions."
            ),
        }
        model = self.settings.codex_model.strip()
        reasoning_effort = self.settings.codex_reasoning_effort.strip()
        if model:
            params["model"] = model
        if reasoning_effort:
            params["config"] = {"model_reasoning_effort": reasoning_effort}
        params.update(extra)
        return params

    async def _request(
        self,
        process: asyncio.subprocess.Process,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        await self._write_message(
            process,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
        )
        while True:
            message = await asyncio.wait_for(self._read_message(process), timeout=self.settings.codex_timeout_seconds)
            if "id" in message and message.get("method"):
                await self._respond_to_server_request(process, message)
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex app-server {method} failed: {message['error']}")
            return message.get("result") or {}

    async def _notify(
        self,
        process: asyncio.subprocess.Process,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_message(process, payload)

    async def _write_message(self, process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise RuntimeError("Codex app-server stdin is closed")
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _read_message(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        if process.stdout is None:
            raise RuntimeError("Codex app-server stdout is closed")
        line = await process.stdout.readline()
        if not line:
            stderr = b""
            if process.stderr:
                stderr = await process.stderr.read()
            raise RuntimeError(f"Codex app-server exited unexpectedly: {stderr.decode(errors='replace')}")
        return json.loads(line.decode("utf-8"))

    async def _respond_to_server_request(
        self,
        process: asyncio.subprocess.Process,
        request: dict[str, Any],
        conversation_id: str = "",
    ) -> None:
        method = request.get("method")
        request_id = request.get("id")
        if method in {"item/commandExecution/requestApproval", "execCommandApproval"}:
            approved = await self._request_feishu_approval(conversation_id, method, request.get("params") or {})
            result = {"decision": _approval_decision(method, approved)}
        elif method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
            approved = await self._request_feishu_approval(conversation_id, method, request.get("params") or {})
            result = {"decision": _approval_decision(method, approved)}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {"id": ":default"}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            approved = await self._request_feishu_approval(conversation_id, method, request.get("params") or {})
            result = {"action": "accept" if approved else "decline", "content": None}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "item/tool/call":
            result = {
                "success": False,
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": "Feishu bridge does not execute dynamic client tools yet.",
                    }
                ],
            }
        else:
            result = {"error": {"code": -32601, "message": f"Unsupported server request: {method}"}}
        if "error" in result:
            await self._write_message(process, {"jsonrpc": "2.0", "id": request_id, **result})
        else:
            await self._write_message(process, {"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _request_feishu_approval(self, conversation_id: str, method: str, params: dict[str, Any]) -> bool:
        if _auto_approve_local_requests(self.settings):
            return True
        if self.approval_handler is None or not conversation_id:
            return False
        request = approval_request_from_server(conversation_id=conversation_id, method=method, params=params)
        return await self.approval_handler(request)


def _codex_exec_model_args(model: str, reasoning_effort: str) -> list[str]:
    args: list[str] = []
    normalized_model = model.strip()
    normalized_effort = reasoning_effort.strip()
    if normalized_model:
        args.extend(["--model", normalized_model])
    if normalized_effort:
        args.extend(["--config", f'model_reasoning_effort="{normalized_effort}"'])
    return args


def _set_app_server_turn_model_params(params: dict[str, Any], model: str, reasoning_effort: str) -> None:
    normalized_model = model.strip()
    normalized_effort = reasoning_effort.strip()
    if normalized_model:
        params["model"] = normalized_model
    if normalized_effort:
        params["effort"] = normalized_effort


def _input_items_to_text(items: list[dict[str, Any]]) -> str:
    text_parts: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type in {"text", "input_text"}:
            text_parts.append(str(item.get("text") or ""))
        elif item_type == "localImage":
            text_parts.append(f"[local image: {item.get('path')}]")
        elif item_type == "image":
            text_parts.append("[image]")
        else:
            text_parts.append(f"[{item_type or 'unknown item'}]")
    return "\n".join(part for part in text_parts if part)


def _approval_decision(method: str, approved: bool) -> str:
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return "approved" if approved else "denied"
    return "accept" if approved else "decline"


def _auto_approve_local_requests(settings: Settings) -> bool:
    return (
        settings.codex_approval_policy.strip().lower() == "never"
        or settings.codex_sandbox.strip().lower() == "danger-full-access"
    )


def _caused_by_timeout(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, TimeoutError):
            return True
        current = current.__cause__ or current.__context__
    return False


class _CodexTurnEventMapper:
    def __init__(self, event_handler: CodexStreamHandler | None) -> None:
        self.event_handler = event_handler
        self.pending_agent_messages: dict[str, str] = {}
        self.pending_reasoning: dict[str, str] = {}

    async def handle(
        self,
        method: str,
        params: dict[str, Any],
        thread_id: str,
        turn_id: str | None,
    ) -> None:
        if not _matches_thread(params, thread_id) or not _matches_turn(params, turn_id):
            return
        if method == "turn/started":
            return
        if method == "item/agentMessage/delta":
            item_id = str(params.get("itemId") or "")
            delta = str(params.get("delta") or "")
            if item_id:
                self.pending_agent_messages[item_id] = self.pending_agent_messages.get(item_id, "") + delta
            await self.emit(CodexStreamEvent("assistant_delta", delta, item_id or None))
            return
        if method == "item/reasoning/summaryTextDelta":
            item_id = str(params.get("itemId") or "")
            delta = str(params.get("delta") or "")
            if item_id:
                self.pending_reasoning[item_id] = self.pending_reasoning.get(item_id, "") + delta
            await self.emit(CodexStreamEvent("reasoning", delta, item_id or None))
            return
        if method == "turn/plan/updated":
            plan_text = _format_plan(params.get("plan"))
            if plan_text:
                await self.emit(CodexStreamEvent("plan", plan_text))
            return
        if method == "codex/event/exec_command_begin":
            await self.emit(_codex_exec_event(params, running=True))
            return
        if method == "codex/event/exec_command_end":
            await self.emit(_codex_exec_event(params, running=False))
            return
        if method == "codex/event/patch_apply_begin":
            await self.emit(CodexStreamEvent("tool", "Applying file changes."))
            return
        if method == "codex/event/patch_apply_end":
            msg = params.get("msg") if isinstance(params.get("msg"), dict) else {}
            success = msg.get("success") if isinstance(msg, dict) else None
            text = "File changes applied." if success is not False else "File changes failed."
            await self.emit(CodexStreamEvent("tool", text))
            return
        if method == "item/started":
            event = _thread_item_event(params.get("item"), running=True)
            if event:
                await self.emit(event)
            return
        if method == "item/completed":
            item = params.get("item")
            event = self._completed_item_event(item)
            if event:
                await self.emit(event)

    async def emit(self, event: CodexStreamEvent | None) -> None:
        if event is None or not self.event_handler or not event.text.strip():
            return
        result = self.event_handler(event)
        if inspect.isawaitable(result):
            await result

    def _completed_item_event(self, item: Any) -> CodexStreamEvent | None:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agentMessage":
            if item.get("phase") == "final_answer":
                return None
            text = str(item.get("text") or "")
            if not text or item_id in self.pending_agent_messages:
                return None
            return CodexStreamEvent("assistant", text, item_id or None)
        if item_type == "reasoning":
            text = _reasoning_text(item)
            if not text or item_id in self.pending_reasoning:
                return None
            return CodexStreamEvent("reasoning", text, item_id or None)
        return _thread_item_event(item, running=False)


def _matches_thread(params: dict[str, Any], thread_id: str) -> bool:
    candidate = params.get("threadId")
    if candidate is None:
        return True
    return str(candidate) == thread_id


def _matches_turn(params: dict[str, Any], turn_id: str | None) -> bool:
    if not turn_id:
        return True
    candidate = _read_turn_id(params)
    if candidate is None:
        return True
    return candidate == turn_id


def _read_turn_id(params: dict[str, Any]) -> str | None:
    if isinstance(params.get("turnId"), str):
        return str(params["turnId"])
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return str(turn["id"])
    return None


def _format_plan(plan: Any) -> str:
    if not isinstance(plan, list):
        return ""
    lines: list[str] = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        step = str(entry.get("step") or "").strip()
        if step:
            lines.append(f"- {step}")
    return "\n".join(lines)


def _codex_exec_event(params: dict[str, Any], running: bool) -> CodexStreamEvent | None:
    msg = params.get("msg")
    if not isinstance(msg, dict):
        return None
    command = msg.get("command")
    command_text = _command_to_text(command)
    if not command_text:
        return None
    command_label, shortened = _summarize_command_for_feishu(command)
    details = "\nDetails: full command hidden in Feishu; open Codex for the exact command." if shortened else ""
    if running:
        return CodexStreamEvent("tool", f"Running command: {command_label}{details}")
    exit_code = msg.get("exit_code", msg.get("exitCode"))
    if exit_code in (None, 0):
        return CodexStreamEvent("tool", f"Command finished: {command_label}{details}")
    return CodexStreamEvent("tool", f"Command failed ({exit_code}): {command_label}{details}")


def _thread_item_event(item: Any, running: bool) -> CodexStreamEvent | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    item_id = str(item.get("id") or "") or None
    status = "started" if running else "finished"
    if item_type == "commandExecution":
        command = item.get("command")
        command_text = _command_to_text(command)
        if command_text:
            command_label, shortened = _summarize_command_for_feishu(command)
            details = "\nDetails: full command hidden in Feishu; open Codex for the exact command." if shortened else ""
            return CodexStreamEvent("tool", f"Command {status}: {command_label}{details}", item_id)
    if item_type == "fileChange":
        return CodexStreamEvent("tool", f"File change {status}.", item_id)
    if item_type == "mcpToolCall":
        tool = str(item.get("tool") or "MCP tool").strip()
        return CodexStreamEvent("tool", f"{tool} {status}.", item_id)
    if item_type == "webSearch":
        query = str(item.get("query") or "web search").strip()
        return CodexStreamEvent("tool", f"Web search {status}: {query}", item_id)
    if item_type == "plan" and not running:
        text = str(item.get("text") or "").strip()
        if text:
            return CodexStreamEvent("plan", text, item_id)
    return None


def _command_to_text(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(part) for part in command if str(part).strip())
    return str(command or "").strip()


def _summarize_command_for_feishu(command: Any) -> tuple[str, bool]:
    raw = _command_to_text(command)
    if not raw:
        return "", False
    script = _shell_script_from_command(command)
    if script:
        label = _summarize_shell_script(script)
        return label, _collapse_space(label) != _collapse_space(raw)
    single_line = _collapse_space(raw)
    max_len = 140
    if len(single_line) <= max_len and "\n" not in raw:
        return single_line, False
    return f"{single_line[: max_len - 3].rstrip()}...", True


def _shell_script_from_command(command: Any) -> str:
    if isinstance(command, list):
        parts = [str(part) for part in command]
    else:
        try:
            parts = shlex.split(str(command or ""))
        except ValueError:
            return ""
    if len(parts) < 3:
        return ""
    shell_name = Path(parts[0]).name
    if shell_name not in {"bash", "sh", "zsh"}:
        return ""
    for flag in ("-lc", "-c"):
        if flag in parts:
            index = parts.index(flag)
            if index + 1 < len(parts):
                return parts[index + 1].strip()
    return ""


def _summarize_shell_script(script: str) -> str:
    first_line = next((line.strip() for line in script.splitlines() if line.strip()), "")
    if not first_line:
        return "shell script"
    if "<<" in first_line:
        program = first_line.split()[0] if first_line.split() else "shell"
        return f"{program} inline script"
    if len(script.splitlines()) > 1:
        return f"{_collapse_space(first_line)[:100].rstrip()}..."
    return _collapse_space(first_line)[:140].rstrip()


def _collapse_space(text: str) -> str:
    return " ".join(text.split())


def _thread_read_response_to_stream_events(response: dict[str, Any]) -> list[CodexStreamEvent]:
    thread = response.get("thread")
    turns = thread.get("turns") if isinstance(thread, dict) else []
    if not isinstance(turns, list):
        return []
    events: list[CodexStreamEvent] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            event = _history_item_event(item)
            if event:
                events.append(event)
    return events


def _feishu_history_marker(conversation_id: str) -> str:
    return f"Feishu history sync for conversation {conversation_id}"


def _thread_contains_text(response: dict[str, Any], needle: str) -> bool:
    if not needle:
        return False
    thread = response.get("thread")
    turns = thread.get("turns") if isinstance(thread, dict) else []
    if not isinstance(turns, list):
        return False
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and needle in _thread_item_text(item):
                return True
    return False


def _history_item_event(item: Any) -> CodexStreamEvent | None:
    if not isinstance(item, dict):
        return None
    item_id = str(item.get("id") or "") or None
    item_type = item.get("type")
    if item_type == "userMessage":
        text = _user_message_text(item)
        return CodexStreamEvent("history", f"User: {text}", item_id) if text else None
    if item_type == "agentMessage":
        text = str(item.get("text") or "").strip()
        return CodexStreamEvent("history", f"Codex: {text}", item_id) if text else None
    if item_type == "reasoning":
        text = _reasoning_text(item)
        return CodexStreamEvent("history", f"Thinking: {text}", item_id) if text else None
    if item_type == "plan":
        text = str(item.get("text") or "").strip()
        return CodexStreamEvent("history", f"Plan:\n{text}", item_id) if text else None
    return None


def _thread_item_text(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "userMessage":
        return _user_message_text(item)
    if item_type == "agentMessage":
        return str(item.get("text") or "").strip()
    if item_type == "reasoning":
        return _reasoning_text(item)
    if item_type == "plan":
        return str(item.get("text") or "").strip()
    return ""


def _user_message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _reasoning_text(item: dict[str, Any]) -> str:
    summary = item.get("summary")
    if isinstance(summary, list):
        return "\n".join(str(part) for part in summary if str(part).strip()).strip()
    content = item.get("content")
    if isinstance(content, list):
        return "\n".join(str(part) for part in content if str(part).strip()).strip()
    return str(item.get("text") or "").strip()


def create_codex_client(settings: Settings, approval_handler: ApprovalHandler | None = None) -> CodexClient:
    if settings.codex_use_app_server:
        return CodexAppServerClient(settings, approval_handler=approval_handler)
    return CodexExecClient(settings)


def jsonrpc_request(method: str, params: dict, request_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
