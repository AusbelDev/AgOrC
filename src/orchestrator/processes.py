from __future__ import annotations

import asyncio
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

LineCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class ProcessResult:
    code: int
    stdout: str
    stderr: str


async def stop_process(process: asyncio.subprocess.Process) -> None:
    # Kill the process group even when its leader exited but a child holds a pipe.
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
    if os.name == "posix":
        # The leader can exit before a child that ignores SIGTERM. Reap the whole group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def run_process(
    argv: list[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    on_line: LineCallback | None = None,
    timeout: float = 900,  # noqa: ASYNC109 - deadline is enforced below with asyncio.timeout
    capture_limit: int | None = 100000,
) -> ProcessResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
        limit=8 * 1024 * 1024,
    )
    buffers: dict[str, deque[str]] = {
        "stdout": deque(maxlen=2000 if capture_limit is not None else None),
        "stderr": deque(maxlen=2000),
    }

    async def read(stream: asyncio.StreamReader, name: str) -> None:
        while line := await stream.readline():
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            buffers[name].append(
                text if capture_limit is None and name == "stdout" else text[-20000:]
            )
            if on_line:
                await on_line(name, text)

    async def write() -> None:
        if process.stdin is not None:
            try:
                process.stdin.write((input_text or "").encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

    assert process.stdout is not None and process.stderr is not None
    tasks = [
        asyncio.create_task(read(process.stdout, "stdout")),
        asyncio.create_task(read(process.stderr, "stderr")),
        asyncio.create_task(write()),
    ]
    succeeded = False
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks)
            code = await process.wait()
            succeeded = True
    finally:
        if not succeeded:
            await stop_process(process)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    stdout = "\n".join(buffers["stdout"])
    if capture_limit is not None:
        stdout = stdout[-capture_limit:]
    return ProcessResult(code, stdout, "\n".join(buffers["stderr"])[-20000:])
