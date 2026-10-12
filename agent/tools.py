"""Tool registration and built-in file-system tools for the teaching Agent."""

from __future__ import annotations

import inspect
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import AgentConfig

MAX_FILE_BYTES = 1024 * 1024
DEFAULT_SEARCH_RESULTS = 50
SHELL_WARNING = "教学用命令执行工具，不是安全沙箱。"


@dataclass
class ToolContext:
    """Mutable state shared by tools during one Agent run."""

    work_dir: Path | str
    session_id: str = ""
    config: AgentConfig | None = None
    root_dir: Path | str | None = None

    def __post_init__(self) -> None:
        configured_root = self.root_dir if self.root_dir is not None else self.work_dir
        self.root_dir = Path(configured_root).expanduser().resolve()
        self.work_dir = Path(self.work_dir).expanduser().resolve()
        if not _is_within(self.work_dir, self.root_dir):
            self.work_dir = self.root_dir


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    function: Callable[..., str]


TOOL_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, schema: dict[str, Any]) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a callable and its OpenAI function schema."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("tool name must be a non-empty string")
    if name in TOOL_REGISTRY:
        raise ValueError(f"tool already registered: {name}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("tool description must be a non-empty string")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("tool schema must be an object schema")

    def register(function: Callable[..., str]) -> Callable[..., str]:
        parameters = dict(schema)
        TOOL_REGISTRY[name] = Tool(name, description, parameters, function)
        return function

    return register


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return the registered tools in OpenAI Function Calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": item.name,
                "description": item.description,
                "parameters": item.schema,
            },
        }
        for item in sorted(TOOL_REGISTRY.values(), key=lambda registered: registered.name)
    ]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_existing(context: ToolContext, value: str | Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path(context.work_dir) / candidate
    normalized = candidate.resolve(strict=False)
    if not _is_within(normalized, Path(context.root_dir)):
        raise ValueError(f"{label} is outside the allowed work directory")
    try:
        resolved = normalized.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist") from exc
    if not _is_within(resolved, Path(context.root_dir)):
        raise ValueError(f"{label} is outside the allowed work directory")
    return resolved


def _resolve_for_write(context: ToolContext, value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path(context.work_dir) / candidate
    candidate = candidate.absolute()
    parent = candidate.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    resolved_parent = parent.resolve(strict=True)
    if not _is_within(resolved_parent, Path(context.root_dir)):
        raise ValueError("path is outside the allowed work directory")
    resolved = (resolved_parent / Path(*candidate.parts[len(parent.parts) :])).resolve(strict=False)
    if not _is_within(resolved, Path(context.root_dir)):
        raise ValueError("path is outside the allowed work directory")
    if resolved.exists() and resolved.is_symlink():
        raise ValueError("symbolic-link targets are not allowed")
    return resolved


def _error(message: str) -> str:
    return f"[错误] {message}"


def _result_or_error(function: Callable[..., str], *args: Any, **kwargs: Any) -> str:
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        return _error(str(exc) or "tool execution failed")


@tool(
    name="read_file",
    description="读取工作目录范围内的 UTF-8 文本文件",
    schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "文件路径"}},
        "required": ["path"],
        "additionalProperties": False,
    },
)
def read_file(path: str, context: ToolContext) -> str:
    file_path = _resolve_existing(context, path, "file path")
    if not file_path.is_file():
        raise ValueError("path is not a file")
    if file_path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file exceeds the 1 MB read limit")
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("binary or non-UTF-8 files are not supported") from exc


@tool(
    name="write_file",
    description="在工作目录范围内写入或追加 UTF-8 文本文件",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "要写入的文本"},
            "append": {"type": "boolean", "description": "是否追加，默认 false"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)
def write_file(path: str, content: str, context: ToolContext, append: bool = False) -> str:
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("content exceeds the 1 MB write limit")
    file_path = _resolve_for_write(context, path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with file_path.open(mode, encoding="utf-8") as output:
        output.write(content)
    return f"已{'追加' if append else '写入'}文件: {file_path.relative_to(context.root_dir)}"


@tool(
    name="list_dir",
    description="列出当前目录或指定目录的直接内容",
    schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录路径，默认当前目录"}},
        "additionalProperties": False,
    },
)
def list_dir(path: str | None = None, *, context: ToolContext) -> str:
    directory = _resolve_existing(context, path or ".", "directory path")
    if not directory.is_dir():
        raise ValueError("path is not a directory")
    entries = [entry for entry in directory.iterdir() if not entry.name.startswith(".")]
    entries.sort(key=lambda entry: entry.name.casefold())
    if not entries:
        return "目录为空"
    return "\n".join(
        f"{entry.name}{'/' if entry.is_dir() else ''}" for entry in entries
    )


@tool(
    name="change_dir",
    description="切换 ToolContext 的当前工作目录",
    schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目标目录路径"}},
        "required": ["path"],
        "additionalProperties": False,
    },
)
def change_dir(path: str, context: ToolContext) -> str:
    directory = _resolve_existing(context, path, "directory path")
    if not directory.is_dir():
        raise ValueError("path is not a directory")
    context.work_dir = directory
    return f"当前工作目录已切换为: {directory.relative_to(context.root_dir)}"


def _command_args(command: str) -> list[str]:
    if not command.strip():
        raise ValueError("command must be a non-empty string")
    tokens = shlex.split(command, posix=False)
    if not tokens:
        raise ValueError("command must be a non-empty string")
    return [
        token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
        for token in tokens
    ]


@tool(
    name="shell",
    description="在当前工作目录执行教学用命令（不是安全沙箱）",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "number", "minimum": 0.1, "description": "超时时间（秒）"},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)
def shell(command: str, context: ToolContext, timeout: float | None = None) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    cwd = _resolve_existing(context, ".", "working directory")
    if not cwd.is_dir():
        raise ValueError("current working directory is not a directory")
    configured_timeout = context.config.shell_timeout if context.config else 10
    effective_timeout = configured_timeout if timeout is None else timeout
    if isinstance(effective_timeout, bool) or not isinstance(effective_timeout, (int, float)):
        raise ValueError("timeout must be a number")
    if effective_timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    try:
        completed = subprocess.run(
            _command_args(command),
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _error(f"命令执行超时（{effective_timeout:g} 秒）; {SHELL_WARNING}")
    output = (
        f"{SHELL_WARNING}\n"
        f"退出码: {completed.returncode}\n"
        f"stdout:\n{completed.stdout}"
        f"stderr:\n{completed.stderr}"
    )
    return output.rstrip()


@tool(
    name="search_files",
    description="递归搜索工作目录内的文件名和可选内容关键词",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "文件名 glob 模式"},
            "path": {"type": "string", "description": "搜索起始目录，默认当前目录"},
            "content_keyword": {"type": "string", "description": "可选的文本内容关键词"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
)
def search_files(
    pattern: str,
    context: ToolContext,
    path: str | None = None,
    content_keyword: str | None = None,
    max_results: int = DEFAULT_SEARCH_RESULTS,
) -> str:
    if not pattern:
        raise ValueError("pattern must be non-empty")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 50:
        raise ValueError("max_results must be between 1 and 50")
    start = _resolve_existing(context, path or ".", "search path")
    if not start.is_dir():
        raise ValueError("search path is not a directory")
    matches: list[Path] = []
    for candidate in start.rglob(pattern):
        if not candidate.is_file() or any(part.startswith(".") for part in candidate.relative_to(context.root_dir).parts):
            continue
        resolved = candidate.resolve()
        if not _is_within(resolved, Path(context.root_dir)):
            continue
        if content_keyword is not None:
            try:
                if content_keyword not in resolved.read_text(encoding="utf-8"):
                    continue
            except (OSError, UnicodeDecodeError):
                continue
        matches.append(resolved)
    matches = sorted(set(matches), key=lambda item: str(item.relative_to(context.root_dir)).casefold())
    if not matches:
        return "未找到匹配文件"
    truncated = len(matches) > max_results
    lines = [str(item.relative_to(context.root_dir)) for item in matches[:max_results]]
    if truncated:
        lines.append(f"结果已截断，仅显示前 {max_results} 项")
    return "\n".join(lines)


def execute_tool(name: str, arguments: dict[str, Any] | str, context: ToolContext) -> str:
    """Execute one registered tool and convert every failure to an error string."""
    registered = TOOL_REGISTRY.get(name)
    if registered is None:
        return _error(f"未知工具: {name}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return _error("工具参数不是有效 JSON")
    if not isinstance(arguments, dict):
        return _error("工具参数必须是 JSON 对象")
    try:
        signature = inspect.signature(registered.function)
        signature.bind(**arguments, context=context)
        return registered.function(**arguments, context=context)
    except TypeError as exc:
        return _error(f"工具参数错误: {exc}")
    except Exception as exc:
        return _error(str(exc) or "工具执行失败")
