from __future__ import annotations

import asyncio
import json
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .approval import ApprovalHandler, approval_request_from_server
from .config import Settings
from .thread_store import ThreadStore


class CodexClient(ABC):
    @abstractmethod
    async def ask(self, message: str | list[dict[str, Any]], conversation_id: str) -> str:
        raise NotImplementedError

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

    async def ask(self, message: str | list[dict[str, Any]], conversation_id: str) -> str:
        process = await self._start_process()
        answer = ""
        active_turn_id: str | None = None
        try:
            await self._initialize(process)
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
                incoming = await asyncio.wait_for(
                    self._read_message(process),
                    timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
                )
                if "method" not in incoming:
                    continue
                method = incoming["method"]
                params = incoming.get("params") or {}
                if "id" in incoming:
                    await self._respond_to_server_request(process, incoming, conversation_id)
                    continue
                if method == "item/agentMessage/delta" and params.get("turnId") == active_turn_id:
                    answer += str(params.get("delta") or "")
                elif method == "item/completed" and params.get("turnId") == active_turn_id:
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                        answer = str(item.get("text") or answer).strip()
                elif method == "error" and params.get("turnId") == active_turn_id:
                    raise RuntimeError(f"Codex app-server error: {params.get('error')}")
                elif method == "thread/status/changed" and params.get("threadId") == thread_id:
                    if (params.get("status") or {}).get("type") == "idle" and answer.strip():
                        return answer.strip()
                elif method == "turn/completed" and params.get("threadId") == thread_id:
                    if answer.strip():
                        return answer.strip()
            raise TimeoutError("Timed out waiting for Codex app-server final answer")
        finally:
            await self._stop_process(process)

    async def ask_with_image(self, prompt: str, image_path: Path, conversation_id: str) -> str:
        if self.settings.image_use_exec_fallback:
            return await CodexExecClient(self.settings).ask_with_image(prompt, image_path, conversation_id)
        return await super().ask_with_image(prompt, image_path, conversation_id)

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

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

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
        existing = self.thread_store.get(conversation_id)
        if existing:
            try:
                response = await self._request(
                    process,
                    "thread/resume",
                    self._thread_params({"threadId": existing}),
                )
                return str(response["thread"]["id"])
            except Exception:
                self.thread_store.delete(conversation_id)

        response = await self._request(process, "thread/start", self._thread_params({"ephemeral": False}))
        thread_id = str(response["thread"]["id"])
        self.thread_store.set(conversation_id, thread_id)
        await self._request(
            process,
            "thread/name/set",
            {"threadId": thread_id, "name": f"{self.settings.codex_thread_name_prefix}: {conversation_id}"},
        )
        return thread_id

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
        if item_type == "text":
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


def create_codex_client(settings: Settings, approval_handler: ApprovalHandler | None = None) -> CodexClient:
    if settings.codex_use_app_server:
        return CodexAppServerClient(settings, approval_handler=approval_handler)
    return CodexExecClient(settings)


def jsonrpc_request(method: str, params: dict, request_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
