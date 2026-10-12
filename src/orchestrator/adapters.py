from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import Provider, Role
from .processes import run_process

EventSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class BackendError(RuntimeError):
    pass


@dataclass
class AgentRequest:
    role_name: str
    role: Role
    provider: Provider
    workspace: Path
    prompt: str
    schema: dict[str, Any] | None = None
    timeout: float = 900


@dataclass
class AgentResult:
    text: str
    session_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class AgentBackend(Protocol):
    """API and Claude adapters can implement this without changing the workflow."""

    async def run(self, request: AgentRequest, emit: EventSink) -> AgentResult: ...


def login_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GEMINI_BASE_URL",
        "GEMINI_API_BASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUD_SHELL",
    ):
        environment.pop(name, None)
    environment.update(NO_COLOR="1", CI="1", GEMINI_CLI_NO_RELAUNCH="true")
    return environment


def executable(provider: Provider) -> str:
    command = (
        provider.executable or {"codex-cli": "codex", "gemini-cli": "gemini"}[provider.backend]
    )
    resolved = shutil.which(command)
    if not resolved:
        raise BackendError(f"Cannot find {command!r}. Install it and log in from your terminal.")
    return resolved


def codex_command(request: AgentRequest, schema_path: Path | None) -> list[str]:
    argv = [
        executable(request.provider),
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--color",
        "never",
        "--model",
        request.role.model,
        "--sandbox",
        "workspace-write" if request.role_name == "worker" else "read-only",
        "-c",
        'approval_policy="never"',
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'model_provider="openai"',
    ]
    if request.role.reasoning != "default":
        argv += ["-c", f"model_reasoning_effort={json.dumps(request.role.reasoning)}"]
    if schema_path:
        argv += ["--output-schema", str(schema_path)]
    return [*argv, "-"]


class CodexBackend:
    async def run(self, request: AgentRequest, emit: EventSink) -> AgentResult:
        result = AgentResult("")
        errors: list[str] = []
        completed = False

        async def line(stream: str, text: str) -> None:
            nonlocal completed
            if stream == "stderr":
                await emit("log", text, {})
                return
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                await emit("log", text, {})
                return
            kind = event.get("type", "event")
            if kind == "thread.started":
                result.session_id = event.get("thread_id", "")
            elif kind == "turn.completed":
                completed = True
                result.usage = event.get("usage", {})
            elif kind in ("turn.failed", "error"):
                error = event.get("error", event.get("message", "Codex failed"))
                errors.append(error if isinstance(error, str) else json.dumps(error))
            item = event.get("item", {})
            if kind == "item.completed" and item.get("type") == "agent_message":
                result.text = item.get("text", "")
            description = item.get("command") or item.get("text") or kind
            await emit("agent", str(description), event)

        with tempfile.TemporaryDirectory(prefix="orchestrator-codex-") as directory:
            schema_path = None
            if request.schema:
                schema_path = Path(directory) / "response.schema.json"
                schema_path.write_text(json.dumps(request.schema), encoding="utf-8")
            process = await run_process(
                codex_command(request, schema_path),
                request.workspace,
                input_text=request.prompt,
                env=login_environment(),
                on_line=line,
                timeout=request.timeout,
            )
        if process.code != 0 or not completed or not result.text.strip():
            detail = "\n".join(errors) or process.stderr or "No completed response received."
            raise BackendError(f"Codex failed (exit {process.code}): {detail[-4000:]}")
        return result


READ_TOOLS = ["read_file", "read_many_files", "list_directory", "glob", "grep_search"]


def gemini_policy(worker: bool) -> str:
    # Only repository read tools and, for workers, edit tools are enabled.
    # No shell, MCP actions, network tools, or nested agents in this adapter.
    allowed = READ_TOOLS + (["write_file", "replace"] if worker else [])
    return (
        '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 998\n\n'
        f'[[rule]]\ntoolName = {json.dumps(allowed)}\ndecision = "allow"\npriority = 999\n'
    )


def gemini_settings(request: AgentRequest) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "security": {"auth": {"selectedType": "oauth-personal", "enforcedType": "oauth-personal"}},
        "general": {"enableAutoUpdate": False},
    }
    if request.role.reasoning != "default":
        settings["modelConfigs"] = {
            "overrides": [
                {
                    "match": {"model": request.role.model},
                    "modelConfig": {
                        "generateContentConfig": {
                            "thinkingConfig": {"thinkingLevel": request.role.reasoning.upper()},
                        }
                    },
                }
            ]
        }
    return settings


def gemini_system_settings_path() -> Path:
    configured = os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path("/Library/Application Support/GeminiCli/settings.json")
    return Path("/etc/gemini-cli/settings.json")


def merge_settings(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_settings(merged[key], value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = [*merged[key], *value]
        else:
            merged[key] = value
    return merged


def effective_gemini_settings(request: AgentRequest) -> dict[str, Any]:
    path = gemini_system_settings_path()
    base: dict[str, Any] = {}
    if path.exists():
        try:
            base = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(base, dict):
                raise ValueError("Expected a JSON object")
        except (OSError, ValueError) as exc:
            raise BackendError(
                f"Cannot preserve Gemini system settings from {path}: {exc}"
            ) from exc
    enforced = base.get("security", {}).get("auth", {}).get("enforcedType")
    if enforced and enforced != "oauth-personal":
        raise BackendError(
            f"Gemini system settings enforce {enforced!r}; this provider requires Google login."
        )
    return merge_settings(base, gemini_settings(request))


class GeminiBackend:
    async def run(self, request: AgentRequest, emit: EventSink) -> AgentResult:
        result = AgentResult("")
        completed = False
        failed = False
        error_detail = ""

        async def line(stream: str, text: str) -> None:
            nonlocal completed, failed, error_detail
            if stream == "stderr":
                await emit("log", text, {})
                return
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                await emit("log", text, {})
                return
            kind = event.get("type", "event")
            if kind == "init":
                result.session_id = event.get("session_id", "")
                actual_model = event.get("model")
                if actual_model and actual_model != request.role.model:
                    raise BackendError(
                        f"Gemini selected {actual_model!r} instead of {request.role.model!r}. "
                        "Model fallback is not enabled."
                    )
            elif kind == "message" and event.get("role") == "assistant":
                content = event.get("content", "")
                result.text = result.text + content if event.get("delta", True) else content
            elif kind == "result":
                completed = True
                failed = event.get("status") != "success"
                result.usage = event.get("stats", {})
                error_detail = str(event.get("error", ""))
            await emit("agent", str(event.get("content") or event.get("tool_name") or kind), event)

        with tempfile.TemporaryDirectory(prefix="orchestrator-gemini-") as directory:
            policy_path = Path(directory) / "policy.toml"
            policy_path.write_text(gemini_policy(request.role_name == "worker"), encoding="utf-8")
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(
                json.dumps(effective_gemini_settings(request)), encoding="utf-8"
            )
            environment = login_environment()
            environment["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(settings_path)
            environment.setdefault(
                "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
                str(gemini_system_settings_path().with_name("system-defaults.json")),
            )
            environment["NO_BROWSER"] = "true"
            argv = [
                executable(request.provider),
                "--model",
                request.role.model,
                "--output-format",
                "stream-json",
                "--skip-trust",
                "--approval-mode",
                "auto_edit",
                "--admin-policy",
                str(policy_path),
                "--prompt",
                "Follow the task supplied on stdin.",
            ]
            if request.provider.sandbox:
                argv += ["--sandbox"]
            prompt = request.prompt
            if request.schema:
                prompt += "\nReturn ONLY JSON matching this schema:\n" + json.dumps(request.schema)
            process = await run_process(
                argv,
                request.workspace,
                input_text=prompt,
                env=environment,
                on_line=line,
                timeout=request.timeout,
            )
        if process.code != 0 or not completed or failed or not result.text.strip():
            detail = error_detail or process.stderr or "No successful response received."
            if process.code == 41 or "Manual authorization is required" in detail:
                raise BackendError(
                    "Gemini requires an interactive Google login. Run `gemini` in your terminal, "
                    "choose Sign in with Google, then resume this run or rerun doctor --live."
                )
            raise BackendError(f"Gemini failed (exit {process.code}): {detail[-4000:]}")
        return result


class DemoBackend:
    """An offline end-to-end fixture; never contacts a model or edits the user's repository."""

    async def run(self, request: AgentRequest, emit: EventSink) -> AgentResult:
        for message in {
            "planner": ["Inspecting the demo function and tests", "Preparing implementation steps"],
            "worker": ["Fixing the empty-list case", "Updating the demo implementation"],
            "reviewer": ["Inspecting the diff", "Checking acceptance criteria and test results"],
        }[request.role_name]:
            await emit("agent", message, {"demo": True})
            await asyncio.sleep(0.15)
        if request.role_name == "planner":
            text = json.dumps(
                {
                    "summary": "Return zero when calculating the average of an empty list.",
                    "steps": ["Handle empty input", "Run the existing regression tests"],
                    "acceptance_criteria": [
                        "Empty input returns 0",
                        "Nonempty averages are unchanged",
                    ],
                }
            )
        elif request.role_name == "worker":
            (request.workspace / "average.py").write_text(
                "def average(values):\n    return sum(values) / len(values) if values else 0\n",
                encoding="utf-8",
            )
            text = "Added the empty-input guard to average()."
        else:
            text = json.dumps(
                {
                    "accepted": True,
                    "summary": "The guard fixes the bug and preserves other behavior.",
                    "findings": [],
                }
            )
        return AgentResult(text, session_id="offline-demo")


class BackendRegistry:
    def __init__(self, *, demo: bool = False) -> None:
        self.backends: dict[str, AgentBackend] = {
            "codex-cli": CodexBackend(),
            "gemini-cli": GeminiBackend(),
        }
        self.demo = demo

    def register(self, name: str, backend: AgentBackend) -> None:
        self.backends[name] = backend

    def get(self, provider: Provider) -> AgentBackend:
        if self.demo:
            return DemoBackend()
        try:
            return self.backends[provider.backend]
        except KeyError as exc:
            raise BackendError(f"No adapter registered for {provider.backend!r}.") from exc
