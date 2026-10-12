from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

ROLES = ("planner", "worker", "reviewer")
BACKENDS = ("codex-cli", "gemini-cli")
EFFORTS = {
    "codex-cli": ("default", "none", "low", "medium", "high", "xhigh", "max"),
    "gemini-cli": ("default", "minimal", "low", "medium", "high"),
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


@dataclass
class Role:
    provider: str
    model: str
    reasoning: str = "default"
    instructions: str = ""


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
                    "Available: codex-cli, gemini-cli. API and Claude adapters are planned."
                )
            if provider.auth != "login":
                raise ConfigError(f"Provider {name}: only existing CLI logins are supported yet.")
            if not isinstance(provider.executable, str):
                raise ConfigError(f"Provider {name}: executable must be a path or command name.")
            if not isinstance(provider.sandbox, bool):
                raise ConfigError(f"Provider {name}: sandbox must be a boolean.")
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
            unexpected = set(data) - {"providers", "roles", "workflow"}
            if unexpected:
                raise ConfigError(f"Unknown configuration sections: {', '.join(unexpected)}")
            config = cls(
                providers={key: Provider(**value) for key, value in data["providers"].items()},
                roles={key: Role(**value) for key, value in data["roles"].items()},
                workflow=Workflow(**data.get("workflow", {})),
            )
            return config.validate()
        except (TypeError, KeyError, AttributeError) as exc:
            raise ConfigError(f"Invalid configuration: {exc}") from exc


def default_config() -> Config:
    return Config(
        providers={
            "codex": Provider("codex-cli", executable="codex", models=["gpt-6.1-sol"]),
            "gemini": Provider("gemini-cli", executable="gemini", models=["gemini-3.8-flash"]),
        },
        roles={
            "planner": Role("codex", "gpt-6.1-sol", "high"),
            "worker": Role("gemini", "gemini-3.8-flash", "medium"),
            "reviewer": Role("codex", "gpt-6.1-sol", "high"),
        },
    )


def load_config(path: Path) -> Config:
    if not path.exists():
        return default_config()
    try:
        with path.open("rb") as handle:
            return Config.from_dict(tomllib.load(handle))
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
