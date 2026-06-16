from feishu_codex_bridge.codex_client import (
    CodexAppServerClient,
    CodexStreamEvent,
    _auto_approve_local_requests,
    _approval_decision,
    _caused_by_timeout,
    _codex_exec_model_args,
    _codex_exec_event,
    _set_app_server_turn_model_params,
)
from feishu_codex_bridge.config import Settings


class FakeAppServerProcess:
    returncode = None


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_codex_exec_model_args_include_model_and_reasoning_effort() -> None:
    assert _codex_exec_model_args("gpt-5.5", "medium") == [
        "--model",
        "gpt-5.5",
        "--config",
        'model_reasoning_effort="medium"',
    ]


def test_app_server_model_params_include_medium_effort(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_model="gpt-5.5",
        codex_reasoning_effort="medium",
        codex_thread_map_path=tmp_path / "threads.json",
    )
    client = CodexAppServerClient(settings)

    thread_params = client._thread_params({})
    turn_params: dict[str, str] = {}
    _set_app_server_turn_model_params(turn_params, settings.codex_model, settings.codex_reasoning_effort)

    assert thread_params["model"] == "gpt-5.5"
    assert thread_params["config"] == {"model_reasoning_effort": "medium"}
    assert turn_params == {"model": "gpt-5.5", "effort": "medium"}


def test_approval_decision_matches_protocol_variants() -> None:
    assert _approval_decision("item/commandExecution/requestApproval", True) == "accept"
    assert _approval_decision("item/commandExecution/requestApproval", False) == "decline"
    assert _approval_decision("execCommandApproval", True) == "approved"
    assert _approval_decision("execCommandApproval", False) == "denied"


def test_auto_approve_local_requests_when_approval_is_never() -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_approval_policy="never",
        codex_sandbox="read-only",
    )

    assert _auto_approve_local_requests(settings)


def test_auto_approve_local_requests_when_sandbox_is_danger_full_access() -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_approval_policy="on-request",
        codex_sandbox="danger-full-access",
    )

    assert _auto_approve_local_requests(settings)


def test_active_thread_cache_skips_repeated_resume(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_thread_map_path=tmp_path / "threads.json",
    )
    client = CodexAppServerClient(settings)
    client.thread_store.set("chat", "thread_1")
    client._active_threads["chat"] = "thread_1"

    async def fail_request(*_args, **_kwargs):
        raise AssertionError("thread/resume should not be called for an active thread")

    client._request = fail_request  # type: ignore[method-assign]

    assert run(client._load_or_create_thread(FakeAppServerProcess(), "chat")) == "thread_1"  # type: ignore[arg-type]


def test_fixed_thread_id_takes_priority_over_thread_map(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_fixed_thread_id="fixed_thread",
        codex_thread_map_path=tmp_path / "threads.json",
    )
    client = CodexAppServerClient(settings)
    client.thread_store.set("chat", "mapped_thread")
    calls: list[tuple[str, dict]] = []

    async def fake_loaded_thread_ids(_process):
        return set()

    async def fake_request(_process, method: str, params=None):
        calls.append((method, params or {}))
        assert method == "thread/resume"
        assert (params or {})["threadId"] == "fixed_thread"
        return {"thread": {"id": "fixed_thread"}}

    client._loaded_thread_ids = fake_loaded_thread_ids  # type: ignore[method-assign]
    client._request = fake_request  # type: ignore[method-assign]

    assert run(client._load_or_create_thread(FakeAppServerProcess(), "chat")) == "fixed_thread"  # type: ignore[arg-type]
    assert client.thread_store.get("chat") == "mapped_thread"
    assert calls == [("thread/resume", {"threadId": "fixed_thread", **client._thread_params({})})]


def test_caused_by_timeout_checks_exception_chain() -> None:
    error = RuntimeError("wrapper")
    error.__cause__ = TimeoutError("too slow")

    assert _caused_by_timeout(error)
    assert not _caused_by_timeout(RuntimeError("other"))


def test_load_conversation_reads_existing_thread_history(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_thread_map_path=tmp_path / "threads.json",
    )
    client = CodexAppServerClient(settings)

    async def fake_get_process():
        return FakeAppServerProcess()

    async def fake_load_or_create_thread(_process, conversation_id: str):
        assert conversation_id == "chat"
        return "thread_1"

    async def fake_request(_process, method: str, params=None):
        assert method == "thread/read"
        assert params == {"threadId": "thread_1", "includeTurns": True}
        return {
            "thread": {
                "turns": [
                    {
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "user-1",
                                "content": [{"type": "text", "text": "hello"}],
                            },
                            {
                                "type": "agentMessage",
                                "id": "agent-1",
                                "text": "hi",
                            },
                            {
                                "type": "reasoning",
                                "id": "reason-1",
                                "summary": ["thinking"],
                            },
                        ],
                    }
                ]
            }
        }

    client._get_process = fake_get_process  # type: ignore[method-assign]
    client._load_or_create_thread = fake_load_or_create_thread  # type: ignore[method-assign]
    client._request = fake_request  # type: ignore[method-assign]

    assert run(client.load_conversation("chat")) == [
        CodexStreamEvent(kind="history", text="User: hello", item_id="user-1"),
        CodexStreamEvent(kind="history", text="Codex: hi", item_id="agent-1"),
        CodexStreamEvent(kind="history", text="Thinking: thinking", item_id="reason-1"),
    ]
    assert run(client.load_conversation("chat")) == []


def test_seed_history_injects_feishu_history_once(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_thread_map_path=tmp_path / "threads.json",
    )
    client = CodexAppServerClient(settings)
    calls: list[tuple[str, dict]] = []

    async def fake_get_process():
        return FakeAppServerProcess()

    async def fake_load_or_create_thread(_process, conversation_id: str):
        assert conversation_id == "chat"
        return "thread_1"

    async def fake_request(_process, method: str, params=None):
        calls.append((method, params or {}))
        if method == "thread/read":
            return {"thread": {"turns": []}}
        if method == "thread/inject_items":
            return {}
        raise AssertionError(method)

    client._get_process = fake_get_process  # type: ignore[method-assign]
    client._load_or_create_thread = fake_load_or_create_thread  # type: ignore[method-assign]
    client._request = fake_request  # type: ignore[method-assign]

    assert run(client.seed_history("chat", "User: hello"))
    assert not run(client.seed_history("chat", "User: hello again"))
    assert calls[0] == ("thread/read", {"threadId": "thread_1", "includeTurns": True})
    assert calls[1][0] == "thread/inject_items"
    injected = calls[1][1]["items"][0]
    assert injected["type"] == "message"
    assert injected["role"] == "user"
    assert "Feishu history sync for conversation chat" in injected["content"][0]["text"]
    assert "User: hello" in injected["content"][0]["text"]
    assert len(calls) == 2


def test_seed_history_skips_existing_marker_in_thread(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_thread_map_path=tmp_path / "threads.json",
    )
    client = CodexAppServerClient(settings)
    calls: list[str] = []

    async def fake_get_process():
        return FakeAppServerProcess()

    async def fake_load_or_create_thread(_process, conversation_id: str):
        assert conversation_id == "chat"
        return "thread_1"

    async def fake_request(_process, method: str, params=None):
        calls.append(method)
        if method == "thread/read":
            return {
                "thread": {
                    "turns": [
                        {
                            "items": [
                                {
                                    "type": "userMessage",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "Feishu history sync for conversation chat",
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            }
        raise AssertionError(method)

    client._get_process = fake_get_process  # type: ignore[method-assign]
    client._load_or_create_thread = fake_load_or_create_thread  # type: ignore[method-assign]
    client._request = fake_request  # type: ignore[method-assign]

    assert not run(client.seed_history("chat", "User: hello"))
    assert calls == ["thread/read"]


def test_existing_thread_resume_failure_is_not_replaced(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_thread_map_path=tmp_path / "threads.json",
    )
    client = CodexAppServerClient(settings)
    client.thread_store.set("chat", "old_thread")

    async def fake_loaded_thread_ids(_process):
        return set()

    async def fake_request(_process, method: str, params=None):
        assert method == "thread/resume"
        raise RuntimeError("resume failed")

    client._loaded_thread_ids = fake_loaded_thread_ids  # type: ignore[method-assign]
    client._request = fake_request  # type: ignore[method-assign]

    import pytest

    with pytest.raises(RuntimeError, match="resume failed"):
        run(client._load_or_create_thread(FakeAppServerProcess(), "chat"))  # type: ignore[arg-type]

    assert client.thread_store.get("chat") == "old_thread"


def test_ask_stream_emits_codex_progress_events(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        use_hermes_feishu_env=False,
        codex_thread_map_path=tmp_path / "threads.json",
        codex_timeout_seconds=1,
    )
    client = CodexAppServerClient(settings)
    messages = iter(
        [
            {"method": "turn/started", "params": {"threadId": "thread_1", "turn": {"id": "turn_1"}}},
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {"threadId": "thread_1", "itemId": "reason_1", "delta": "Thinking"},
            },
            {
                "method": "codex/event/exec_command_begin",
                "params": {"msg": {"call_id": "exec_1", "command": "npm test"}},
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread_1",
                    "item": {
                        "id": "answer_1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "done",
                    },
                },
            },
            {"method": "turn/completed", "params": {"threadId": "thread_1", "turn": {"status": "completed"}}},
        ]
    )
    events: list[CodexStreamEvent] = []

    async def fake_get_process():
        return FakeAppServerProcess()

    async def fake_load_or_create_thread(_process, conversation_id: str):
        assert conversation_id == "chat"
        return "thread_1"

    async def fake_request(_process, method: str, params=None):
        assert method == "turn/start"
        return {"turn": {"id": "turn_1"}}

    async def fake_read_message(_process):
        return next(messages)

    client._get_process = fake_get_process  # type: ignore[method-assign]
    client._load_or_create_thread = fake_load_or_create_thread  # type: ignore[method-assign]
    client._request = fake_request  # type: ignore[method-assign]
    client._read_message = fake_read_message  # type: ignore[method-assign]

    answer = run(client.ask_stream("hello", "chat", events.append))

    assert answer == "done"
    assert events == [
        CodexStreamEvent(kind="status", text="Codex started working.", item_id=None),
        CodexStreamEvent(kind="reasoning", text="Thinking", item_id="reason_1"),
        CodexStreamEvent(kind="tool", text="Running command: npm test", item_id=None),
    ]


def test_long_inline_command_is_summarized_for_feishu() -> None:
    event = _codex_exec_event(
        {
            "msg": {
                "command": [
                    "/bin/zsh",
                    "-lc",
                    "python3 - <<'PY'\nprint('hello')\nPY",
                ],
                "exit_code": 0,
            }
        },
        running=False,
    )

    assert event == CodexStreamEvent(
        kind="tool",
        text=(
            "Command finished: python3 inline script\n"
            "Details: full command hidden in Feishu; open Codex for the exact command."
        ),
    )
