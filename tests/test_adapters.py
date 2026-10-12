import json
import sys
from pathlib import Path

import pytest

from orchestrator.adapters import (
    AgentRequest,
    AntigravityBackend,
    BackendError,
    CodexBackend,
    antigravity_command,
    codex_command,
    login_environment,
)
from orchestrator.config import default_config


async def ignore(*args):
    pass


def request(tmp_path, role="worker", provider="antigravity"):
    config = default_config()
    config.roles[role].provider = provider
    config.roles[role].model = config.providers[provider].models[0]
    return AgentRequest(role, config.roles[role], config.providers[provider], tmp_path, "Fix a bug")


def fake_cli(tmp_path: Path, events: list[dict], code: int = 0, stderr: str = "") -> str:
    path = tmp_path / "fake-cli"
    path.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "sys.stdin.read()\n"
        f"events = {events!r}\n"
        "for event in events: print(json.dumps(event), flush=True)\n"
        f"print({stderr!r}, file=sys.stderr)\n"
        f"sys.exit({code})\n"
    )
    path.chmod(0o755)
    return str(path)


def terminal(response="Fixed", status="SUCCESS", **extra):
    return {
        "event": "result",
        "result": {
            "conversation_id": "agy-1",
            "response": response,
            "status": status,
            "usage": {"input_tokens": 12, "output_tokens": 4},
            **extra,
        },
    }


def test_logins_do_not_inherit_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    monkeypatch.setenv("GEMINI_API_KEY", "private-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://custom-endpoint.invalid")
    monkeypatch.setenv("CODEX_HOME", "/tmp/existing-login")
    environment = login_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "GEMINI_API_KEY" not in environment
    assert "GOOGLE_GEMINI_BASE_URL" not in environment
    assert "GEMINI_CLI_NO_RELAUNCH" not in environment
    assert environment["CODEX_HOME"] == "/tmp/existing-login"


def test_codex_scopes_and_reasoning(tmp_path):
    worker = request(tmp_path, provider="codex")
    worker.provider.executable = sys.executable
    command = codex_command(worker, None)
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert 'model_reasoning_effort="medium"' in command
    assert 'forced_login_method="chatgpt"' in command
    reviewer = request(tmp_path, role="reviewer", provider="codex")
    reviewer.provider.executable = sys.executable
    command = codex_command(reviewer, None)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert not any("bypass" in argument for argument in command)


def test_antigravity_native_flags_and_role_modes(tmp_path):
    worker = request(tmp_path)
    worker.provider.executable = sys.executable
    command = antigravity_command(worker)
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--mode") + 1] == "accept-edits"
    assert command[command.index("--effort") + 1] == "medium"
    assert command[command.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert "--print" not in command and "--prompt" not in command
    assert "--dangerously-skip-permissions" not in command
    assert "--admin-policy" not in command
    worker.role_name = "reviewer"
    worker.provider.sandbox = True
    worker.role.reasoning = "default"
    worker.schema = {"type": "object"}
    command = antigravity_command(worker)
    assert command[command.index("--mode") + 1] == "plan"
    assert "--sandbox" in command
    assert "--effort" not in command
    assert json.loads(command[command.index("--json-schema") + 1]) == worker.schema


async def test_codex_actual_stream_shape(tmp_path):
    req = request(tmp_path, provider="codex")
    req.provider.executable = fake_cli(
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixed"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10}},
        ],
    )
    result = await CodexBackend().run(req, ignore)
    assert result.text == "Fixed"
    assert result.session_id == "thread-1"
    assert result.usage["input_tokens"] == 10


async def test_codex_fails_on_incomplete_turn(tmp_path):
    req = request(tmp_path, provider="codex")
    req.provider.executable = fake_cli(
        tmp_path,
        [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Partial"}},
            {"type": "turn.failed", "error": {"message": "Login expired"}},
        ],
    )
    with pytest.raises(BackendError, match="Login expired"):
        await CodexBackend().run(req, ignore)


async def test_antigravity_stream_uses_terminal_response_and_retains_progress(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(
        tmp_path,
        [
            {"event": "init", "conversation_id": "agy-1", "init": {"model": req.role.model}},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "state": "ACTIVE",
                    "text_delta": "Draft answer",
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "replace_file_content",
                },
            },
            terminal("Final answer"),
        ],
    )
    seen = []

    async def collect(kind, message, data):
        seen.append(message)

    result = await AntigravityBackend().run(req, collect)
    assert result.text == "Final answer"
    assert result.session_id == "agy-1"
    assert result.usage["input_tokens"] == 12
    assert "Draft answer" in seen and "replace_file_content" in seen


async def test_antigravity_structured_output(tmp_path):
    req = request(tmp_path, role="planner")
    req.schema = {"type": "object"}
    req.provider.executable = fake_cli(
        tmp_path,
        [
            terminal(
                "Text is not authoritative for structured requests",
                structured_output={"ok": True},
            )
        ],
    )
    result = await AntigravityBackend().run(req, ignore)
    assert json.loads(result.text) == {"ok": True}


@pytest.mark.parametrize("status", ["ERROR", "CANCELED", "INTERRUPTED", "WAITING", "RUNNING"])
async def test_non_success_result_fails_even_with_exit_zero_and_partial_text(tmp_path, status):
    req = request(tmp_path)
    req.provider.executable = fake_cli(tmp_path, [terminal("Partial response", status=status)])
    with pytest.raises(BackendError, match="Antigravity failed"):
        await AntigravityBackend().run(req, ignore)


async def test_missing_terminal_result_does_not_accept_progress(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(
        tmp_path,
        [
            {
                "event": "step_update",
                "step_update": {"text_delta": "Looks finished"},
            }
        ],
    )
    with pytest.raises(BackendError, match="No successful terminal result"):
        await AntigravityBackend().run(req, ignore)


async def test_antigravity_rejects_model_change(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(
        tmp_path,
        [
            {"event": "init", "init": {"model": "different-model"}},
        ],
    )
    with pytest.raises(BackendError, match="instead of"):
        await AntigravityBackend().run(req, ignore)


async def test_antigravity_expired_login_has_actionable_error(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(
        tmp_path,
        [
            terminal(
                "",
                status="ERROR",
                error="authentication required",
            )
        ],
        code=1,
    )
    with pytest.raises(BackendError, match="Run `agy`"):
        await AntigravityBackend().run(req, ignore)


async def test_antigravity_api_settings_are_not_used_for_login_adapter(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(tmp_path, [], code=1, stderr="GEMINI_API_KEY is not set")
    with pytest.raises(BackendError, match="Remove modelProvider"):
        await AntigravityBackend().run(req, ignore)


async def test_antigravity_stdin_protocol_and_login_environment(tmp_path):
    req = request(tmp_path)
    script = tmp_path / "inspect-cli"
    script.write_text(
        f"#!{sys.executable}\nimport os, json, sys\n"
        "messages = sys.stdin.read().splitlines()\n"
        "assert len(messages) == 1\n"
        "message = json.loads(messages[0])\n"
        "assert message == {'event': 'user', 'message': {'content': 'Fix a bug'}}\n"
        "assert 'GEMINI_API_KEY' not in os.environ\n"
        "assert '--input-format' in sys.argv\n"
        "assert '--dangerously-skip-permissions' not in sys.argv\n"
        f"print(json.dumps({terminal()!r}))\n"
    )
    script.chmod(0o755)
    req.provider.executable = str(script)
    assert (await AntigravityBackend().run(req, ignore)).text == "Fixed"
