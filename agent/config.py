"""Configuration loading for the teaching Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml
from dotenv import dotenv_values


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str | None = field(default=None, repr=False, compare=False)
    temperature: float = 0.7
    timeout: float = 60.0
    max_retries: int = 2
    streaming: bool = True


@dataclass(frozen=True)
class AgentConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    work_dir: Path = field(default_factory=Path.cwd)
    max_iterations: int = 10
    session_dir: Path = Path(".sessions")
    session_auto_save: bool = True
    system_prompt: str = ""
    shell_timeout: int = 10
    search_max_results: int = 100

    @property
    def streaming(self) -> bool:
        return self.llm.streaming


class ConfigError(ValueError):
    """Raised when the project configuration is invalid."""


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as config_file:
            data = yaml.safe_load(config_file) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Unable to read configuration file: {path}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration file must contain a YAML mapping")
    return data


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Configuration section '{name}' must be a mapping")
    return value


def _number(value: object, name: str, *, integer: bool = False, minimum: int | float = 0) -> int | float:
    if isinstance(value, bool):
        raise ConfigError(f"'{name}' must be at least {minimum}")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be at least {minimum}") from exc
    if parsed < minimum:
        raise ConfigError(f"'{name}' must be at least {minimum}")
    return parsed


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"'{name}' must be a boolean")


def _resolve_path(value: object, base_dir: Path, name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ConfigError(f"'{name}' must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_config(
    config_path: str | os.PathLike | None = None,
    env_path: str | os.PathLike | None = None,
    environ: Mapping[str, str] | None = None,
) -> AgentConfig:
    """Load YAML settings, dotenv overrides, then process environment overrides."""
    config_file = Path(config_path or "config.yaml").expanduser().resolve()
    dotenv_file = Path(env_path).expanduser() if env_path is not None else config_file.parent / ".env"
    if not dotenv_file.is_absolute():
        dotenv_file = config_file.parent / dotenv_file
    dotenv_file = dotenv_file.resolve()

    yaml_data = _read_yaml(config_file)
    llm_data = _section(yaml_data, "llm")
    agent_data = _section(yaml_data, "agent")
    session_data = _section(yaml_data, "session")
    tools_data = _section(yaml_data, "tools")

    try:
        dotenv_data = dotenv_values(dotenv_file, encoding="utf-8") if dotenv_file.exists() else {}
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Unable to read dotenv file: {dotenv_file}") from exc

    process_env = os.environ if environ is None else environ

    def setting(env_name: str, yaml_value: object, default: object) -> object:
        if env_name in dotenv_data and dotenv_data[env_name] is not None:
            value = dotenv_data[env_name]
        else:
            value = yaml_value if yaml_value is not None else default
        if env_name in process_env:
            value = process_env[env_name]
        return value

    base_dir = config_file.parent
    llm_config = LLMConfig(
        base_url=str(setting("AGENT_BASE_URL", llm_data.get("base_url"), LLMConfig.base_url)),
        model=str(setting("AGENT_MODEL", llm_data.get("model"), LLMConfig.model)),
        api_key=str(setting("AGENT_API_KEY", None, "")) or None,
        temperature=float(llm_data.get("temperature", LLMConfig.temperature)),
        timeout=float(llm_data.get("timeout", LLMConfig.timeout)),
        max_retries=int(llm_data.get("max_retries", LLMConfig.max_retries)),
        streaming=_bool(llm_data.get("streaming", LLMConfig.streaming), "llm.streaming"),
    )

    work_dir = _resolve_path(
        setting("AGENT_WORK_DIR", agent_data.get("work_dir", "."), "."),
        base_dir,
        "agent.work_dir",
    )
    max_iterations = int(agent_data.get("max_iterations", AgentConfig.max_iterations))
    if not 1 <= max_iterations <= 50:
        raise ConfigError("'agent.max_iterations' must be between 1 and 50")

    session_dir = _resolve_path(session_data.get("dir", ".sessions"), base_dir, "session.dir")
    shell_timeout = int(tools_data.get("shell_timeout", AgentConfig.shell_timeout))
    if not 1 <= shell_timeout <= 30:
        raise ConfigError("'tools.shell_timeout' must be between 1 and 30")
    search_max_results = int(tools_data.get("search_max_results", AgentConfig.search_max_results))
    if not 1 <= search_max_results <= 100:
        raise ConfigError("'tools.search_max_results' must be between 1 and 100")

    return AgentConfig(
        llm=LLMConfig(
            base_url=llm_config.base_url,
            model=llm_config.model,
            api_key=llm_config.api_key,
            temperature=llm_config.temperature,
            timeout=float(_number(llm_config.timeout, "llm.timeout", minimum=0.1)),
            max_retries=int(_number(llm_config.max_retries, "llm.max_retries", integer=True)),
            streaming=llm_config.streaming,
        ),
        work_dir=work_dir,
        max_iterations=max_iterations,
        session_dir=session_dir,
        session_auto_save=_bool(session_data.get("auto_save", AgentConfig.session_auto_save), "session.auto_save"),
        system_prompt=str(agent_data.get("system_prompt", AgentConfig.system_prompt) or ""),
        shell_timeout=shell_timeout,
        search_max_results=search_max_results,
    )
