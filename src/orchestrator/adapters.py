from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import EXECUTABLES, Provider, Role
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
    environment.update(NO_COLOR="1", CI="1")
    return environment


def executable(provider: Provider) -> str:
    command = provider.executable or EXECUTABLES[provider.backend]
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


def antigravity_command(request: AgentRequest) -> list[str]:
    argv = [
        executable(request.provider),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        request.role.model,
        "--mode",
        "accept-edits" if request.role_name == "worker" else "plan",
        "--print-timeout",
        f"{request.timeout}s",
    ]
    if request.role.reasoning != "default":
        argv += ["--effort", request.role.reasoning]
    if request.schema:
        argv += ["--json-schema", json.dumps(request.schema)]
    if request.provider.sandbox:
        argv += ["--sandbox"]
    return argv


class AntigravityBackend:
    async def run(self, request: AgentRequest, emit: EventSink) -> AgentResult:
        result = AgentResult("")
        completed = False
        status = ""
        error_detail = ""

        async def line(stream: str, text: str) -> None:
            nonlocal completed, status, error_detail
            if stream == "stderr":
                await emit("log", text, {})
                return
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                await emit("log", text, {})
                return
            if not isinstance(event, dict):
                raise BackendError("Antigravity emitted a malformed event object.")
            kind = event.get("event", "event")
            if kind == "init":
                result.session_id = event.get("conversation_id", "")
                details = event.get("init", {})
                if not isinstance(details, dict):
                    raise BackendError("Antigravity emitted malformed initialization data.")
                actual_model = details.get("model")
                if actual_model and actual_model != request.role.model:
                    raise BackendError(
                        f"Antigravity selected {actual_model!r} instead of "
                        f"{request.role.model!r}. Model fallback is not enabled."
                    )
                await emit("agent", "Antigravity session started", event)
            elif kind == "step_update":
                step = event.get("step_update", {})
                if not isinstance(step, dict):
                    raise BackendError("Antigravity emitted a malformed step update.")
                # Text deltas are progress only. The terminal result is the authoritative answer.
                message = step.get("text_delta") or step.get("tool_name") or step.get("step_type")
                await emit("agent", str(message or "step_update"), event)
            elif kind == "result":
                if completed:
                    raise BackendError("Antigravity emitted multiple results for one task stage.")
                details = event.get("result", {})
                if not isinstance(details, dict):
                    raise BackendError("Antigravity emitted a malformed terminal result.")
                completed = True
                status = details.get("status", "")
                result.session_id = details.get("conversation_id", result.session_id)
                result.usage = details.get("usage", {})
                response = details.get("response", "")
                if request.schema and "structured_output" in details:
                    response = json.dumps(details["structured_output"])
                if not isinstance(response, str):
                    raise BackendError("Antigravity's response must be text.")
                result.text = response
                error_detail = str(details.get("error", ""))
                await emit("agent", f"Antigravity finished: {status or 'unknown status'}", event)
            else:
                await emit("agent", str(kind), event)

        prompt = json.dumps({"event": "user", "message": {"content": request.prompt}}) + "\n"
        process = await run_process(
            antigravity_command(request),
            request.workspace,
            input_text=prompt,
            env=login_environment(),
            on_line=line,
            timeout=request.timeout,
        )
        if process.code != 0 or not completed or status != "SUCCESS" or not result.text.strip():
            detail = error_detail or process.stderr or "No successful terminal result received."
            lowered = detail.lower()
            if any(
                marker in lowered
                for marker in (
                    "authentication required",
                    "not authenticated",
                    "not logged in",
                    "login required",
                    "sign in",
                    "sign-in",
                    "no credentials",
                    "credentials expired",
                )
            ):
                raise BackendError(
                    "Antigravity requires a signed-in CLI session. Run `agy` in your terminal "
                    "to sign in, then resume this run or rerun doctor --live."
                )
            if "gemini_api_key" in lowered:
                raise BackendError(
                    "Antigravity is configured for API-key authentication. Remove modelProvider "
                    "from ~/.gemini/antigravity-cli/settings.json and sign in using `agy` "
                    "to use this login adapter."
                )
            raise BackendError(
                f"Antigravity failed (exit {process.code}, status {status}): {detail[-4000:]}"
            )
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
            "agy-cli": AntigravityBackend(),
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
