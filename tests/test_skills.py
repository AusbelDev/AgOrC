import asyncio
import json
from pathlib import Path

import pytest

from orchestrator import git
from orchestrator.adapters import AgentResult, BackendRegistry
from orchestrator.config import ConfigError, default_config, load_config, save_config
from orchestrator.engine import Engine
from orchestrator.skills import (
    SkillError,
    cleanup_runtime,
    discover_skills,
    global_skill_roots,
    materialize_skills,
    skill_prompt,
    stage_skills,
    validate_snapshot,
)


def write_skill(root, name, body="Follow our team rules.", description="Use for work bugs."):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
    )
    return directory


def test_global_roots_use_each_harness_and_codex_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    assert global_skill_roots("codex-cli") == [
        tmp_path / "custom-codex/skills",
        tmp_path / ".agents/skills",
    ]
    assert global_skill_roots("agy-cli") == [tmp_path / ".gemini/antigravity-cli/skills"]


def test_global_discovery_is_per_harness_with_required_selection(tmp_path, monkeypatch):
    codex = tmp_path / "codex-skills"
    agy = tmp_path / "agy-skills"
    write_skill(codex, "review-rules")
    write_skill(agy, "bug-workflow")
    monkeypatch.setattr(
        "orchestrator.skills.global_skill_roots",
        lambda backend: [codex if backend == "codex-cli" else agy],
    )
    config = default_config()
    config.roles["worker"].skills = []
    config.roles["worker"].required_skills = ["bug-workflow"]
    found = discover_skills(config, tmp_path)
    assert [skill.name for skill in found["planner"]] == ["review-rules"]
    assert [skill.name for skill in found["worker"]] == ["bug-workflow"]
    assert [skill.name for skill in found["reviewer"]] == ["review-rules"]


def test_project_and_configured_skills_override_global_definitions(tmp_path, monkeypatch):
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    extra = tmp_path / "extra"
    write_skill(global_root, "same-name", body="Global instructions")
    write_skill(project / ".agents/skills", "same-name", body="Project instructions")
    write_skill(extra, "same-name", body="Configured instructions")
    monkeypatch.setattr("orchestrator.skills.global_skill_roots", lambda backend: [global_root])
    config = default_config()
    assert discover_skills(config, project)["worker"][0].instructions == "Project instructions"
    config.skills.paths = [str(extra)]
    assert discover_skills(config, project)["worker"][0].instructions == "Configured instructions"


def test_missing_required_skill_fails_clearly(tmp_path):
    config = default_config()
    config.roles["worker"].required_skills = ["company-guidelines"]
    with pytest.raises(SkillError, match="Role worker: skills not found: company-guidelines"):
        discover_skills(config, tmp_path)


def test_yaml_multiline_description_and_symlinked_skill_folder(tmp_path):
    target = write_skill(
        tmp_path / "packages",
        "bug-workflow",
        description="|\n  Use for bugs.\n  Follow Jira rules.",
    )
    links = tmp_path / "skills"
    links.mkdir()
    (links / "linked-skill").symlink_to(target, target_is_directory=True)
    config = default_config()
    config.skills.paths = [str(links)]
    found = discover_skills(config, tmp_path)["worker"]
    assert found[0].name == "bug-workflow"
    assert found[0].description == "Use for bugs.\nFollow Jira rules."


def test_skill_paths_are_relative_to_configuration_file(tmp_path):
    config = default_config()
    config.skills.paths = ["./team-skills"]
    write_skill(tmp_path / "team-skills", "company-rules")
    profile = tmp_path / "work.toml"
    save_config(config, profile)
    loaded = load_config(profile)
    assert loaded.skills.paths == [str(tmp_path / "team-skills")]
    assert discover_skills(loaded, tmp_path)["worker"][0].name == "company-rules"


@pytest.mark.parametrize("values", ["rules", [1], [""], ["*"]])
def test_invalid_required_skill_configuration(values):
    config = default_config()
    config.roles["worker"].required_skills = values
    with pytest.raises(ConfigError):
        config.validate()


def test_snapshot_preserves_resources_and_loads_only_required_body(tmp_path):
    library = tmp_path / "library"
    required = write_skill(
        library, "company-rules", body="REQUIRED_RULE_BODY\nRead references/rules.md."
    )
    (required / "references").mkdir()
    (required / "references/rules.md").write_text("Reference content")
    (required / "scripts").mkdir()
    (required / "scripts/check.sh").write_text("#!/bin/sh\nexit 0\n")
    (required / "scripts/check.sh").chmod(0o755)
    write_skill(library, "optional-workflow", body="OPTIONAL_RULE_BODY")
    config = default_config()
    config.skills.paths = [str(library)]
    config.roles["worker"].required_skills = ["company-rules"]
    snapshots = tmp_path / "saved-run"
    snapshots.mkdir()
    catalog = stage_skills(discover_skills(config, tmp_path), snapshots, "skills", config)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = ".orchestrator-skills-0123456789ab"
    runtime = materialize_skills(snapshots, workspace, directory, catalog["worker"])
    prompt = skill_prompt(workspace, runtime)
    assert "REQUIRED_RULE_BODY" in prompt
    assert "OPTIONAL_RULE_BODY" not in prompt
    package = workspace / Path(runtime[0]["path"]).parent
    assert (package / "references/rules.md").read_text() == "Reference content"
    assert (package / "scripts/check.sh").stat().st_mode & 0o111
    validate_snapshot(workspace, {"worker": runtime})
    cleanup_runtime(workspace, directory)
    assert not (workspace / directory).exists()
    assert (snapshots / catalog["worker"][0]["path"]).exists()


def test_resource_symlink_cannot_copy_files_outside_package(tmp_path):
    library = tmp_path / "library"
    directory = write_skill(library, "rules")
    outside = tmp_path / "private-file"
    outside.write_text("Must not be copied")
    (directory / "outside.txt").symlink_to(outside)
    config = default_config()
    config.skills.paths = [str(library)]
    with pytest.raises(SkillError, match="outside its package"):
        stage_skills(discover_skills(config, tmp_path), tmp_path, "saved-skills", config)
    assert not (tmp_path / "saved-skills").exists()


async def test_skill_runtime_is_cleaned_on_interruption(store, project, tmp_path):
    library = tmp_path / "library"
    write_skill(library, "company-rules")
    config = default_config()
    config.skills.paths = [str(library)]
    started = asyncio.Event()

    class BlockingBackend:
        async def run(self, request, emit):
            assert await asyncio.to_thread(
                lambda: list(request.workspace.glob(".orchestrator-skills-*/*/SKILL.md"))
            )
            started.set()
            await asyncio.Event().wait()

    registry = BackendRegistry()
    registry.register("codex-cli", BlockingBackend())
    engine = Engine(store, registry)
    run = await engine.create("Fix value", project, config)
    task = asyncio.create_task(engine.execute(run.id))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    assert (await task).status == "paused"
    assert not await asyncio.to_thread(
        lambda: list(Path(run.worktree).glob(".orchestrator-skills-*"))
    )


async def test_engine_skill_loading_is_pinned_and_temporary(store, project, tmp_path):
    library = tmp_path / "library"
    source = write_skill(library, "company-rules", body="ORIGINAL_REQUIRED_BODY")
    config = default_config()
    config.skills.paths = [str(library)]
    config.roles["worker"].required_skills = ["company-rules"]
    for name in ("planner", "reviewer"):
        config.roles[name].skills = []
    config.workflow.checks = [
        [
            "python3",
            "-c",
            "from pathlib import Path; assert not list(Path('.').glob('.orchestrator-skills-*'))",
        ]
    ]
    prompts = {}

    class SkillBackend:
        async def run(self, request, emit):
            prompts[request.role_name] = request.prompt
            if request.role_name == "planner":
                assert "SKILLS AVAILABLE" not in request.prompt
                return AgentResult(
                    json.dumps(
                        {
                            "summary": "Fix it",
                            "steps": ["Edit module"],
                            "acceptance_criteria": ["Value is 2"],
                        }
                    )
                )
            if request.role_name == "worker":
                assert "ORIGINAL_REQUIRED_BODY" in request.prompt
                assert "CHANGED_REQUIRED_BODY" not in request.prompt
                skill_file = next(request.workspace.glob(".orchestrator-skills-*/*/SKILL.md"))
                assert "ORIGINAL_REQUIRED_BODY" in skill_file.read_text()
                (request.workspace / "module.py").write_text("value = 2\n")
                return AgentResult("Used company-rules")
            return AgentResult(json.dumps({"accepted": True, "summary": "Correct", "findings": []}))

    registry = BackendRegistry()
    registry.register("codex-cli", SkillBackend())
    registry.register("agy-cli", SkillBackend())
    engine = Engine(store, registry)
    run = await engine.create("Fix value", project, config)
    (source / "SKILL.md").write_text("Changed after creation: CHANGED_REQUIRED_BODY")
    result = await engine.execute(run.id)
    assert result.status == "completed"
    assert result.checks[0]["code"] == 0
    assert not await asyncio.to_thread(
        lambda: list(Path(run.worktree).glob(".orchestrator-skills-*"))
    )
    changes = (store.directory(run.id) / "changes.diff").read_text()
    assert "+value = 2" in changes and "SKILL.md" not in changes


async def test_changed_skill_snapshot_blocks_resume(store, project, tmp_path):
    library = tmp_path / "library"
    write_skill(library, "company-rules")
    config = default_config()
    config.skills.paths = [str(library)]
    engine = Engine(store)
    run = await engine.create("Fix value", project, config)
    snapshot = store.directory(run.id) / run.skills["worker"][0]["path"]
    snapshot.write_text("Tampered snapshot")
    result = await engine.execute(run.id)
    assert result.status == "failed"
    assert "Saved skill package changed" in result.error


async def test_managed_runtime_exclusion_does_not_hide_source_changes(project):
    base = await git.git(project, "rev-parse", "HEAD")
    before = await git.fingerprint(project, base)
    runtime = project / ".orchestrator-skills-0123456789ab"
    runtime.mkdir()
    (runtime / "SKILL.md").write_text("Internal skill")
    ignored = (runtime.name,)
    assert await git.fingerprint(project, base, ignored=ignored) == before
    assert await git.diff(project, base, ignored=ignored) == ""
    (project / "module.py").write_text("value = 2\n")
    assert await git.fingerprint(project, base, ignored=ignored) != before


def test_claude_global_and_project_skills(tmp_path, monkeypatch):
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    assert global_skill_roots("claude-cli") == [home / "skills"]
    write_skill(home / "skills", "global-claude")
    project = tmp_path / "repo"
    write_skill(project / ".claude/skills", "project-claude")
    monkeypatch.setattr("orchestrator.skills.global_skill_roots", global_skill_roots)
    config = default_config()
    for role in config.roles.values():
        role.provider = "claude"
        role.model = "sonnet"
    catalogs = discover_skills(config, project)
    for catalog in catalogs.values():
        assert [skill.name for skill in catalog] == ["global-claude", "project-claude"]
