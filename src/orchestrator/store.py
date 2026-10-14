from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Run:
    id: str
    task: str
    repo: str
    worktree: str
    branch: str
    base: str
    config: dict[str, Any]
    demo: bool = False
    status: str = "ready"
    stage: str = "planner"
    attempt: int = 0
    plan: dict[str, Any] = field(default_factory=dict)
    worker: str = ""
    review: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    implemented_notes: int = 0
    skills: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    skills_prepared: bool = False
    fingerprint: str = ""
    elapsed_seconds: float = 0
    error: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)


class Store:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = sqlite3.connect(self.root / "runs.sqlite3")
        self.database.execute("PRAGMA journal_mode=WAL")
        self.database.executescript("""
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence);
        """)
        self.database.commit()

    def directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise ValueError("Invalid run ID.")
        directory = self.root / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save(self, run: Run) -> None:
        run.updated_at = now()
        with self.database:
            self.database.execute(
                "INSERT OR REPLACE INTO runs(id, data) VALUES (?, ?)",
                (run.id, json.dumps(asdict(run))),
            )

    def get(self, run_id: str) -> Run:
        row = self.database.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Run not found: {run_id}")
        return Run(**json.loads(row[0]))

    def list(self) -> list[Run]:
        rows = self.database.execute("SELECT data FROM runs ORDER BY rowid DESC").fetchall()
        return [Run(**json.loads(row[0])) for row in rows]

    def event(self, run_id: str, kind: str, message: str, data: dict[str, Any]) -> dict[str, Any]:
        event = {"time": now(), "kind": kind, "message": message[:20000], "data": data}
        with self.database:
            self.database.execute(
                "INSERT INTO events(run_id, data) VALUES (?, ?)", (run_id, json.dumps(event))
            )
        return event

    def events(self, run_id: str, limit: int = 250) -> list[dict[str, Any]]:
        rows = self.database.execute(
            "SELECT data FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def add_note(self, run_id: str, note: str) -> Run:
        with self.lock(run_id):
            run = self.get(run_id)
            if run.status in ("completed", "needs_attention"):
                raise ValueError("This run has finished. Start a new task for further changes.")
            run.notes.append(note)
            if run.stage in ("checks", "reviewer"):
                run.stage = "worker"
            self.save(run)
            return run

    def artifact(self, run_id: str, name: str, text: str) -> None:
        if Path(name).name != name:
            raise ValueError("Artifact names must not contain directories.")
        target = self.directory(run_id) / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)

    @contextmanager
    def lock(self, run_id: str):
        import fcntl

        with (self.directory(run_id) / "run.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("This run is already executing in another process.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def close(self) -> None:
        self.database.close()
