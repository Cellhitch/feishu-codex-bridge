from feishu_codex_bridge.codex_client import (
    CodexAppServerClient,
    _approval_decision,
    _codex_exec_model_args,
    _set_app_server_turn_model_params,
)
from feishu_codex_bridge.config import Settings


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
