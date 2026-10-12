"""Interactive command-line interface for the teaching Agent."""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TextIO

try:
    import readline as _readline
except ImportError:  # pragma: no cover - Windows fallback
    try:
        import pyreadline3 as _readline
    except ImportError:  # pragma: no cover - optional dependency
        _readline = None

from .config import AgentConfig
from .loop import AgentLoop, AgentLoopError
from .session import Session, SessionError, SessionManager
from .tools import TOOL_REGISTRY, ToolContext, execute_tool

try:
    from colorama import Fore, Style, init as colorama_init
except ImportError:  # pragma: no cover - exercised only without optional dependency
    class _NoColor:
        BLACK = BLUE = CYAN = GREEN = MAGENTA = RED = RESET = WHITE = YELLOW = ""

    Fore = Style = _NoColor()

    def colorama_init(*args: Any, **kwargs: Any) -> None:
        return None


HELP_TEXT = """可用命令：
/help                         显示帮助
/exit 或 /quit                保存并退出
/tools                        列出可用工具
/session new                  保存当前会话并创建新会话
/session list                 列出会话
/session load <id>            加载指定会话
/session info                 显示当前会话信息
/history                      显示对话摘要
/clear                        清空消息历史（不会撤销文件或命令操作）
/cd <path>                    切换当前工作目录
/config                       显示非敏感配置
"""


class CLI:
    """Own terminal interaction while delegating work to AgentLoop."""

    def __init__(
        self,
        loop: AgentLoop,
        session: Session,
        session_manager: SessionManager,
        config: AgentConfig,
        *,
        stream: bool | None = None,
        input_func: Callable[[str], str] = input,
        output: TextIO | None = None,
        color: bool = True,
        startup_notice: str | None = None,
    ) -> None:
        self.loop = loop
        self.session = session
        self.session_manager = session_manager
        self.config = config
        self.stream = config.streaming if stream is None else stream
        self.input_func = input_func
        self.output = output or sys.stdout
        self.color = color
        self.startup_notice = startup_notice
        colorama_init(strip=not color, autoreset=False)
        self._configure_readline()

    def _configure_readline(self) -> None:
        if _readline is None or self.input_func is not input:
            return
        try:
            _readline.parse_and_bind('tab: complete')
            commands = ['/help', '/exit', '/quit', '/tools', '/session', '/history', '/clear', '/cd', '/config']
            _readline.set_completer(lambda text, state: [item for item in commands if item.startswith(text)][state] if state < len([item for item in commands if item.startswith(text)]) else None)
        except (AttributeError, TypeError):
            return

    def run(self) -> int:
        self._system(self.startup_notice) if self.startup_notice else None
        self._show_banner()
        while True:
            try:
                line = self.input_func("> ")
            except EOFError:
                self._save(force=True)
                self._system("输入结束，已退出。")
                return 0
            except KeyboardInterrupt:
                self._write("\n[中断] 已取消当前输入。\n")
                continue

            if not line.strip():
                continue
            if line.lstrip().startswith("/"):
                should_exit = self._handle_command(line.strip())
                if should_exit:
                    return 0
                continue
            self.run_message(line)

    def run_message(self, user_input: str) -> bool:
        """Run one user message, display it, and apply the save policy."""
        try:
            if self.stream:
                answer = self.loop.stream_run(
                    user_input,
                    on_text=self._stream_text,
                    on_tool_call=self._show_tool_call,
                    on_tool_result=self._show_tool_result,
                )
                if answer:
                    self._write("\n")
            else:
                answer = self.loop.run(
                    user_input,
                    on_tool_call=self._show_tool_call,
                    on_tool_result=self._show_tool_result,
                )
                self._answer(answer)
            self._sync_session()
            saved = self._save()
            return saved
        except KeyboardInterrupt:
            self._write("\n[中断] 已取消当前模型请求。\n")
            self._sync_session()
            self._save(force=True)
            return False
        except (AgentLoopError, SessionError, RuntimeError) as exc:
            self._error(self._safe_error(exc))
            self._sync_session()
            self._save(force=True)
            return False

    def _handle_command(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self._error(f"命令格式错误: {exc}")
            return False
        if not parts:
            return False
        command = parts[0].lower()
        if command in {"/exit", "/quit"}:
            self._save(force=True)
            return True
        if command == "/help":
            self._write(HELP_TEXT)
        elif command == "/tools":
            self._show_tools()
        elif command == "/session":
            self._session_command(parts[1:])
        elif command == "/history":
            self._show_history()
        elif command == "/clear":
            self.loop.messages.clear()
            self._sync_session()
            self._save()
            self._system("消息历史已清空；这不会撤销已经完成的文件或命令操作。")
        elif command == "/cd":
            path = " ".join(parts[1:])
            if not path:
                self._error("用法: /cd <path>")
            else:
                result = execute_tool("change_dir", {"path": path}, self.loop.context)
                if result.startswith("[错误]"):
                    self._error(result)
                else:
                    self._sync_session()
                    self._save()
                    self._system(result)
        elif command == "/config":
            self._show_config()
        else:
            self._system(f"未知命令: {parts[0]}")
            self._write(HELP_TEXT)
        return False

    def _session_command(self, args: list[str]) -> None:
        if not args:
            self._write("用法: /session new|list|load <id>|info\n")
            return
        action = args[0].lower()
        if action == "new" and len(args) == 1:
            if not self._save(force=True):
                return
            new_session = self.session_manager.new_session(
                root_dir=self.config.work_dir,
                work_dir=self.config.work_dir,
            )
            self._activate(new_session)
            self._system(f"已创建新会话: {new_session.id}")
        elif action == "list" and len(args) == 1:
            sessions = self.session_manager.list_sessions()
            if not sessions:
                self._write("暂无已保存会话。\n")
                return
            for item in sessions:
                self._write(f"{item.id}\t{item.title or '(无标题)'}\t{item.updated_at}\n")
        elif action == "load" and len(args) == 2:
            if not self._save(force=True):
                return
            try:
                loaded = self.session_manager.load_session(args[1])
                self._validate_restore_root(loaded)
            except SessionError as exc:
                self._error(self._safe_error(exc))
                return
            self._activate(loaded)
            self._system(f"已加载会话: {loaded.id}")
        elif action == "info" and len(args) == 1:
            self._write(
                f"id: {self.session.id}\n"
                f"title: {self.session.title or '(无标题)'}\n"
                f"created_at: {self.session.created_at}\n"
                f"updated_at: {self.session.updated_at}\n"
                f"work_dir: {self.session.work_dir}\n"
            )
        else:
            self._write("用法: /session new|list|load <id>|info\n")

    def _activate(self, session: Session) -> None:
        self.session = session
        context = ToolContext(
            work_dir=session.work_dir,
            root_dir=session.root_dir,
            session_id=session.id,
            config=self.config,
        )
        self.loop = AgentLoop(
            self.loop.llm,
            context,
            messages=session.messages,
            max_iterations=self.config.max_iterations,
        )

    def _validate_restore_root(self, session: Session) -> None:
        configured_root = self.config.work_dir.resolve()
        saved_root = session.root_dir.resolve()
        if saved_root != configured_root:
            raise SessionError("Session root directory differs from the configured work directory")
        if not saved_root.exists() or not saved_root.is_dir():
            raise SessionError("Session root directory does not exist")
        if not session.work_dir.exists() or not session.work_dir.is_dir():
            raise SessionError("Session current work directory does not exist")
        try:
            session.work_dir.resolve().relative_to(saved_root)
        except ValueError as exc:
            raise SessionError("Session current work directory is outside the allowed root") from exc

    def _show_banner(self) -> None:
        self._system(f"Agent | Session: {self.session.id} | 工作目录: {self.loop.context.work_dir}")
        self._system("输入 /help 查看可用命令。")

    def _show_tools(self) -> None:
        for name in ("read_file", "write_file", "list_dir", "change_dir", "shell", "search_files"):
            state = "启用" if name in TOOL_REGISTRY else "禁用"
            self._write(f"{name}: {state}\n")

    def _show_history(self) -> None:
        if not self.loop.messages:
            self._write("暂无消息。\n")
            return
        for index, message in enumerate(self.loop.messages, start=1):
            role = message.get("role", "unknown")
            if role == "assistant" and message.get("tool_calls"):
                details = ", ".join(
                    str(call.get("function", {}).get("name", "<unknown>"))
                    for call in message["tool_calls"]
                    if isinstance(call, dict)
                )
                summary = f"tool_calls: {details}"
            else:
                summary = self._truncate(str(message.get("content") or "").replace("\n", " "), 160)
            self._write(f"{index}. {role}: {self._redact(summary)}\n")

    def _show_config(self) -> None:
        self._write(
            f"model: {self.config.llm.model}\n"
            f"base_url: {self.config.llm.base_url}\n"
            f"work_dir: {self.loop.context.work_dir}\n"
            f"max_iterations: {self.config.max_iterations}\n"
            f"session_dir: {self.config.session_dir}\n"
            f"auto_save: {self.config.session_auto_save}\n"
            f"streaming: {self.stream}\n"
        )

    def _show_tool_call(self, name: str, arguments: str) -> None:
        self._write(self._gray(f"[调用工具] {name} {self._truncate(arguments, 240)}\n"))

    def _show_tool_result(self, call_id: str, result: str) -> None:
        self._write(self._gray(f"[工具结果] {call_id}: {self._truncate(result, 400)}\n"))

    def _stream_text(self, text: str) -> None:
        self.output.write(text)
        self.output.flush()

    def _answer(self, answer: str) -> None:
        self._write(self._green(answer) + ("\n" if not answer.endswith("\n") else ""))

    def _sync_session(self) -> None:
        self.session.messages = self.loop.messages
        self.session.work_dir = Path(self.loop.context.work_dir).resolve()
        self.session.root_dir = Path(self.loop.context.root_dir).resolve()

    def _save(self, *, force: bool = False) -> bool:
        self._sync_session()
        if not force and not self.config.session_auto_save:
            return True
        try:
            self.session_manager.save_session(self.session)
            return True
        except SessionError as exc:
            self._error(f"Session 保存失败: {self._safe_error(exc)}")
            return False

    def _safe_error(self, error: BaseException) -> str:
        text = str(error) or error.__class__.__name__
        api_key = self.config.llm.api_key
        if api_key:
            text = text.replace(api_key, "[已隐藏]")
        return text

    def _redact(self, text: str) -> str:
        return self._safe_error(RuntimeError(text))

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit] + "..."

    def _write(self, text: str) -> None:
        self.output.write(text)
        self.output.flush()

    def _system(self, text: str) -> None:
        self._write(self._blue(f"[系统] {text}\n"))

    def _error(self, text: str) -> None:
        self._write(self._red(f"[错误] {text}\n"))

    def _gray(self, text: str) -> str:
        return f"{Fore.WHITE}{text}{Style.RESET_ALL}" if self.color else text

    def _blue(self, text: str) -> str:
        return f"{Fore.CYAN}{text}{Style.RESET_ALL}" if self.color else text

    def _red(self, text: str) -> str:
        return f"{Fore.RED}{text}{Style.RESET_ALL}" if self.color else text

    def _green(self, text: str) -> str:
        return f"{Fore.GREEN}{text}{Style.RESET_ALL}" if self.color else text
