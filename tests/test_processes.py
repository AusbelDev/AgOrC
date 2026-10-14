import asyncio
import sys

import pytest

from orchestrator.processes import run_process


async def test_stdout_and_stderr_are_drained_concurrently(tmp_path):
    result = await run_process(
        [
            sys.executable,
            "-c",
            "import sys; "
            "[print('x' * 1000, file=sys.stderr) for _ in range(200)]; "
            "print(sys.stdin.read())",
        ],
        tmp_path,
        input_text="task text",
        timeout=5,
    )
    assert result.code == 0
    assert result.stdout == "task text"
    assert result.stderr


async def test_structural_output_can_be_captured_without_truncation(tmp_path):
    result = await run_process(
        [sys.executable, "-c", "print('x' * 150000)"],
        tmp_path,
        capture_limit=None,
        timeout=5,
    )
    assert len(result.stdout) == 150000


async def test_timeout_terminates_spawned_process_group(tmp_path):
    marker = tmp_path / "should-not-exist"
    child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
    script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); "
    script += "time.sleep(20)"
    with pytest.raises(asyncio.TimeoutError):
        await run_process([sys.executable, "-c", script], tmp_path, timeout=0.2)
    await asyncio.sleep(1.1)
    assert not marker.exists()
