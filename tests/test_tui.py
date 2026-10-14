import asyncio

from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    RichLog,
    Select,
    TabbedContent,
    TextArea,
)

from orchestrator.config import default_config, load_config
from orchestrator.tui import OrchestratorApp


async def test_tui_model_settings_and_offline_workflow(store, tmp_path):
    path = tmp_path / "profile.toml"
    app = OrchestratorApp(default_config(), path, store, tmp_path)
    async with app.run_test(size=(150, 55)) as pilot:
        app.query_one("#worker-model", Input).value = "custom-gemini-model"
        app.query_one("#save", Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()
        assert load_config(path).roles["worker"].model == "custom-gemini-model"
        app.query_one("#planner-provider", Select).value = "antigravity"
        await pilot.pause()
        assert app.query_one("#planner-model", Input).value == "gemini-3.8-flash-medium"
        assert app.query_one("#planner-reasoning", Select).value == "high"
        app.query_one("#planner-provider", Select).value = "codex"
        await pilot.pause()
        assert app.query_one("#planner-model", Input).value == "gpt-6.1-sol"
        app.query_one("#demo", Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#demo")
        await pilot.pause()
        for _ in range(100):
            if app.selected_run and app.selected_run.status == "completed":
                break
            await asyncio.sleep(0.05)
        assert app.selected_run.status == "completed"
        assert app.selected_run.demo
        if app.running_task is not None:
            await asyncio.wait_for(app.running_task, timeout=5)
        assert "if values else 0" in app.query_one("#diff", TextArea).text


async def test_tui_skill_catalog_and_required_settings(store, tmp_path, monkeypatch):
    library = tmp_path / "global-skills"
    directory = library / "work-guidelines"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: work-guidelines\ndescription: Use for company coding tasks.\n---\n"
        "Follow our testing conventions.\n"
    )
    monkeypatch.setattr("orchestrator.skills.global_skill_roots", lambda backend: [library])
    profile = tmp_path / "work.toml"
    app = OrchestratorApp(default_config(), profile, store, tmp_path)
    async with app.run_test(size=(150, 55)) as pilot:
        app.query_one("#tabs", TabbedContent).active = "skills-tab"
        await pilot.pause()
        app.query_one("#worker-required-skills", Input).value = "work-guidelines"
        await app.refresh_skills()
        table = app.query_one("#skills-catalog", DataTable)
        assert table.row_count == 3
        assert any(table.get_row_at(index)[2] == "Required" for index in range(table.row_count))
        app.query_one("#save", Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()
        assert load_config(profile).roles["worker"].required_skills == ["work-guidelines"]


async def test_tui_claude_selection_and_small_terminal(store, tmp_path):
    profile = tmp_path / "profile.toml"
    app = OrchestratorApp(default_config(), profile, store, tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#worker-provider", Select).value = "claude"
        await pilot.pause()
        assert app.query_one("#worker-model", Input).value == "sonnet"
        assert app.query_one("#worker-reasoning", Select).value == "medium"
        app.query_one("#worker-model", Input).value = "claude-pinned-model"
        app.query_one("#worker-provider", Select).value = "antigravity"
        await pilot.pause()
        app.query_one("#worker-provider", Select).value = "claude"
        await pilot.pause()
        assert app.query_one("#worker-model", Input).value == "claude-pinned-model"
        app.query_one("#save", Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()
        saved = load_config(profile)
        assert saved.roles["worker"].provider == "claude"
        assert saved.roles["worker"].model == "claude-pinned-model"
        app.start_launch(demo=True)
        await asyncio.wait_for(app.running_task, timeout=5)
        await pilot.pause()
        assert app.query_one("#activity", RichLog).content_region.height > 0


async def test_tui_stop_during_setup_saves_resumable_run_and_clears_diff(
    store, project, tmp_path, monkeypatch
):
    app = OrchestratorApp(default_config(), tmp_path / "profile.toml", store, project)
    started = asyncio.Event()
    release = asyncio.Event()
    original = app.engine.create

    async def delayed_create(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.engine, "create", delayed_create)
    async with app.run_test(size=(150, 55)):
        app.query_one("#task", TextArea).load_text("Fix a bug")
        app.query_one("#diff", TextArea).load_text("An old run's diff")
        app.start_launch()
        task = app.running_task
        await asyncio.wait_for(started.wait(), timeout=5)
        app.action_stop()
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert app.selected_run.status == "paused"
        assert store.get(app.selected_run.id).stage == "planner"
        assert app.query_one("#diff", TextArea).text == ""
        assert not app.query_one("#resume", Button).disabled
        assert not any(event["kind"] == "stage" for event in store.events(app.selected_run.id))


async def test_tui_relative_skill_paths_use_config_directory(store, tmp_path):
    config_path = tmp_path / "profiles" / "work.toml"
    app = OrchestratorApp(default_config(), config_path, store, tmp_path)
    async with app.run_test(size=(150, 55)):
        app.query_one("#skills-paths", TextArea).load_text('["team-skills"]')
        assert app.form_config().skills.paths == [str(config_path.parent / "team-skills")]


async def test_tui_failed_save_preserves_active_settings(store, tmp_path, monkeypatch):
    app = OrchestratorApp(default_config(), tmp_path / "profile.toml", store, tmp_path)

    def failed_save(*args):
        raise OSError("Settings file is not writable")

    monkeypatch.setattr("orchestrator.tui.save_config", failed_save)
    async with app.run_test(size=(150, 55)) as pilot:
        app.query_one("#worker-model", Input).value = "unsaved-model"
        app.query_one("#save", Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()
        assert app.config.roles["worker"].model == "gemini-3.8-flash-medium"


async def test_tui_mcp_controls_save_per_role_and_follow_provider(store, tmp_path):
    profile = tmp_path / "mcp.toml"
    app = OrchestratorApp(default_config(), profile, store, tmp_path)
    async with app.run_test(size=(150, 55)) as pilot:
        assert app.query_one("#worker-mcp", Checkbox).disabled
        app.query_one("#planner-mcp", Checkbox).value = True
        app.query_one("#worker-provider", Select).value = "claude"
        await pilot.pause()
        toggle = app.query_one("#worker-mcp", Checkbox)
        tools = app.query_one("#worker-mcp-tools", Input)
        assert not toggle.disabled
        assert tools.disabled
        toggle.value = True
        await pilot.pause()
        assert not tools.disabled
        tools.value = "mcp__jira__get_issue, mcp__jira__search"
        app.query_one("#save", Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()
        saved = load_config(profile)
        assert saved.roles["planner"].use_mcp
        assert saved.roles["worker"].use_mcp
        assert saved.roles["worker"].mcp_tools == ["mcp__jira__get_issue", "mcp__jira__search"]
        assert not saved.roles["reviewer"].use_mcp
        toggle.value = False
        await pilot.pause()
        assert tools.disabled
        assert app.form_config().roles["worker"].mcp_tools == saved.roles["worker"].mcp_tools
        app.query_one("#worker-provider", Select).value = "codex"
        await pilot.pause()
        assert tools.disabled
        assert app.form_config().roles["worker"].mcp_tools == []
        app.query_one("#worker-provider", Select).value = "antigravity"
        await pilot.pause()
        assert toggle.disabled
        assert not app.form_config().roles["worker"].use_mcp


async def test_tui_codex_api_auth_is_shared_by_provider_and_persists(store, tmp_path):
    profile = tmp_path / "api.toml"
    config = default_config()
    config.providers["custom codex.with spaces"] = config.providers["codex"]
    config.roles["worker"].provider = "custom codex.with spaces"
    app = OrchestratorApp(config, profile, store, tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        field = app.provider_auth_fields["codex"]
        app.query_one(f"#{field}", Select).value = "api"
        app.query_one("#planner-mcp", Checkbox).value = True
        await pilot.pause()
        app.query_one("#save", Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()
        saved = load_config(profile)
        assert saved.providers["codex"].auth == "api"
        assert saved.providers[saved.roles["planner"].provider].auth == "api"
        assert saved.providers[saved.roles["reviewer"].provider].auth == "api"
        assert saved.providers["custom codex.with spaces"].auth == "login"
        assert saved.providers["claude"].auth == "login"
        assert saved.roles["planner"].use_mcp
    reopened = OrchestratorApp(saved, profile, store, tmp_path)
    async with reopened.run_test(size=(80, 24)):
        field = reopened.provider_auth_fields["codex"]
        assert reopened.query_one(f"#{field}", Select).value == "api"
