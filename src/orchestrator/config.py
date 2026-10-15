from __future__ import annotations

import os
import re
import warnings
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from .compat import tomllib

ROLES = ("planner", "worker", "reviewer")
BACKENDS = ("codex-cli", "agy-cli", "claude-cli")
EXECUTABLES = {"codex-cli": "codex", "agy-cli": "agy", "claude-cli": "claude"}
EFFORTS = {
    "codex-cli": ("default", "none", "low", "medium", "high", "xhigh", "max"),
    "agy-cli": ("default", "low", "medium", "high", "xhigh", "max"),
    "claude-cli": ("default", "low", "medium", "high", "xhigh", "max", "ultracode"),
}


class ConfigError(ValueError):
    pass


@dataclass
class Provider:
    backend: str
    auth: str = "login"
    executable: str = ""
    models: list[str] = field(default_factory=list)
    sandbox: bool = False
    codex_isolation: str = "sandbox"


@dataclass
class Role:
    provider: str
    model: str
    reasoning: str = "default"
    instructions: str = ""
    skills: list[str] = field(default_factory=lambda: ["*"])
    required_skills: list[str] = field(default_factory=list)
    use_mcp: bool = False
    mcp_tools: list[str] = field(default_factory=list)


@dataclass
class SkillSettings:
    inherit_global: bool = True
    discover_project: bool = True
    paths: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    max_revision_rounds: int = 2
    stage_timeout_seconds: int = 900
    run_timeout_seconds: int = 3600
    checks: list[list[str]] = field(default_factory=list)


@dataclass
class Config:
    providers: dict[str, Provider]
    roles: dict[str, Role]
    workflow: Workflow = field(default_factory=Workflow)
    skills: SkillSettings = field(default_factory=SkillSettings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> Config:
        if not self.providers:
            raise ConfigError("Configure at least one provider.")
        for name, provider in self.providers.items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigError("Provider names must be nonempty strings.")
            if provider.backend not in BACKENDS:
                raise ConfigError(
                    f"Provider {name}: backend {provider.backend!r} is not implemented. "
                    f"Available: {', '.join(BACKENDS)}. Direct API adapters are planned."
                )
            auth_methods = ("login", "api") if provider.backend == "codex-cli" else ("login",)
            if provider.auth not in auth_methods:
                raise ConfigError(
                    f"Provider {name}: {provider.backend} supports auth = "
                    + " or ".join(repr(method) for method in auth_methods)
                    + ". Codex API keys must be configured through the Codex CLI."
                )
            if not isinstance(provider.executable, str):
                raise ConfigError(f"Provider {name}: executable must be a path or command name.")
            if not isinstance(provider.sandbox, bool):
                raise ConfigError(f"Provider {name}: sandbox must be a boolean.")
            if provider.codex_isolation not in ("sandbox", "container"):
                raise ConfigError(
                    f"Provider {name}: codex_isolation must be 'sandbox' or 'container'."
                )
            if provider.codex_isolation == "container" and provider.backend != "codex-cli":
                raise ConfigError(f"Provider {name}: codex_isolation applies only to Codex.")
            if not isinstance(provider.models, list) or not all(
                isinstance(model, str) and model.strip() for model in provider.models
            ):
                raise ConfigError(f"Provider {name}: models must be an array of model IDs.")
        if set(self.roles) != set(ROLES):
            raise ConfigError("Configure exactly three roles: planner, worker, reviewer.")
        for name, role in self.roles.items():
            if role.provider not in self.providers:
                raise ConfigError(f"Role {name}: unknown provider {role.provider!r}.")
            if (
                not isinstance(role.model, str)
                or not role.model.strip()
                or role.model.startswith("-")
            ):
                raise ConfigError(f"Role {name}: enter a valid model ID.")
            backend = self.providers[role.provider].backend
            if role.reasoning not in EFFORTS[backend]:
                raise ConfigError(f"Role {name}: {backend} does not support {role.reasoning!r}.")
            if not isinstance(role.instructions, str):
                raise ConfigError(f"Role {name}: instructions must be text.")
            if not isinstance(role.use_mcp, bool):
                raise ConfigError(f"Role {name}: use_mcp must be a boolean.")
            if not isinstance(role.mcp_tools, list) or not all(
                isinstance(tool, str)
                and re.fullmatch(r"mcp__[\w-]+__[\w.*-]+", tool)
                for tool in role.mcp_tools
            ):
                raise ConfigError(
                    f"Role {name}: mcp_tools must be an array of MCP tool names or patterns, "
                    "for example mcp__jira__get_issue or mcp__jira__*."
                )
            if role.mcp_tools and backend != "claude-cli":
                raise ConfigError(
                    f"Role {name}: mcp_tools applies only to Claude Code. "
                    "Configure MCP tool permissions in your selected CLI instead."
                )
            for setting in ("skills", "required_skills"):
                values = getattr(role, setting)
                if not isinstance(values, list) or not all(
                    isinstance(value, str) and value.strip() for value in values
                ):
                    raise ConfigError(f"Role {name}: {setting} must be an array of skill names.")
            if "*" in role.required_skills:
                raise ConfigError(f"Role {name}: required_skills must name individual skills.")
        for name in ("inherit_global", "discover_project"):
            if type(getattr(self.skills, name)) is not bool:
                raise ConfigError(f"skills.{name} must be a boolean.")
        if not isinstance(self.skills.paths, list) or not all(
            isinstance(path, str) and path.strip() for path in self.skills.paths
        ):
            raise ConfigError("skills.paths must be an array of local paths.")
        workflow = self.workflow
        for name in ("max_revision_rounds", "stage_timeout_seconds", "run_timeout_seconds"):
            value = getattr(workflow, name)
            minimum = 0 if name == "max_revision_rounds" else 1
            if type(value) is not int or value < minimum:
                raise ConfigError(f"workflow.{name} must be an integer >= {minimum}.")
        if not isinstance(workflow.checks, list):
            raise ConfigError("workflow.checks must be an array of argv arrays.")
        for command in workflow.checks:
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(arg, str) and arg for arg in command)
            ):
                raise ConfigError("Each check must be a nonempty argv array of strings.")
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        try:
            data, migrated = migrate_gemini_config(data)
            if migrated:
                warnings.warn(
                    "Migrated Gemini CLI providers to Antigravity (agy): "
                    + ", ".join(migrated)
                    + ". Save settings to persist the updated configuration.",
                    stacklevel=2,
                )
            unexpected = set(data) - {"providers", "roles", "workflow", "skills"}
            if unexpected:
                raise ConfigError(f"Unknown configuration sections: {', '.join(unexpected)}")
            config = cls(
                providers={key: Provider(**value) for key, value in data["providers"].items()},
                roles={key: Role(**value) for key, value in data["roles"].items()},
                workflow=Workflow(**data.get("workflow", {})),
                skills=SkillSettings(**data.get("skills", {})),
            )
            return config.validate()
        except (TypeError, KeyError, AttributeError) as exc:
            raise ConfigError(f"Invalid configuration: {exc}") from exc


def default_config() -> Config:
    return Config(
        providers={
            "codex": Provider("codex-cli", executable="codex", models=["gpt-6.1-sol"]),
            "antigravity": Provider(
                "agy-cli", executable="agy", models=["gemini-3.8-flash-medium"]
            ),
            "claude": Provider(
                "claude-cli", executable="claude", models=["sonnet", "opus", "haiku"]
            ),
        },
        roles={
            "planner": Role("codex", "gpt-6.1-sol", "high"),
            "worker": Role("antigravity", "gemini-3.8-flash-medium", "medium"),
            "reviewer": Role("codex", "gpt-6.1-sol", "high"),
        },
    )


def migrate_gemini_config(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Keep named providers, instructions, and custom IDs while replacing the retired backend."""
    copied = deepcopy(data)
    migrated = []
    for name, provider in copied.get("providers", {}).items():
        if provider.get("backend") != "gemini-cli":
            continue
        provider["backend"] = "agy-cli"
        command = provider.get("executable", "")
        if command in ("", "gemini") or Path(command).name == "gemini":
            provider["executable"] = "agy"
        else:
            raise ConfigError(
                f"Provider {name}: update the custom Gemini executable {command!r} to your "
                "Antigravity executable before migrating."
            )
        provider["models"] = [
            "gemini-3.8-flash-medium" if model == "gemini-3.8-flash" else model
            for model in provider.get("models", [])
        ]
        for role in copied.get("roles", {}).values():
            if role.get("provider") == name and role.get("model") == "gemini-3.8-flash":
                effort = role.get("reasoning", "default")
                if effort not in ("default", "low", "medium", "high"):
                    raise ConfigError(
                        f"Provider {name}: choose an Antigravity model slug and supported "
                        f"reasoning level instead of legacy effort {effort!r}."
                    )
                role["model"] = f"gemini-3.8-flash-{'medium' if effort == 'default' else effort}"
        migrated.append(name)
    return copied, migrated


def load_config(path: Path) -> Config:
    if not path.exists():
        return default_config()
    try:
        with path.open("rb") as handle:
            config = Config.from_dict(tomllib.load(handle))
            config.skills.paths = [
                str((path.parent / Path(source).expanduser()).resolve())
                for source in config.skills.paths
            ]
            return config
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


def save_config(config: Config, path: Path) -> None:
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(tomli_w.dumps(config.to_dict()), encoding="utf-8")
    temporary.replace(path)


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "orchestrator"
