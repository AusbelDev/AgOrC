from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import git
from orchestrator.config import default_config
from orchestrator.engine import Engine, create_demo
from orchestrator.store import Store


@pytest.fixture(autouse=True)
def isolate_global_skills(monkeypatch):
    # Unit tests supply their own skill folders instead of reading the developer's library.
    monkeypatch.setattr("orchestrator.skills.global_skill_roots", lambda backend: [])


@pytest.fixture
def store(tmp_path: Path):
    storage = Store(tmp_path / "state")
    yield storage
    storage.close()


@pytest.fixture
async def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "module.py").write_text("value = 1\n")
    await git.git(directory, "init", "-b", "main")
    await git.git(directory, "add", ".")
    await git.git(
        directory,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        "Initial",
    )
    return directory


@pytest.fixture
async def demo_run(store):
    return await create_demo(Engine(store), default_config())


@pytest.fixture
async def paused_demo_run(store, demo_run):
    engine = Engine(store)
    original_checks = engine.checks

    async def pause_after_checks(run, config):
        await original_checks(run, config)
        engine.pause()

    engine.checks = pause_after_checks
    paused = await engine.execute(demo_run.id)
    assert paused.status == "paused" and paused.stage == "reviewer"
    return paused
