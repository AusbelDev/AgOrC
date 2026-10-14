from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import shutil
import sys
from pathlib import Path

from .config import (
    EXECUTABLES,
    ROLES,
    ConfigError,
    default_config,
    default_state_dir,
    load_config,
    save_config,
)
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
    diagnostics.add_argument(
        "--skills",
        action="store_true",
        help="With --live, verify reading a temporary skill and its reference file",
    )
    terminal = commands.add_parser("tui", help="Open the terminal interface (default)")
    terminal.add_argument("--repo", type=Path, default=Path.cwd())
    skills = commands.add_parser("skills", help="Inspect skills available to each role")
    skills.add_argument("--repo", type=Path, default=Path.cwd())
    skills.add_argument("--role", choices=(*ROLES, "all"), default="all")
    skills.add_argument("--json", action="store_true", help="Print the skill catalog as JSON")
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


async def doctor(config, *, live: bool = False, test_skills: bool = False) -> int:
    missing = False
    active_providers = {role.provider for role in config.roles.values()}
    for name, provider in config.providers.items():
        if name not in active_providers:
            print(f"{name}: unused by current roles (not checked)")
            continue
        command = provider.executable or EXECUTABLES[provider.backend]
        location = shutil.which(command)
        if not location:
            print(f"{name}: {command} is not installed")
            missing = True
            continue
        result = await run_process([location, "--version"], Path.cwd(), timeout=20)
        print(f"{name}: {result.stdout or result.stderr}")
        if result.code:
            missing = True
        for role_name, role in config.roles.items():
            if role.provider != name:
                continue
            if provider.backend == "agy-cli":
                mcp = "existing Antigravity MCP configuration and grants"
            elif role.use_mcp:
                mcp = "enabled using existing CLI configuration and permissions"
                if provider.backend == "claude-cli" and role.mcp_tools:
                    mcp += "; additional allowed tools: " + ", ".join(role.mcp_tools)
            else:
                mcp = (
                    "user configuration ignored" if provider.backend == "codex-cli" else "disabled"
                )
            print(f"  {role_name} MCP: {mcp} (connection not tested)")
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
        elif provider.backend == "claude-cli":
            from .adapters import BackendError, claude_login_status

            try:
                method = await claude_login_status(provider, Path.cwd())
                print(f"  Logged in using {method}.")
            except BackendError as exc:
                print(f"  {exc}")
                missing = True
        else:
            print(
                "  Antigravity login is managed by agy; sign in with `agy` if needed. "
                "Use doctor --live to verify access."
            )
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
            prompt = 'Do not use any tools. Return only the JSON object {"ok":true}.'
            expected = {"ok": True}
            schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            }
            if test_skills:
                import uuid

                from .skills import read_skill, skill_prompt, stage_skills

                fixture = workspace / "skill-fixture"
                fixture.mkdir()
                references = fixture / "references"
                references.mkdir()
                token = uuid.uuid4().hex
                (references / "marker.txt").write_text(token, encoding="utf-8")
                (fixture / "SKILL.md").write_text(
                    "---\nname: local-skill-check\ndescription: Use for skill diagnostics.\n---\n"
                    "Read references/marker.txt relative to this skill's directory. Return only "
                    "a JSON object with the token field containing that file's exact contents.\n",
                    encoding="utf-8",
                )
                configured = type(config).from_dict(config.to_dict())
                for role in configured.roles.values():
                    role.required_skills = []
                skill = read_skill(fixture / "SKILL.md", "diagnostic")
                catalog = stage_skills(
                    {role: [skill] for role in ROLES}, workspace, "skill-snapshot", configured
                )
                prompt = (
                    "Use only the supplied local-skill-check skill and its reference. "
                    "Do not use other skills or edit any files. Return the requested JSON.\n"
                    + skill_prompt(workspace, catalog["planner"])
                )
                expected = {"token": token}
                schema = {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"token": {"type": "string"}},
                    "required": ["token"],
                }
            for role in config.roles.values():
                identity = (
                    role.provider, role.model, role.reasoning, role.use_mcp, tuple(role.mcp_tools)
                )
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
                        prompt,
                        schema,
                        timeout=90,
                    ),
                    ignore,
                )
                response = result.text.strip()
                if response.startswith("```") and response.endswith("```"):
                    response = "\n".join(response.splitlines()[1:-1])
                if json.loads(response) != expected:
                    raise ValueError(f"Unexpected live response from {role.provider}.")
                print(
                    "  Skill instructions and reference file read succeeded."
                    if test_skills
                    else "  Login and model request succeeded.",
                    flush=True,
                )
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
        if args.command == "skills":
            from .skills import discover_skills

            catalogs = discover_skills(config, args.repo.expanduser().resolve())
            selected = ROLES if args.role == "all" else (args.role,)
            if args.json:
                print(
                    json.dumps(
                        {
                            role: [
                                {
                                    "name": skill.name,
                                    "description": skill.description,
                                    "source": str(skill.source),
                                    "scope": skill.scope,
                                    "required": skill.name in config.roles[role].required_skills,
                                }
                                for skill in catalogs[role]
                            ]
                            for role in selected
                        },
                        indent=2,
                    )
                )
            else:
                for role in selected:
                    backend = config.providers[config.roles[role].provider].backend
                    print(f"{role} ({backend})")
                    for skill in catalogs[role]:
                        loading = (
                            "required"
                            if skill.name in config.roles[role].required_skills
                            else "when relevant"
                        )
                        print(f"  {skill.name} [{loading}] — {skill.description}")
                        print(f"    {skill.source}")
                    if not catalogs[role]:
                        print("  No skills available.")
            return
        if args.command == "doctor":
            if args.skills and not args.live:
                raise ValueError("doctor --skills requires --live; it makes a model request.")
            code = asyncio.run(doctor(config, live=args.live, test_skills=args.skills))
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
