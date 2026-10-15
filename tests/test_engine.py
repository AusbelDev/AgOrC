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


async def test_mcp_planner_settings_and_requirements_reach_downstream_roles(store, project):
    ticket_plan = {
        "summary": "Jira BACKEND-123 requires value = 2",
        "steps": ["Set value to 2 and preserve the public API"],
        "acceptance_criteria": ["Value is 2", "Public API unchanged"],
    }
    requests = []

    class JiraPlanner(ScriptedBackend):
        async def run(self, request, emit):
            requests.append(request)
            assert "Read Jira BACKEND-123" in request.prompt
            if request.role_name == "planner":
                assert request.role.use_mcp
                return AgentResult(json.dumps(ticket_plan))
            assert not request.role.use_mcp
            assert ticket_plan["summary"] in request.prompt
            assert "Public API unchanged" in request.prompt
            return await super().run(request, emit)

    engine = engine_for(store, JiraPlanner())
    config = default_config()
    config.roles["planner"].use_mcp = True
    run = await engine.create(
        "Read Jira BACKEND-123 and implement its requirements", project, config
    )
    config.roles["planner"].use_mcp = False
    finished = await engine.execute(run.id)
    assert finished.status == "completed"
    assert [req.role_name for req in requests] == ["planner", "worker", "reviewer"]
    assert store.get(run.id).config["roles"]["planner"]["use_mcp"]
    assert "planner: existing CLI MCP configuration enabled" in report(finished)


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


async def test_check_timeout_is_recorded_and_cannot_be_overruled(store, project, monkeypatch):
    async def timed_out(*args, **kwargs):
        raise asyncio.TimeoutError("Verification timed out")

    monkeypatch.setattr("orchestrator.engine.run_process", timed_out)
    engine = engine_for(store, ScriptedBackend())
    config = default_config()
    config.workflow.max_revision_rounds = 0
    config.workflow.checks = [[sys.executable, "-c", "pass"]]
    run = await engine.create("Fix value", project, config)
    finished = await engine.execute(run.id)
    assert finished.status == "needs_attention"
    assert finished.checks[0]["code"] == -1
    assert finished.checks[0]["stderr"] == "Verification timed out"
    assert finished.review["accepted"] is True


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


@pytest.mark.parametrize("container_isolation", [False, True])
async def test_reviewer_changes_fail_instead_of_being_accepted(store, project, container_isolation):
    engine = engine_for(store, ScriptedBackend(mutate_reviewer=True))
    run = await engine.create("Fix value", project, default_config())
    finished = await engine.execute(run.id, container_isolation=container_isolation)
    assert finished.status == "failed"
    assert "reviewer modified" in finished.error


async def test_incomplete_review_preserves_cause_and_can_resume_without_reimplementation(
    store, project
):
    summary = "Review incomplete: bwrap namespace permission denied; repository reads failed."
    backend = ScriptedBackend([{"accepted": False, "summary": summary, "findings": []}])
    engine = engine_for(store, backend)
    config = default_config()
    config.workflow.checks = [[sys.executable, "-c", "from module import value; assert value == 2"]]
    run = await engine.create("Fix value", project, config)
    failed = await engine.execute(run.id)
    assert failed.status == "failed"
    assert failed.stage == "reviewer"
    assert failed.attempt == 0
    assert summary in failed.error
    assert summary in (store.directory(run.id) / "report.md").read_text()
    assert backend.calls == ["planner", "worker", "reviewer"]
    backend.reviews = [ACCEPT]
    resumed = await engine.execute(run.id)
    assert resumed.status == "completed"
    assert backend.calls == ["planner", "worker", "reviewer", "reviewer"]


async def test_explicit_container_resume_keeps_saved_settings_and_retries_review(
    store, project, tmp_path
):
    seen = []

    class NamespaceBackend(ScriptedBackend):
        async def run(self, request, emit):
            seen.append((request.role_name, request.provider.codex_isolation))
            if request.role_name == "reviewer" and request.provider.codex_isolation == "sandbox":
                return AgentResult(json.dumps({
                    "accepted": False,
                    "summary": "bwrap namespace permission denied",
                    "findings": [],
                }))
            return await super().run(request, emit)

    config = default_config()
    config.providers["codex"].auth = "api"
    config.roles["planner"].use_mcp = True
    config.roles["reviewer"].use_mcp = True
    library = tmp_path / "skills" / "rules"
    library.mkdir(parents=True)
    (library / "SKILL.md").write_text(
        "---\nname: rules\ndescription: Testing rules.\n---\nKeep the public API.\n"
    )
    config.skills.paths = [str(library)]
    config.roles["reviewer"].required_skills = ["rules"]
    config.workflow.checks = [[sys.executable, "-c", "from module import value; assert value == 2"]]
    engine = engine_for(store, NamespaceBackend())
    run = await engine.create("Fix value", project, config)
    failed = await engine.execute(run.id)
    assert failed.status == "failed" and failed.stage == "reviewer"
    config.providers["codex"].codex_isolation = "container"
    # Editing defaults alone must not silently change a saved run's permissions.
    still_failed = await engine.execute(run.id)
    assert still_failed.status == "failed"
    resumed = await engine.execute(run.id, container_isolation=True)
    assert resumed.status == "completed"
    assert resumed.checks == failed.checks
    assert resumed.skills == failed.skills
    assert resumed.config == config.to_dict()
    assert [role for role, _ in seen].count("worker") == 1
    assert seen[-1] == ("reviewer", "container")
    events = [e for e in store.events(run.id) if e["kind"] == "isolation"]
    assert len(events) == 1
    assert events[0]["data"]["providers"] == ["codex"]
    assert "Container isolation" in report(resumed)


async def test_container_resume_rejects_runs_without_codex_roles(store, project):
    config = default_config()
    for role in config.roles.values():
        role.provider = "antigravity"
        role.model = "fixture-model"
    engine = engine_for(store, ScriptedBackend())
    run = await engine.create("Fix value", project, config)
    before = run.config
    with pytest.raises(ValueError, match="no Codex roles"):
        await engine.execute(run.id, container_isolation=True)
    assert store.get(run.id).config == before
    assert engine.active is None


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


async def test_claude_cli_end_to_end_uses_structured_roles_and_real_checks(
    store, project, tmp_path
):
    cli = tmp_path / "claude-fixture"
    cli.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['auth', 'status']:\n"
        "    print(json.dumps({'authMethod': 'claude.ai', 'loggedIn': True}))\n"
        "    sys.exit(0)\n"
        "prompt = sys.stdin.read()\n"
        "assert 'Fix value' in prompt\n"
        "result = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Fixed'}\n"
        "if '--json-schema' in sys.argv:\n"
        "    schema = json.loads(sys.argv[sys.argv.index('--json-schema') + 1])\n"
        f"    result['structured_output'] = {PLAN!r} "
        f"if 'steps' in schema['properties'] else {ACCEPT!r}\n"
        "    assert sys.argv[sys.argv.index('--tools') + 1] == 'Read,Glob,Grep'\n"
        "else:\n"
        "    Path('module.py').write_text('value = 2\\n')\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-4-6'}))\n"
        "print(json.dumps(result))\n"
    )
    cli.chmod(0o755)
    config = default_config()
    config.providers["claude"].executable = str(cli)
    for role in config.roles.values():
        role.provider = "claude"
        role.model = "sonnet"
    config.workflow.checks = [[sys.executable, "-c", "from module import value; assert value == 2"]]
    engine = Engine(store)
    run = await engine.create("Fix value", project, config)
    finished = await engine.execute(run.id)
    assert finished.status == "completed", finished.error
    assert finished.plan == PLAN
    assert finished.review == ACCEPT
    assert finished.checks[0]["code"] == 0
    assert (project / "module.py").read_text() == "value = 1\n"
    assert "+value = 2" in (store.directory(run.id) / "changes.diff").read_text()


@pytest.mark.parametrize("isolation", ["sandbox", "container"])
async def test_codex_api_auth_and_mcp_complete_workflow_without_recording_key(
    store, project, tmp_path, isolation
):
    cli = tmp_path / "codex-fixture"
    cli.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['login', 'status']:\n"
        "    print('Logged in using an API key - sk-fixture-secret')\n"
        "    sys.exit(0)\n"
        "assert 'forced_login_method=\"api\"' in sys.argv\n"
        "assert 'forced_login_method=\"chatgpt\"' not in sys.argv\n"
        f"isolation = {isolation!r}\n"
        "sandbox = sys.argv[sys.argv.index('--sandbox') + 1]\n"
        "assert sandbox == ('danger-full-access' if isolation == 'container' else "
        "'read-only' if '--output-schema' in sys.argv else 'workspace-write')\n"
        "assert 'Read Jira BACKEND-123' in sys.stdin.read()\n"
        "response = 'Fixed'\n"
        "if '--output-schema' in sys.argv:\n"
        "    schema_path = Path(sys.argv[sys.argv.index('--output-schema') + 1])\n"
        "    schema = json.loads(schema_path.read_text())\n"
        "    planner = 'steps' in schema['properties']\n"
        "    assert ('--ignore-user-config' in sys.argv) is not planner\n"
        f"    response = json.dumps({PLAN!r} if planner else {ACCEPT!r})\n"
        "else:\n"
        "    Path('module.py').write_text('value = 2\\n')\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': response}}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n"
    )
    cli.chmod(0o755)
    config = default_config()
    config.providers["codex"].auth = "api"
    config.providers["codex"].codex_isolation = isolation
    config.providers["codex"].executable = str(cli)
    for role in config.roles.values():
        role.provider = "codex"
        role.model = "fixture-model"
    config.roles["planner"].use_mcp = True
    config.workflow.checks = [[sys.executable, "-c", "from module import value; assert value == 2"]]
    engine = Engine(store)
    run = await engine.create("Read Jira BACKEND-123 and fix value", project, config)
    finished = await engine.execute(run.id)
    assert finished.status == "completed", finished.error
    assert finished.checks[0]["code"] == 0
    assert finished.plan == PLAN
    assert finished.review == ACCEPT
    assert "codex: API key" in report(finished)
    assert "sk-fixture-secret" not in json.dumps(finished.config)
    assert "sk-fixture-secret" not in report(finished)
    assert "sk-fixture-secret" not in json.dumps(store.events(run.id))
