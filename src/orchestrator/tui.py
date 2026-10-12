from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.suggester import SuggestFromList
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .config import EFFORTS, ROLES, Config, Role, save_config
from .engine import Engine, create_demo, report
from .store import Run, Store


class OrchestratorApp(App):
    TITLE = "Orchestrator"
    SUB_TITLE = "Plan · Implement · Review"
    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+p", "pause", "Pause"),
        ("ctrl+x", "stop", "Stop"),
        ("ctrl+r", "resume", "Resume"),
    ]
    CSS = """
    Screen { background: $background; }
    #layout { height: 1fr; }
    #setup { width: 44; min-width: 32; padding: 1 2; border-right: solid $primary; }
    #workspace { width: 1fr; padding: 0 1; }
    .section { color: $accent; text-style: bold; margin-top: 1; margin-bottom: 1; }
    .role-title { color: $text; text-style: bold; margin-top: 1; }
    Input, Select { margin-bottom: 1; }
    #task { height: 7; }
    #checks { height: 5; }
    #run-summary { height: auto; min-height: 3; padding: 1; background: $surface; }
    #controls { height: auto; padding: 1 0; }
    #controls Button { min-width: 9; margin-right: 1; }
    #launch { height: auto; margin-top: 1; }
    #launch Button { min-width: 12; margin-right: 1; }
    #tabs { height: 1fr; }
    #activity { height: 1fr; border: solid $panel; }
    #diff { height: 1fr; }
    #history { height: 1fr; }
    #steering { height: auto; margin-top: 1; }
    #steering Input { width: 1fr; }
    #steering Button { min-width: 10; }
    Markdown { padding: 1; }
    #profile-note { color: $text-muted; height: auto; }
    """

    def __init__(self, config: Config, config_path: Path, store: Store, repo: Path) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.store = store
        self.repo = repo
        self.engine = Engine(store, listener=self.receive_event)
        self.selected_run: Run | None = None
        self.running_task: asyncio.Task[None] | None = None
        self.selected_providers = {name: role.provider for name, role in config.roles.items()}
        self.model_choices = {
            name: {role.provider: role.model} for name, role in config.roles.items()
        }

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="layout"):
            with VerticalScroll(id="setup"):
                yield Label("TASK", classes="section")
                yield Label("Repository")
                yield Input(str(self.repo), id="repo")
                yield Label("What should be changed?")
                yield TextArea("", id="task")
                yield Label("MODELS", classes="section")
                for name in ROLES:
                    role = self.config.roles[name]
                    provider = self.config.providers[role.provider]
                    yield Label(name.title(), classes="role-title")
                    yield Select(
                        [
                            (f"{key} · {value.backend}", key)
                            for key, value in self.config.providers.items()
                        ],
                        value=role.provider,
                        allow_blank=False,
                        id=f"{name}-provider",
                    )
                    yield Input(
                        role.model,
                        placeholder="Model ID",
                        id=f"{name}-model",
                        suggester=SuggestFromList(provider.models),
                    )
                    yield Select(
                        [(effort.title(), effort) for effort in EFFORTS[provider.backend]],
                        value=role.reasoning,
                        allow_blank=False,
                        id=f"{name}-reasoning",
                    )
                yield Static(
                    "Provider definitions and role instructions live in your config file. "
                    "These settings apply to new runs.",
                    id="profile-note",
                )
                yield Label("VERIFICATION", classes="section")
                yield Label("Check commands (JSON argv arrays)")
                yield TextArea(json.dumps(self.config.workflow.checks, indent=2), id="checks")
                yield Label("Maximum revision rounds")
                yield Input(
                    str(self.config.workflow.max_revision_rounds), type="integer", id="rounds"
                )
                with Horizontal(id="launch"):
                    yield Button("Start task", variant="primary", id="start")
                    yield Button("Offline demo", id="demo")
                yield Button("Save settings", id="save")
            with Vertical(id="workspace"):
                yield Static(
                    "Ready · choose a repository and describe a task, or try the offline demo.",
                    id="run-summary",
                    markup=False,
                )
                with Horizontal(id="controls"):
                    yield Button("Pause", id="pause", disabled=True)
                    yield Button("Stop", id="stop", disabled=True)
                    yield Button("Resume", id="resume", disabled=True)
                with TabbedContent(id="tabs"):
                    with TabPane("Activity", id="activity-tab"):
                        yield RichLog(id="activity", wrap=True, markup=False, max_lines=2000)
                    with TabPane("Plan", id="plan-tab"):
                        with VerticalScroll():
                            yield Markdown(
                                "The planner's steps and acceptance criteria appear here.",
                                id="plan",
                            )
                    with TabPane("Review", id="review-tab"):
                        with VerticalScroll():
                            yield Markdown(
                                "Review findings and verification results appear here.", id="review"
                            )
                    with TabPane("Diff", id="diff-tab"):
                        yield TextArea("", read_only=True, id="diff")
                    with TabPane("History", id="history-tab"):
                        yield DataTable(id="history", cursor_type="row")
                with Horizontal(id="steering"):
                    yield Input(placeholder="Additional instructions for the next stage", id="note")
                    yield Button("Send", id="send-note", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.query_one("#history", DataTable).add_columns("Run", "Status", "Task", "Updated")
        self.refresh_history()

    @on(Select.Changed)
    def provider_changed(self, event: Select.Changed) -> None:
        for name in ROLES:
            if event.select.id != f"{name}-provider" or event.value is Select.BLANK:
                continue
            provider = self.config.providers[str(event.value)]
            efforts = self.query_one(f"#{name}-reasoning", Select)
            current = efforts.value
            efforts.set_options([(effort.title(), effort) for effort in EFFORTS[provider.backend]])
            efforts.value = current if current in EFFORTS[provider.backend] else "default"
            model = self.query_one(f"#{name}-model", Input)
            model.suggester = SuggestFromList(provider.models)
            previous = self.selected_providers[name]
            if previous != event.value:
                self.model_choices[name][previous] = model.value
                model.value = self.model_choices[name].get(
                    str(event.value), provider.models[0] if provider.models else ""
                )
                self.selected_providers[name] = str(event.value)

    def form_config(self) -> Config:
        copied = Config.from_dict(self.config.to_dict())
        for name in ROLES:
            copied.roles[name] = Role(
                provider=str(self.query_one(f"#{name}-provider", Select).value),
                model=self.query_one(f"#{name}-model", Input).value.strip(),
                reasoning=str(self.query_one(f"#{name}-reasoning", Select).value),
                instructions=self.config.roles[name].instructions,
            )
        copied.workflow.checks = json.loads(self.query_one("#checks", TextArea).text)
        copied.workflow.max_revision_rounds = int(self.query_one("#rounds", Input).value)
        return copied.validate()

    def refresh_history(self) -> None:
        table = self.query_one("#history", DataTable)
        table.clear()
        for run in self.store.list():
            table.add_row(run.id, run.status, run.task[:70], run.updated_at, key=run.id)

    def show_run(self, run: Run) -> None:
        self.selected_run = run
        self.query_one("#run-summary", Static).update(
            f"{'OFFLINE DEMO · ' if run.demo else ''}{run.id} · {run.status.upper()} · "
            f"{run.stage.title()} · revision {run.attempt}\n{run.worktree}"
        )
        plan = run.plan
        content = plan.get("summary", "No plan yet.")
        content += "\n\n" + "\n".join(
            f"{i}. {step}" for i, step in enumerate(plan.get("steps", []), 1)
        )
        if plan.get("acceptance_criteria"):
            content += "\n\n**Acceptance criteria**\n\n" + "\n".join(
                f"- {criterion}" for criterion in plan["acceptance_criteria"]
            )
        self.query_one("#plan", Markdown).update(content)
        self.query_one("#review", Markdown).update(report(run))
        self.update_controls()

    def update_controls(self) -> None:
        running = self.running_task is not None and not self.running_task.done()
        for name in ("start", "demo"):
            self.query_one(f"#{name}", Button).disabled = running
        for name in ("pause", "stop"):
            self.query_one(f"#{name}", Button).disabled = not running
        resumable = self.selected_run and self.selected_run.status not in (
            "completed",
            "needs_attention",
        )
        self.query_one("#resume", Button).disabled = running or not resumable
        self.query_one("#send-note", Button).disabled = not self.selected_run or (
            self.selected_run.status in ("completed", "needs_attention")
        )

    async def receive_event(self, run: Run, event: dict[str, Any]) -> None:
        if event["kind"] not in ("agent", "log", "check-output", "usage"):
            self.show_run(run)
        self.query_one("#activity", RichLog).write(
            f"{event['time'][11:19]}  {event['kind']}  {event['message']}"
        )

    async def launch(self, *, demo: bool = False, resume: bool = False) -> None:
        try:
            if resume:
                if not self.selected_run:
                    return
                run = self.selected_run
            else:
                config = self.form_config()
                if demo:
                    run = await create_demo(self.engine, config)
                else:
                    run = await self.engine.create(
                        self.query_one("#task", TextArea).text,
                        Path(self.query_one("#repo", Input).value),
                        config,
                    )
            self.query_one("#activity", RichLog).clear()
            self.query_one("#tabs", TabbedContent).active = "activity-tab"
            self.show_run(run)
            finished = await self.engine.execute(run.id)
            self.show_run(finished)
            diff_path = self.store.directory(run.id) / "changes.diff"
            if diff_path.exists():
                self.query_one("#diff", TextArea).load_text(diff_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.notify(str(exc), title="Cannot run task", severity="error", timeout=12)
        finally:
            self.running_task = None
            self.update_controls()
            self.refresh_history()

    def start_launch(self, **kwargs: bool) -> None:
        if self.running_task and not self.running_task.done():
            return
        self.running_task = asyncio.create_task(self.launch(**kwargs))
        self.update_controls()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name == "start":
            self.start_launch()
        elif name == "demo":
            self.start_launch(demo=True)
        elif name == "resume":
            self.action_resume()
        elif name == "pause":
            self.action_pause()
        elif name == "stop":
            self.action_stop()
        elif name == "save":
            try:
                self.config = self.form_config()
                save_config(self.config, self.config_path)
                self.notify(f"Saved to {self.config_path}")
            except (ValueError, OSError) as exc:
                self.notify(str(exc), severity="error")
        elif name == "send-note":
            note = self.query_one("#note", Input).value.strip()
            if note and self.selected_run:
                if self.engine.active:
                    self.engine.steer(note)
                else:
                    try:
                        self.selected_run = self.store.add_note(self.selected_run.id, note)
                    except ValueError as exc:
                        self.notify(str(exc), severity="error")
                        return
                self.query_one("#note", Input).value = ""
                self.notify("Instructions added for the next stage.")

    @on(DataTable.RowSelected, "#history")
    def history_selected(self, event: DataTable.RowSelected) -> None:
        if self.running_task and not self.running_task.done():
            self.notify("Pause or stop the current task before opening another run.")
            return
        run = self.store.get(str(event.row_key.value))
        self.show_run(run)
        activity = self.query_one("#activity", RichLog)
        activity.clear()
        for item in self.store.events(run.id):
            activity.write(f"{item['time'][11:19]}  {item['kind']}  {item['message']}")
        diff_path = self.store.directory(run.id) / "changes.diff"
        self.query_one("#diff", TextArea).load_text(
            diff_path.read_text(encoding="utf-8") if diff_path.exists() else ""
        )

    def action_pause(self) -> None:
        if self.running_task:
            self.engine.pause()
            self.notify("Will pause when the current stage finishes.")

    def action_stop(self) -> None:
        if self.running_task and self.engine.active:
            self.running_task.cancel()

    def action_resume(self) -> None:
        if self.selected_run and self.selected_run.status not in ("completed", "needs_attention"):
            self.start_launch(resume=True)

    async def action_quit(self) -> None:
        if self.running_task:
            self.running_task.cancel()
            await asyncio.gather(self.running_task, return_exceptions=True)
        self.exit()
