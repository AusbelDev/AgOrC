import json
import sys

from orchestrator.adapters import AgentResult, BackendRegistry
from orchestrator.cli import doctor
from orchestrator.config import default_config, save_config
from orchestrator.processes import run_process
from orchestrator.store import Store


async def test_installed_cli_demo_and_saved_report(tmp_path):
    state = tmp_path / "state"
    result = await run_process(
        [sys.executable, "-m", "orchestrator.cli", "--state-dir", str(state), "demo"],
        tmp_path,
        timeout=15,
    )
    assert result.code == 0, result.stderr
    assert "Status: completed" in result.stdout
    store = Store(state)
    try:
        run = store.list()[0]
        assert run.demo and run.checks[0]["code"] == 0
        shown = await run_process(
            [sys.executable, "-m", "orchestrator.cli", "--state-dir", str(state), "show", run.id],
            tmp_path,
            timeout=5,
        )
        assert shown.code == 0
        assert "Status: **completed**" in shown.stdout
        assert "The guard fixes the bug" in shown.stdout
    finally:
        store.close()


async def test_cli_skill_catalog_lists_names_and_required_state(tmp_path):
    package = tmp_path / "team-skills" / "company-rules"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: company-rules\ndescription: Use for work projects.\n---\n"
        "PRIVATE_INSTRUCTIONS_BODY\n"
    )
    config = default_config()
    config.skills.inherit_global = False
    config.skills.paths = [str(package.parent)]
    config.roles["worker"].required_skills = ["company-rules"]
    profile = tmp_path / "work.toml"
    save_config(config, profile)
    result = await run_process(
        [
            sys.executable,
            "-m",
            "orchestrator.cli",
            "--config",
            str(profile),
            "skills",
            "--role",
            "worker",
            "--json",
        ],
        tmp_path,
        timeout=5,
    )
    assert result.code == 0, result.stderr
    found = json.loads(result.stdout)["worker"]
    assert found[0]["name"] == "company-rules" and found[0]["required"] is True
    assert "PRIVATE_INSTRUCTIONS_BODY" not in result.stdout


async def test_live_skill_diagnostic_requires_reading_skill_and_reference(
    tmp_path, monkeypatch, capsys
):
    executable = tmp_path / "fixture-cli"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if sys.argv[1:] == ['--version']: print('fixture 1.0')\n"
        "elif sys.argv[1:] == ['login', 'status']: print('Logged in using ChatGPT')\n"
        "else: sys.exit(1)\n"
    )
    executable.chmod(0o755)
    config = default_config()
    for provider in config.providers.values():
        provider.executable = str(executable)
    requests = []

    class SkillReader:
        async def run(self, request, emit):
            requests.append(request)
            assert "Read references/marker.txt" not in request.prompt
            entry = next(
                json.loads(line)
                for line in request.prompt.splitlines()
                if line.startswith('{"name":')
            )
            assert entry["required"] is False
            main = request.workspace / entry["path"]
            assert "Read references/marker.txt" in main.read_text()
            token = (main.parent / "references/marker.txt").read_text()
            assert token not in request.prompt
            return AgentResult(json.dumps({"token": token}))

    monkeypatch.setattr(BackendRegistry, "get", lambda self, provider: SkillReader())
    assert await doctor(config, live=True, test_skills=True) == 0
    assert len(requests) == 2
    assert "reference file read succeeded" in capsys.readouterr().out


async def test_doctor_skips_missing_unused_providers(tmp_path, monkeypatch, capsys):
    executable = tmp_path / "fixture-codex"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if sys.argv[1:] == ['--version']: print('fixture 1.0')\n"
        "else: print('Logged in using ChatGPT')\n"
    )
    executable.chmod(0o755)
    config = default_config()
    config.providers["codex"].executable = str(executable)
    config.providers["claude"].executable = "missing-claude-fixture"
    config.providers["antigravity"].executable = "missing-agy-fixture"
    config.roles["worker"].provider = "codex"
    assert await doctor(config) == 0
    assert "claude: unused" in capsys.readouterr().out


async def test_doctor_claude_login(tmp_path, capsys):
    executable = tmp_path / "fixture-claude"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "if sys.argv[1:] == ['--version']: print('Claude Code fixture')\n"
        "else: print(json.dumps({'authMethod': 'claude.ai', 'loggedIn': True}))\n"
    )
    executable.chmod(0o755)
    config = default_config()
    config.providers["claude"].executable = str(executable)
    for role in config.roles.values():
        role.provider = "claude"
        role.model = "sonnet"
    assert await doctor(config) == 0
    assert "Logged in using claude.ai" in capsys.readouterr().out


async def test_live_doctor_checks_distinct_mcp_settings(tmp_path, monkeypatch, capsys):
    executable = tmp_path / "fixture-codex"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if sys.argv[1:] == ['--version']: print('fixture 1.0')\n"
        "else: print('Logged in using ChatGPT')\n"
    )
    executable.chmod(0o755)
    config = default_config()
    config.providers["codex"].executable = str(executable)
    for role in config.roles.values():
        role.provider = "codex"
        role.model = "fixture-model"
        role.reasoning = "default"
    config.roles["planner"].use_mcp = True
    seen = []

    class Probe:
        async def run(self, request, emit):
            seen.append(request.role.use_mcp)
            return AgentResult('{"ok":true}')

    monkeypatch.setattr(BackendRegistry, "get", lambda self, provider: Probe())
    assert await doctor(config, live=True) == 0
    assert seen == [True, False]
    output = capsys.readouterr().out
    assert "planner MCP: enabled" in output
    assert "connection not tested" in output


async def test_doctor_accepts_api_auth_and_does_not_print_key(tmp_path, capsys):
    cli = tmp_path / "codex"
    cli.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if sys.argv[1:] == ['--version']: print('fixture 1.0')\n"
        "else: print('Logged in using an API key - sk-fixture-secret')\n"
    )
    cli.chmod(0o755)
    config = default_config()
    config.providers["codex"].auth = "api"
    config.providers["codex"].executable = str(cli)
    for role in config.roles.values():
        role.provider = "codex"
        role.model = "fixture-model"
    assert await doctor(config) == 0
    output = capsys.readouterr().out
    assert "Logged in using API key" in output
    assert "sk-fixture-secret" not in output
    config.providers["codex"].auth = "login"
    assert await doctor(config) == 1
    output = capsys.readouterr().out
    assert "Current CLI login: API key" in output
    assert "sk-fixture-secret" not in output


async def test_cli_container_resume_updates_saved_isolation_and_finishes_pending_review(
    store, paused_demo_run, tmp_path
):
    result = await run_process(
        [
            sys.executable, "-m", "orchestrator.cli",
            "--state-dir", str(store.root),
            "resume", paused_demo_run.id, "--container-isolation",
        ],
        tmp_path,
        timeout=15,
    )
    assert result.code == 0, result.stderr
    assert "Container isolation enabled" in result.stdout
    assert "Status: completed" in result.stdout
    saved = store.get(paused_demo_run.id)
    assert saved.config["providers"]["codex"]["codex_isolation"] == "container"
    assert saved.checks == paused_demo_run.checks
