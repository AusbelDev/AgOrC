import sys

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
