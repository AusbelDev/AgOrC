import asyncio
import json
import sys

import pytest

from orchestrator import git
from orchestrator.adapters import AgentResult, BackendError, BackendRegistry
from orchestrator.config import default_config
from orchestrator.engine import Engine, parse_response, report

PLAN = {"summary": "Fix value", "steps": ["Update value"], "acceptance_criteria": ["Value is 2"]}
ACCEPT = {"accepted": True, "summary": "Correct", "findings": []}
REJECT = {
    "accepted": False,
    "summary": "Needs a fix",
    "findings": [
        {
            "severity": "blocking",
            "file": "module.py",
            "line": 1,
            "message": "Value is wrong",
            "requested_change": "Set value to 2",
        }
    ],
}


class ScriptedBackend:
    def __init__(self, reviews=None, *, mutate_reviewer=False):
        self.calls = []
        self.reviews = reviews or [ACCEPT]
        self.mutate_reviewer = mutate_reviewer

    async def run(self, request, emit):
        self.calls.append(request.role_name)
        if request.role_name == "planner":
            return AgentResult(json.dumps(PLAN))
        if request.role_name == "worker":
            (request.workspace / "module.py").write_text("value = 2\n")
            (request.workspace / "new.py").write_text("new = True\n")
            return AgentResult("Updated the value")
        if self.mutate_reviewer:
            (request.workspace / "module.py").write_text("value = 3\n")
        review = self.reviews[min(self.calls.count("reviewer") - 1, len(self.reviews) - 1)]
        return AgentResult(json.dumps(review))


def engine_for(store, backend):
    registry = BackendRegistry()
    registry.register("codex-cli", backend)
    registry.register("agy-cli", backend)
    return Engine(store, registry)


async def test_demo_completes_real_checks_without_touching_source(store, demo_run):
    original = (store.root / "demo-projects").glob("*/average.py")
    source = next(original)
    before = source.read_text()
    finished = await Engine(store).execute(demo_run.id)
    assert finished.status == "completed"
    assert finished.checks[0]["code"] == 0
    assert source.read_text() == before
    assert "if values else 0" in (store.directory(finished.id) / "changes.diff").read_text()
    assert "Acceptance criteria" in (store.directory(finished.id) / "report.md").read_text()


async def test_revision_loop_and_untracked_diff(store, project):
    backend = ScriptedBackend([REJECT, ACCEPT])
    engine = engine_for(store, backend)
    config = default_config()
    config.workflow.checks = [[sys.executable, "-c", "from module import value; assert value == 2"]]
    run = await engine.create("Fix value", project, config)
    finished = await engine.execute(run.id)
    assert finished.status == "completed"
    assert finished.attempt == 1
    assert backend.calls == ["planner", "worker", "reviewer", "worker", "reviewer"]
    assert (project / "module.py").read_text() == "value = 1\n"
    changes = (store.directory(run.id) / "changes.diff").read_text()
    assert "new.py" in changes
    assert "+value = 2" in changes


async def test_failed_checks_cannot_be_overruled_by_reviewer(store, project):
    engine = engine_for(store, ScriptedBackend())
    config = default_config()
    config.workflow.max_revision_rounds = 1
    config.workflow.checks = [[sys.executable, "-c", "raise SystemExit(1)"]]
    run = await engine.create("Fix value", project, config)
    finished = await engine.execute(run.id)
    assert finished.status == "needs_attention"
    assert finished.attempt == 1
    assert finished.review["accepted"] is True
    assert finished.checks[0]["code"] == 1


async def test_pause_resume_keeps_config_and_rechecks_external_changes(store, project):
    backend = ScriptedBackend()
    engine = engine_for(store, backend)
    config = default_config()
    config.workflow.checks = [[sys.executable, "-c", "from module import value; assert value == 2"]]

    async def pause_after_checks(run, event):
        if event["kind"] == "check-output":
            engine.pause()

    # Pause after the checks stage has been saved, immediately before review.
    original_checks = engine.checks

    async def pausing_checks(run, config):
        await original_checks(run, config)
        engine.pause()

    engine.checks = pausing_checks
    run = await engine.create("Fix value", project, config)
    paused = await engine.execute(run.id)
    assert paused.status == "paused" and paused.stage == "reviewer"
    (project / "module.py").write_text("source changed outside the task\n")
    from pathlib import Path

    (Path(paused.worktree) / "module.py").write_text("value = 99\n")
    engine.checks = original_checks
    config.roles["worker"].model = "some-other-model"
    finished = await engine.execute(run.id)
    assert finished.status == "completed"
    assert finished.config["roles"]["worker"]["model"] == "gemini-3.8-flash-medium"
    assert any("Workspace changed" in event["message"] for event in store.events(run.id))
    assert backend.calls.count("worker") == 2


async def test_cancellation_persists_stage_and_resumes(store, project):
    class BlockingBackend(ScriptedBackend):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.block = True

        async def run(self, request, emit):
            if request.role_name == "worker" and self.block:
                self.started.set()
                await asyncio.Event().wait()
            return await super().run(request, emit)

    backend = BlockingBackend()
    engine = engine_for(store, backend)
    run = await engine.create("Fix value", project, default_config())
    task = asyncio.create_task(engine.execute(run.id))
    await asyncio.wait_for(backend.started.wait(), 3)
    task.cancel()
    paused = await task
    assert paused.status == "paused" and paused.stage == "worker"
    assert store.get(run.id).status == "paused"
    backend.block = False
    assert (await engine.execute(run.id)).status == "completed"
    assert backend.calls.count("planner") == 1


async def test_reviewer_changes_fail_instead_of_being_accepted(store, project):
    engine = engine_for(store, ScriptedBackend(mutate_reviewer=True))
    run = await engine.create("Fix value", project, default_config())
    finished = await engine.execute(run.id)
    assert finished.status == "failed"
    assert "reviewer modified" in finished.error


async def test_instructions_added_during_worker_require_another_pass(store, project):
    backend = ScriptedBackend()
    engine = engine_for(store, backend)

    class SteeringBackend(ScriptedBackend):
        async def run(self, request, emit):
            result = await super().run(request, emit)
            if request.role_name == "worker" and not engine.active.notes:
                engine.steer("Also preserve public behavior.")
            return result

    backend = SteeringBackend()
    engine = engine_for(store, backend)
    run = await engine.create("Fix value", project, default_config())
    finished = await engine.execute(run.id)
    assert finished.status == "completed"
    assert backend.calls.count("worker") == 2
    assert finished.implemented_notes == 1


async def test_saved_notes_invalidate_pending_verification_and_respect_run_lock(store, project):
    engine = engine_for(store, ScriptedBackend())
    run = await engine.create("Fix value", project, default_config())
    run.stage = "checks"
    store.save(run)
    saved = store.add_note(run.id, "Keep the signature unchanged.")
    assert saved.stage == "worker"
    with store.lock(run.id), pytest.raises(ValueError, match="already executing"):
        store.add_note(run.id, "Concurrent note")


async def test_branch_or_head_change_blocks_resume(store, project):
    engine = engine_for(store, ScriptedBackend())
    run = await engine.create("Fix value", project, default_config())
    from pathlib import Path

    await git.git(Path(run.worktree), "checkout", "-b", "different")
    finished = await engine.execute(run.id)
    assert finished.status == "failed"
    assert "branch changed" in finished.error


def make_legacy_snapshot(run):
    run.config["providers"]["antigravity"].update(
        backend="gemini-cli", executable="gemini", models=["gemini-3.8-flash"]
    )
    run.config["roles"]["worker"]["model"] = "gemini-3.8-flash"


async def test_saved_legacy_run_migrates_and_records_the_new_backend(store, project):
    engine = engine_for(store, ScriptedBackend())
    run = await engine.create("Fix value", project, default_config())
    make_legacy_snapshot(run)
    store.save(run)
    with pytest.warns(UserWarning, match="Migrated Gemini CLI"):
        finished = await engine.execute(run.id)
    assert finished.status == "completed"
    saved = store.get(run.id)
    assert saved.config["providers"]["antigravity"]["backend"] == "agy-cli"
    assert saved.config["roles"]["worker"]["model"] == "gemini-3.8-flash-medium"
    assert any(event["kind"] == "migration" for event in store.events(run.id))


async def test_historical_report_retains_legacy_model_id(store, project):
    engine = engine_for(store, ScriptedBackend())
    run = await engine.create("Fix value", project, default_config())
    make_legacy_snapshot(run)
    text = report(run)
    assert "antigravity / gemini-3.8-flash / medium" in text
    assert "gemini-3.8-flash-medium" not in text
    assert run.config["providers"]["antigravity"]["backend"] == "gemini-cli"


@pytest.mark.parametrize(
    "payload",
    [
        {"accepted": "true", "summary": "Fine", "findings": []},
        {**ACCEPT, "findings": REJECT["findings"]},
        {"accepted": False, "summary": "No", "findings": []},
    ],
)
def test_invalid_review_never_counts_as_acceptance(payload):
    with pytest.raises(BackendError):
        parse_response(json.dumps(payload), "reviewer")
