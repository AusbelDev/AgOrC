from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from .processes import run_process


class GitError(RuntimeError):
    pass


async def git(repo: Path, *args: str) -> str:
    result = await run_process(["git", *args], repo, timeout=30, capture_limit=None)
    if result.code:
        raise GitError(result.stderr or result.stdout or "Git command failed.")
    return result.stdout


async def repository(path: Path) -> tuple[Path, str]:
    if not await asyncio.to_thread(path.is_dir):
        raise GitError(f"Repository directory does not exist: {path}")
    root = await asyncio.to_thread(Path(await git(path, "rev-parse", "--show-toplevel")).resolve)
    try:
        base = await git(root, "rev-parse", "--verify", "HEAD")
    except GitError as exc:
        raise GitError(
            "The target repository needs an initial commit before creating a worktree."
        ) from exc
    return root, base


async def create_worktree(repo: Path, destination: Path, branch: str, base: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    await git(repo, "worktree", "add", "-b", branch, str(destination), base)


def pathspec(ignored: tuple[str, ...]) -> list[str]:
    return [".", *[f":(top,exclude,literal){path}" for path in ignored]]


async def diff(workspace: Path, base: str, *, ignored: tuple[str, ...] = ()) -> str:
    # Include tracked, staged, and new files without changing the real Git index.
    tracked = await git(
        workspace, "diff", "--no-ext-diff", "--no-textconv", base, "--", *pathspec(ignored)
    )
    names = await git(
        workspace, "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec(ignored)
    )
    pieces = [tracked]
    for name in names.split("\0"):
        if not name:
            continue
        result = await run_process(
            [
                "git",
                "diff",
                "--no-index",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                "/dev/null",
                name,
            ],
            workspace,
            timeout=30,
            capture_limit=None,
        )
        if result.code not in (0, 1):
            raise GitError(result.stderr or f"Cannot inspect new file {name}")
        pieces.append(result.stdout)
    return "\n".join(piece for piece in pieces if piece)


async def fingerprint(workspace: Path, base: str, *, ignored: tuple[str, ...] = ()) -> str:
    # Hash content as well as status, including binary edits and large diffs.
    head = await git(workspace, "rev-parse", "HEAD")
    status = await git(workspace, "status", "--porcelain=v1", "-z", "--", *pathspec(ignored))
    paths = await git(
        workspace,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *pathspec(ignored),
    )
    return await asyncio.to_thread(_hash_files, workspace, head, status, paths)


def _hash_files(workspace: Path, head: str, status: str, paths: str) -> str:
    digest = hashlib.sha256()
    digest.update(head.encode())
    digest.update(status.encode())
    for name in sorted(set(paths.split("\0")) - {""}):
        path = workspace / name
        digest.update(name.encode())
        if path.is_symlink():
            digest.update(str(path.readlink()).encode())
        elif path.is_file():
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        else:
            digest.update(b"<deleted>")
    return digest.hexdigest()


async def validate_worktree(workspace: Path, branch: str, base: str) -> None:
    if not await asyncio.to_thread(workspace.is_dir):
        raise GitError("The saved task worktree no longer exists.")
    if await git(workspace, "branch", "--show-current") != branch:
        raise GitError("The task worktree branch changed. Restore its branch before resuming.")
    if await git(workspace, "rev-parse", "HEAD") != base:
        raise GitError("The task worktree HEAD changed. This run expects its original base commit.")
