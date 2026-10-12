from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from .config import ConfigError, default_config, default_state_dir, load_config, save_config
from .engine import Engine, create_demo, report
from .processes import run_process
from .store import Store


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Plan, implement, and review code with CLI logins."
    )
    result.add_argument("--config", type=Path, default=Path("orchestrator.local.toml"))
    result.add_argument("--state-dir", type=Path, default=default_state_dir())
    commands = result.add_subparsers(dest="command")
    commands.add_parser(
        "init", help="Write a default configuration without overwriting an existing file"
    )
    diagnostics = commands.add_parser(
        "doctor", help="Check installed CLIs and local login indicators"
    )
    diagnostics.add_argument(
        "--live",
        action="store_true",
        help="Also make a minimal model request per distinct role configuration (uses quota)",
    )
    terminal = commands.add_parser("tui", help="Open the terminal interface (default)")
    terminal.add_argument("--repo", type=Path, default=Path.cwd())
    run = commands.add_parser("run", help="Run a task without the TUI")
    run.add_argument("task", help="Task description, or - to read from stdin")
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.add_argument(
        "--check", action="append", help="Verification command, split into argv without a shell"
    )
    commands.add_parser(
        "demo", help="Run an offline bug-fixing demo in an isolated sample repository"
    )
    commands.add_parser("history", help="List saved runs")
    show = commands.add_parser("show", help="Show a saved run's report")
    show.add_argument("run_id")
    resume = commands.add_parser("resume", help="Resume a paused, interrupted, or failed run")
    resume.add_argument("run_id")
    resume.add_argument("--note", help="Additional instructions applied before resuming")
    return result


async def doctor(config, *, live: bool = False) -> int:
    missing = False
    for name, provider in config.providers.items():
        command = provider.executable or ("codex" if provider.backend == "codex-cli" else "gemini")
        location = shutil.which(command)
        if not location:
            print(f"{name}: {command} is not installed")
            missing = True
            continue
        result = await run_process([location, "--version"], Path.cwd(), timeout=20)
        print(f"{name}: {result.stdout or result.stderr}")
        if result.code:
            missing = True
        if provider.backend == "codex-cli":
            status = await run_process([location, "login", "status"], Path.cwd(), timeout=20)
            login = status.stdout + status.stderr
            print(
                "  "
                + next(
                    (line for line in login.splitlines() if "Logged in" in line),
                    "No login confirmed; run codex login.",
                )
            )
            if status.code or "ChatGPT" not in login:
                missing = True
                print("  A ChatGPT login is required for this adapter.")
        else:
            home = Path(os.environ.get("GEMINI_CLI_HOME", Path.home() / ".gemini"))
            cached = (home / "oauth_creds.json").exists()
            print(
                "  Cached Google login found (not validated online)."
                if cached
                else "  No cached Google login found; run gemini and choose Sign in with Google."
            )
            missing |= not cached
    print(
        "Model IDs are user configured; availability is validated "
        "by the provider when a stage runs."
    )
    if live and not missing:
        import tempfile

        from . import git
        from .adapters import AgentRequest, BackendRegistry

        registry = BackendRegistry()
        checked = set()
        with tempfile.TemporaryDirectory(prefix="orchestrator-login-check-") as temporary:
            workspace = Path(temporary)
            await git.git(workspace, "init", "-b", "main")
            for role in config.roles.values():
                identity = (role.provider, role.model, role.reasoning)
                if identity in checked:
                    continue
                checked.add(identity)
                provider = config.providers[role.provider]
                print(f"Checking {role.provider} / {role.model} / {role.reasoning}…", flush=True)

                async def ignore(*args):
                    pass

                result = await registry.get(provider).run(
                    AgentRequest(
                        "planner",
                        role,
                        provider,
                        workspace,
                        'Do not use any tools. Return only the JSON object {"ok":true}.',
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                        },
                        timeout=90,
                    ),
                    ignore,
                )
                response = result.text.strip()
                if response.startswith("```") and response.endswith("```"):
                    response = "\n".join(response.splitlines()[1:-1])
                if json.loads(response) != {"ok": True}:
                    raise ValueError(f"Unexpected live response from {role.provider}.")
                print("  Login and model request succeeded.", flush=True)
    return 1 if missing else 0


async def run_command(args: argparse.Namespace, store: Store, config) -> int:
    async def listener(run, event):
        print(f"[{run.stage}] {event['message']}", flush=True)

    engine = Engine(store, listener=listener)
    if args.command == "demo":
        run = await create_demo(engine, config)
    elif args.command == "resume":
        run = store.get(args.run_id)
        if args.note:
            run = store.add_note(run.id, args.note)
    else:
        if args.check is not None:
            config.workflow.checks = [shlex.split(command) for command in args.check]
        task = sys.stdin.read() if args.task == "-" else args.task
        run = await engine.create(task, args.repo, config)
    result = await engine.execute(run.id)
    print(f"\nRun: {result.id}\nStatus: {result.status}\nWorkspace: {result.worktree}")
    print(f"Report: {store.directory(result.id) / 'report.md'}")
    return 0 if result.status == "completed" else 2


def main() -> None:
    args = parser().parse_args()
    store = None
    try:
        if args.command == "init":
            if args.config.exists():
                raise ValueError(f"Configuration already exists: {args.config}")
            save_config(default_config(), args.config)
            print(f"Created {args.config}")
            return
        config = load_config(args.config)
        if args.command == "doctor":
            code = asyncio.run(doctor(config, live=args.live))
        else:
            store = Store(args.state_dir)
            if args.command == "history":
                for run in store.list():
                    print(f"{run.id}  {run.status:16}  {run.task[:90]}")
                code = 0
            elif args.command == "show":
                print(report(store.get(args.run_id)))
                code = 0
            elif args.command in ("run", "demo", "resume"):
                code = asyncio.run(run_command(args, store, config))
            else:
                from .tui import OrchestratorApp

                OrchestratorApp(config, args.config, store, getattr(args, "repo", Path.cwd())).run()
                code = 0
        raise SystemExit(code)
    except (ConfigError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
