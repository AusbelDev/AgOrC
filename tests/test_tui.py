import asyncio

from textual.widgets import Button, Input, Select, TextArea

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
        app.query_one("#planner-provider", Select).value = "gemini"
        await pilot.pause()
        assert app.query_one("#planner-model", Input).value == "gemini-3.8-flash"
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
        assert "if values else 0" in app.query_one("#diff", TextArea).text
