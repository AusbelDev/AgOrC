import asyncio
import sys
from pathlib import Path

import pytest

from orchestrator.adapters import (
    AgentRequest,
    BackendError,
    CodexBackend,
    GeminiBackend,
    codex_command,
    effective_gemini_settings,
    gemini_policy,
    gemini_settings,
    login_environment,
)
from orchestrator.config import default_config


async def ignore(*args):
    pass


def request(tmp_path, role="worker", provider="gemini"):
    config = default_config()
    config.roles[role].provider = provider
    config.roles[role].model = config.providers[provider].models[0]
    return AgentRequest(role, config.roles[role], config.providers[provider], tmp_path, "Fix a bug")


def fake_cli(tmp_path: Path, events: list[dict], code: int = 0) -> str:
    path = tmp_path / "fake-cli"
    path.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "sys.stdin.read()\n"
        f"events = {events!r}\n"
        "for event in events: print(json.dumps(event), flush=True)\n"
        f"sys.exit({code})\n"
    )
    path.chmod(0o755)
    return str(path)


def test_logins_do_not_inherit_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    monkeypatch.setenv("GEMINI_API_KEY", "private-key")
    monkeypatch.setenv("CODEX_HOME", "/tmp/existing-login")
    environment = login_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "GEMINI_API_KEY" not in environment
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


def test_gemini_role_settings_and_policy(tmp_path):
    worker = request(tmp_path)
    settings = gemini_settings(worker)
    assert settings["security"]["auth"]["enforcedType"] == "oauth-personal"
    thinking = settings["modelConfigs"]["overrides"][0]
    assert thinking["modelConfig"]["generateContentConfig"]["thinkingConfig"] == {
        "thinkingLevel": "MEDIUM"
    }
    policy = gemini_policy(True)
    assert 'toolName = "*"\ndecision = "deny"' in policy
    assert '"write_file"' in policy
    assert '"write_file"' not in gemini_policy(False)
    assert '"run_shell_command"' not in policy


def test_gemini_preserves_system_policies_and_auth_restrictions(tmp_path, monkeypatch):
    import json

    path = tmp_path / "system-settings.json"
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(path))
    path.write_text(
        json.dumps(
            {
                "adminPolicyPaths": ["/company/policies"],
                "general": {
                    "disableYoloMode": True,
                },
            }
        )
    )
    effective = effective_gemini_settings(request(tmp_path))
    assert effective["adminPolicyPaths"] == ["/company/policies"]
    assert effective["general"]["disableYoloMode"] is True
    path.write_text(json.dumps({"security": {"auth": {"enforcedType": "vertex-ai"}}}))
    with pytest.raises(BackendError, match="system settings enforce"):
        effective_gemini_settings(request(tmp_path))


async def test_gemini_expired_login_has_actionable_error(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(tmp_path, [], code=41)
    with pytest.raises(BackendError, match="Run `gemini`"):
        await GeminiBackend().run(req, ignore)


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


async def test_gemini_stream_assembly(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(
        tmp_path,
        [
            {"type": "init", "session_id": "gemini-1", "model": req.role.model},
            {"type": "message", "role": "user", "content": "Task"},
            {"type": "message", "role": "assistant", "content": "Fix", "delta": True},
            {"type": "message", "role": "assistant", "content": "ed", "delta": True},
            {"type": "result", "status": "success", "stats": {"total_tokens": 4}},
        ],
    )
    result = await GeminiBackend().run(req, ignore)
    assert result.text == "Fixed"
    assert result.usage == {"total_tokens": 4}


async def test_gemini_model_change_is_visible_error(tmp_path):
    req = request(tmp_path)
    req.provider.executable = fake_cli(
        tmp_path,
        [
            {"type": "init", "model": "different-model"},
        ],
    )
    with pytest.raises(BackendError, match="instead of"):
        await GeminiBackend().run(req, ignore)


async def test_gemini_stage_settings_are_temporary(tmp_path):
    req = request(tmp_path)
    script = tmp_path / "inspect-cli"
    script.write_text(
        f"#!{sys.executable}\nimport os, json, sys\n"
        "sys.stdin.read()\n"
        "path = os.environ['GEMINI_CLI_SYSTEM_SETTINGS_PATH']\n"
        "settings = json.load(open(path))\n"
        "assert settings['security']['auth']['enforcedType'] == 'oauth-personal'\n"
        "assert 'GEMINI_API_KEY' not in os.environ\n"
        "print(json.dumps({'type': 'message', 'role': 'assistant', 'content': path}))\n"
        "print(json.dumps({'type': 'result', 'status': 'success'}))\n"
    )
    script.chmod(0o755)
    req.provider.executable = str(script)
    result = await GeminiBackend().run(req, ignore)
    assert not await asyncio.to_thread(Path(result.text).exists)
