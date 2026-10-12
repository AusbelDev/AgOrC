import pytest

from orchestrator.config import Config, ConfigError, default_config, load_config, save_config


def test_provider_names_and_custom_models_round_trip(tmp_path):
    config = default_config()
    config.providers["work-google"] = config.providers.pop("gemini")
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
        ("backend", "claude-cli", "not implemented"),
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
