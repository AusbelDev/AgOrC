import pytest

from orchestrator.config import Config, ConfigError, default_config, load_config, save_config


def test_provider_names_and_custom_models_round_trip(tmp_path):
    config = default_config()
    config.providers["work-google"] = config.providers.pop("antigravity")
    config.roles["worker"].provider = "work-google"
    config.roles["worker"].model = "my-custom-model"
    config.roles["worker"].instructions = "Follow the team's regression testing conventions."
    config.workflow.checks = [["python", "-m", "unittest"]]
    path = tmp_path / "work.toml"
    save_config(config, path)
    assert load_config(path).to_dict() == config.to_dict()


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("auth", "api", "only existing CLI logins"),
        ("backend", "unsupported-cli", "not implemented"),
        ("sandbox", "false", "boolean"),
    ],
)
def test_unsupported_provider_settings_fail_clearly(field, value, message):
    config = default_config()
    setattr(config.providers["codex"], field, value)
    with pytest.raises(ConfigError, match=message):
        config.validate()


def test_invalid_role_and_check_configuration():
    config = default_config()
    config.roles["worker"].provider = "nonexistent"
    with pytest.raises(ConfigError, match="unknown provider"):
        config.validate()
    config = default_config()
    config.workflow.checks = ["pytest"]
    with pytest.raises(ConfigError, match="argv"):
        config.validate()
    with pytest.raises(ConfigError, match="Unknown configuration"):
        Config.from_dict({**default_config().to_dict(), "typo": {}})


def legacy_config():
    data = default_config().to_dict()
    provider = data["providers"].pop("antigravity")
    provider.update(backend="gemini-cli", executable="gemini", models=["gemini-3.8-flash"])
    data["providers"]["work-google"] = provider
    data["roles"]["worker"].update(
        provider="work-google", model="gemini-3.8-flash", instructions="Keep my custom rules."
    )
    return data


def test_legacy_configuration_migrates_with_notice_and_preserves_preferences():
    data = legacy_config()
    data["workflow"]["checks"] = [["pytest", "-q"]]
    with pytest.warns(UserWarning, match="Migrated Gemini CLI"):
        config = Config.from_dict(data)
    assert config.providers["work-google"].backend == "agy-cli"
    assert config.providers["work-google"].executable == "agy"
    assert config.roles["worker"].provider == "work-google"
    assert config.roles["worker"].model == "gemini-3.8-flash-medium"
    assert config.roles["worker"].instructions == "Keep my custom rules."
    assert config.workflow.checks == [["pytest", "-q"]]
    assert data["providers"]["work-google"]["backend"] == "gemini-cli"


def test_legacy_custom_models_are_preserved():
    data = legacy_config()
    data["roles"]["worker"]["model"] = "custom-model-id"
    with pytest.warns(UserWarning):
        config = Config.from_dict(data)
    assert config.roles["worker"].model == "custom-model-id"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_legacy_flash_effort_maps_to_matching_antigravity_slug(effort):
    data = legacy_config()
    data["roles"]["worker"]["reasoning"] = effort
    with pytest.warns(UserWarning):
        config = Config.from_dict(data)
    assert config.roles["worker"].model == f"gemini-3.8-flash-{effort}"


def test_legacy_custom_executable_requires_explicit_update():
    data = legacy_config()
    data["providers"]["work-google"]["executable"] = "my-custom-gemini-wrapper"
    with pytest.raises(ConfigError, match="update the custom Gemini executable"):
        Config.from_dict(data)


def test_claude_roles_round_trip_with_custom_provider_model_and_effort(tmp_path):
    config = default_config()
    config.providers["work-claude"] = config.providers.pop("claude")
    config.roles["worker"].provider = "work-claude"
    config.roles["worker"].model = "claude-custom-model"
    config.roles["worker"].reasoning = "max"
    path = tmp_path / "claude.toml"
    save_config(config, path)
    assert load_config(path).to_dict() == config.to_dict()
