from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import ConfigError, load_config


def write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_and_missing_key_are_allowed(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml", env_path=tmp_path / "missing.env", environ={})

    assert config.llm.api_key is None
    assert config.llm.model == "gpt-4o-mini"
    assert config.work_dir == tmp_path.resolve()


def test_reads_yaml_ordinary_configuration(tmp_path: Path) -> None:
    config_path = write_yaml(
        tmp_path / "config.yaml",
        "llm:\n  base_url: https://yaml.example/v1\n  model: yaml-model\n  temperature: 0.2\n"
        "agent:\n  work_dir: workspace\n  max_iterations: 4\nsession:\n  dir: data/sessions\n",
    )

    config = load_config(config_path, env_path=tmp_path / "absent.env", environ={})

    assert config.llm.base_url == "https://yaml.example/v1"
    assert config.llm.model == "yaml-model"
    assert config.llm.temperature == 0.2
    assert config.work_dir == (tmp_path / "workspace").resolve()
    assert config.max_iterations == 4
    assert config.session_dir == (tmp_path / "data/sessions").resolve()


def test_dotenv_overrides_yaml_and_supports_all_agent_variables(tmp_path: Path) -> None:
    config_path = write_yaml(
        tmp_path / "config.yaml",
        "llm:\n  base_url: https://yaml.example/v1\n  model: yaml-model\n"
        "agent:\n  work_dir: yaml-dir\n",
    )
    env_path = tmp_path / ".env.test"
    env_path.write_text(
        "AGENT_API_KEY=dotenv-secret\nAGENT_BASE_URL=https://dotenv.example/v1\n"
        "AGENT_MODEL=dotenv-model\nAGENT_WORK_DIR=dotenv-dir\n",
        encoding="utf-8",
    )

    config = load_config(config_path, env_path=env_path, environ={})

    assert config.llm.api_key == "dotenv-secret"
    assert config.llm.base_url == "https://dotenv.example/v1"
    assert config.llm.model == "dotenv-model"
    assert config.work_dir == (tmp_path / "dotenv-dir").resolve()
    assert "dotenv-secret" not in repr(config.llm)


def test_process_environment_overrides_dotenv(tmp_path: Path) -> None:
    config_path = write_yaml(tmp_path / "config.yaml", "llm:\n  model: yaml-model\n")
    env_path = tmp_path / ".env.test"
    env_path.write_text("AGENT_MODEL=dotenv-model\nAGENT_API_KEY=dotenv-key\n", encoding="utf-8")

    config = load_config(
        config_path,
        env_path=env_path,
        environ={"AGENT_MODEL": "process-model", "AGENT_API_KEY": "process-key"},
    )

    assert config.llm.model == "process-model"
    assert config.llm.api_key == "process-key"


def test_yaml_api_key_is_ignored(tmp_path: Path) -> None:
    config_path = write_yaml(
        tmp_path / "config.yaml",
        "llm:\n  api_key: yaml-secret\n  model: local-model\n",
    )

    config = load_config(config_path, env_path=tmp_path / "absent.env", environ={})

    assert config.llm.api_key is None
    assert config.llm.model == "local-model"


def test_invalid_max_iterations_is_rejected(tmp_path: Path) -> None:
    config_path = write_yaml(tmp_path / "config.yaml", "agent:\n  max_iterations: 0\n")

    with pytest.raises(ConfigError, match="max_iterations"):
        load_config(config_path, env_path=tmp_path / "absent.env", environ={})


def test_zero_max_retries_is_allowed(tmp_path: Path) -> None:
    config_path = write_yaml(tmp_path / "config.yaml", "llm:\n  max_retries: 0\n")

    config = load_config(config_path, env_path=tmp_path / "absent.env", environ={})

    assert config.llm.max_retries == 0


def test_invalid_yaml_shape_is_rejected(tmp_path: Path) -> None:
    config_path = write_yaml(tmp_path / "config.yaml", "- not-a-mapping\n")

    with pytest.raises(ConfigError, match="mapping"):
        load_config(config_path, env_path=tmp_path / "absent.env", environ={})
