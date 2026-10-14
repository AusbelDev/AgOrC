import json
import sys
from pathlib import Path

import pytest

from orchestrator.adapters import (
    AgentRequest,
    AntigravityBackend,
    BackendError,
    ClaudeBackend,
    CodexBackend,
    antigravity_command,
    claude_command,
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
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_PROFILE",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.setenv(name, "do-not-use")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/existing-claude-login")
    environment = login_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "GEMINI_API_KEY" not in environment
    assert "GOOGLE_GEMINI_BASE_URL" not in environment
    assert "GEMINI_CLI_NO_RELAUNCH" not in environment
    assert environment["CODEX_HOME"] == "/tmp/existing-login"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert "ANTHROPIC_BASE_URL" not in environment
    assert "CLAUDE_CODE_USE_BEDROCK" not in environment
    assert "CLAUDE_CODE_USE_VERTEX" not in environment
    assert "CLAUDE_CODE_USE_FOUNDRY" not in environment
    assert environment["CLAUDE_CONFIG_DIR"] == "/tmp/existing-claude-login"


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


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
@pytest.mark.parametrize("use_mcp", [False, True])
def test_codex_mcp_inheritance_keeps_explicit_execution_settings(tmp_path, role, use_mcp):
    req = request(tmp_path, role=role, provider="codex")
    req.provider.executable = sys.executable
    req.role.use_mcp = use_mcp
    command = codex_command(req, tmp_path / "schema.json")
    assert ("--ignore-user-config" in command) is not use_mcp
    assert command[command.index("--model") + 1] == req.role.model
    assert command[command.index("--sandbox") + 1] == (
        "workspace-write" if role == "worker" else "read-only"
    )
    assert 'approval_policy="never"' in command
    assert 'forced_login_method="chatgpt"' in command
    assert 'model_provider="openai"' in command
    assert command[command.index("--output-schema") + 1] == str(tmp_path / "schema.json")


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


def fake_claude(tmp_path, events, *, method="claude.ai", code=0, mcp_tools=None):
    path = tmp_path / "fake-claude"
    login = {"authMethod": method, "loggedIn": method != "none"}
    path.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "if sys.argv[1:] == ['auth', 'status']:\n"
        f"    print(json.dumps({login!r}))\n"
        "    sys.exit(0)\n"
        + (
            "assert '--disallowedTools' not in sys.argv\n"
            "allowed = sys.argv[sys.argv.index('--allowedTools') + 1].split(',')\n"
            f"assert all(tool in allowed for tool in {mcp_tools!r})\n"
            if mcp_tools is not None
            else ""
        )
        + "assert sys.stdin.read() == 'Fix a bug'\n"
        f"for event in {events!r}: print(json.dumps(event), flush=True)\n"
        f"sys.exit({code})\n"
    )
    path.chmod(0o755)
    return str(path)


def claude_result(**extra):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Fixed",
        "session_id": "claude-1",
        "usage": {"input_tokens": 20, "output_tokens": 8},
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 20}},
        "total_cost_usd": 0.01,
        **extra,
    }


def test_claude_tools_permissions_schema_and_effort(tmp_path):
    req = request(tmp_path, provider="claude")
    req.provider.executable = sys.executable
    req.provider.sandbox = True
    req.schema = {"type": "object"}
    command = claude_command(req)
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--effort") + 1] == "medium"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,Bash"
    assert "Bash" not in command[command.index("--allowedTools") + 1]
    assert "Edit(./**)" in command[command.index("--allowedTools") + 1]
    assert command[command.index("--disallowedTools") + 1] == "mcp__*"
    settings = json.loads(command[command.index("--settings") + 1])
    assert settings["forceLoginMethod"] == "claudeai"
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert json.loads(command[command.index("--json-schema") + 1]) == req.schema
    assert "--no-session-persistence" in command
    assert not any("bypass" in argument or "skip-permissions" in argument for argument in command)
    req.role_name = "reviewer"
    req.role.reasoning = "default"
    command = claude_command(req)
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--effort" not in command


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
@pytest.mark.parametrize("use_mcp", [False, True])
def test_claude_mcp_grants_preserve_builtin_role_permissions(tmp_path, role, use_mcp):
    req = request(tmp_path, role=role, provider="claude")
    req.provider.executable = sys.executable
    req.role.use_mcp = use_mcp
    req.role.mcp_tools = ["mcp__jira__get_issue", "mcp__jira__search"]
    command = claude_command(req)
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == (
        "Read,Glob,Grep,Edit,Write,Bash" if role == "worker" else "Read,Glob,Grep"
    )
    allowed = command[command.index("--allowedTools") + 1].split(",")
    assert ("mcp__jira__get_issue" in allowed) is use_mcp
    assert ("mcp__jira__search" in allowed) is use_mcp
    assert ("--disallowedTools" in command) is not use_mcp
    assert "Bash" not in allowed
    assert ("Edit(./**)" in allowed) is (role == "worker")


def test_claude_mcp_can_reuse_existing_permission_grants(tmp_path):
    req = request(tmp_path, provider="claude")
    req.provider.executable = sys.executable
    req.role.use_mcp = True
    command = claude_command(req)
    assert "--disallowedTools" not in command
    assert command[command.index("--allowedTools") + 1] == "Read,Glob,Grep,Edit(./**)"


async def test_claude_mcp_grants_reach_subprocess_and_tool_activity_is_streamed(tmp_path):
    req = request(tmp_path, role="planner", provider="claude")
    req.role.use_mcp = True
    req.role.mcp_tools = ["mcp__jira__get_issue"]
    req.provider.executable = fake_claude(
        tmp_path,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "mcp__jira__get_issue"}]
                },
            },
            claude_result(result="Jira BACKEND-123 requires handling empty exports"),
        ],
        mcp_tools=req.role.mcp_tools,
    )
    events = []

    async def capture(kind, message, data):
        events.append((kind, message))

    result = await ClaudeBackend().run(req, capture)
    assert "BACKEND-123" in result.text
    assert ("agent", "mcp__jira__get_issue") in events


async def test_claude_stream_terminal_result_and_native_alias(tmp_path):
    req = request(tmp_path, provider="claude")
    req.provider.executable = fake_claude(
        tmp_path,
        [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Draft answer"},
                        {"type": "tool_use", "name": "Edit"},
                    ]
                },
            },
            claude_result(),
        ],
    )
    seen = []

    async def collect(kind, message, data):
        seen.append(message)

    result = await ClaudeBackend().run(req, collect)
    assert result.text == "Fixed"
    assert result.session_id == "claude-1"
    assert result.usage["input_tokens"] == 20
    assert result.usage["total_cost_usd"] == 0.01
    assert "Draft answer" in seen and "Edit" in seen


async def test_claude_structured_output_is_authoritative(tmp_path):
    req = request(tmp_path, role="planner", provider="claude")
    req.schema = {"type": "object"}
    req.provider.executable = fake_claude(tmp_path, [claude_result(structured_output={"ok": True})])
    assert json.loads((await ClaudeBackend().run(req, ignore)).text) == {"ok": True}


@pytest.mark.parametrize("method", ["api_key", "api_key_helper", "third_party", "none"])
async def test_claude_rejects_non_subscription_auth_before_model_request(tmp_path, method):
    req = request(tmp_path, provider="claude")
    req.provider.executable = fake_claude(tmp_path, [], method=method)
    with pytest.raises(BackendError, match="claude auth login"):
        await ClaudeBackend().run(req, ignore)


@pytest.mark.parametrize(
    "events,code,message",
    [
        (
            [{"type": "assistant", "message": {"content": [{"text": "Partial"}]}}],
            0,
            "No successful terminal result",
        ),
        (
            [claude_result(subtype="error_max_turns", is_error=True, errors=["limit reached"])],
            0,
            "limit reached",
        ),
        ([claude_result()], 1, "exit 1"),
        ([claude_result(), claude_result()], 0, "multiple results"),
        ([["malformed"]], 0, "malformed event"),
    ],
)
async def test_claude_incomplete_or_failed_stream_is_not_accepted(tmp_path, events, code, message):
    req = request(tmp_path, provider="claude")
    req.provider.executable = fake_claude(tmp_path, events, code=code)
    with pytest.raises(BackendError, match=message):
        await ClaudeBackend().run(req, ignore)


async def test_claude_pinned_model_cannot_silently_change(tmp_path):
    req = request(tmp_path, provider="claude")
    req.role.model = "claude-sonnet-4-6"
    req.provider.executable = fake_claude(
        tmp_path,
        [
            {"type": "system", "subtype": "init", "model": "claude-opus-4-6"},
            claude_result(),
        ],
    )
    with pytest.raises(BackendError, match="instead of"):
        await ClaudeBackend().run(req, ignore)


async def test_claude_schema_result_must_include_structured_output(tmp_path):
    req = request(tmp_path, role="planner", provider="claude")
    req.schema = {"type": "object"}
    req.provider.executable = fake_claude(tmp_path, [claude_result(result='{"ok":true}')])
    with pytest.raises(BackendError, match="required structured output"):
        await ClaudeBackend().run(req, ignore)
