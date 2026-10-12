from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import main as app
from agent.cli import CLI
from agent.config import AgentConfig, LLMConfig
from agent.loop import AgentLoop
from agent.session import SessionManager
from agent.tools import ToolContext


class FakeLLM:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.stream_responses = list(responses)
        self.chat_calls = 0
        self.stream_calls = 0

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.chat_calls += 1
        return self.responses.pop(0)

    def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], on_content_delta=None):
        self.stream_calls += 1
        response = self.stream_responses.pop(0)
        content = response.get("content")
        if content and on_content_delta:
            for part in (content[:2], content[2:]):
                if part:
                    on_content_delta(part)
        return response


def fixture_cli(tmp_path: Path, *, stream: bool = False, auto_save: bool = True, responses=()):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    config = AgentConfig(
        llm=LLMConfig(api_key="secret-cli-key", streaming=stream),
        work_dir=root,
        session_dir=tmp_path / ".sessions",
        session_auto_save=auto_save,
        system_prompt="system test prompt",
    )
    manager = SessionManager(config.session_dir, root_dir=root)
    session = manager.new_session(root_dir=root, work_dir=root)
    llm = FakeLLM(*responses)
    context = ToolContext(work_dir=root, root_dir=root, session_id=session.id, config=config)
    loop = AgentLoop(llm, context, messages=session.messages, max_iterations=config.max_iterations)
    output = io.StringIO()
    cli = CLI(loop, session, manager, config, stream=stream, output=output, color=False)
    return cli, manager, session, llm, output, config


def test_parser_supports_all_options_and_conflict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = app.build_parser().parse_args(
        ["--message", "hello", "--resume", "--config", str(tmp_path / "config.yaml"), "--no-stream"]
    )
    assert args.message == "hello"
    assert args.resume and args.no_stream
    assert args.config == str(tmp_path / "config.yaml")

    with pytest.raises(SystemExit):
        app.main(["--resume", "--session", "some-id"])
    assert "不能同时使用" in capsys.readouterr().err


def test_single_message_nonstream_saves_and_prints(tmp_path: Path) -> None:
    cli, manager, session, llm, output, _ = fixture_cli(
        tmp_path, responses=({"role": "assistant", "content": "answer\nline"},)
    )

    cli.run_message("question")

    assert llm.chat_calls == 1
    assert llm.stream_calls == 0
    assert session.messages[-1]["content"] == "answer\nline"
    assert manager.load_session(session.id).messages == session.messages
    assert "answer\nline" in output.getvalue()


def test_single_message_model_error_returns_failure(tmp_path: Path) -> None:
    class FailingLLM(FakeLLM):
        def chat(self, messages, tools):
            raise RuntimeError("request failed")

    cli, _, _, _, output, _ = fixture_cli(tmp_path)
    cli.loop.llm = FailingLLM()

    assert cli.run_message("question") is False
    assert "错误" in output.getvalue()

def test_stream_mode_uses_real_stream_interface_and_saves(tmp_path: Path) -> None:
    cli, manager, session, llm, output, _ = fixture_cli(
        tmp_path, stream=True, responses=({"role": "assistant", "content": "streamed answer"},)
    )

    cli.run_message("question")

    assert llm.stream_calls == 1
    assert llm.chat_calls == 0
    assert "streamed answer" in output.getvalue()
    assert manager.load_session(session.id).messages == session.messages


def test_config_auto_save_false_skips_turn_save_but_exit_saves(tmp_path: Path) -> None:
    cli, manager, session, _, _, _ = fixture_cli(
        tmp_path, auto_save=False, responses=({"role": "assistant", "content": "answer"},)
    )

    cli.run_message("question")
    assert manager.list_sessions() == []

    assert cli._save(force=True)
    assert manager.load_session(session.id).messages == session.messages


def test_help_tools_config_and_clear_commands(tmp_path: Path) -> None:
    cli, _, session, _, output, _ = fixture_cli(tmp_path)
    cli.loop.messages.extend([
        {"role": "user", "content": "secret-cli-key should redact"},
        {"role": "assistant", "content": "answer"},
    ])

    for command in ("/help", "/tools", "/history", "/config"):
        cli._handle_command(command)
    cli._handle_command("/clear")

    text = output.getvalue()
    assert "read_file" in text and "search_files" in text
    assert "secret-cli-key" not in text
    assert "不会撤销" in text
    assert session.messages == []


def test_cd_supports_spaces_and_uses_change_dir_tool(tmp_path: Path) -> None:
    cli, _, session, _, output, _ = fixture_cli(tmp_path)
    (Path(cli.loop.context.work_dir) / "folder with spaces").mkdir()

    cli._handle_command('/cd "folder with spaces"')

    assert cli.loop.context.work_dir.name == "folder with spaces"
    assert session.work_dir == cli.loop.context.work_dir
    assert "已切换" in output.getvalue()


def test_session_new_and_load_rebuild_loop(tmp_path: Path) -> None:
    cli, manager, original, llm, output, config = fixture_cli(tmp_path)
    cli.loop.messages.append({"role": "user", "content": "existing"})
    manager.save_session(original)
    target = manager.new_session(root_dir=config.work_dir, work_dir=config.work_dir)
    target.messages = [{"role": "user", "content": "target history"}]
    manager.save_session(target)

    cli._handle_command(f"/session load {target.id}")

    assert cli.session.id == target.id
    assert cli.loop.messages == target.messages
    assert cli.loop.context.session_id == target.id
    assert cli.loop.llm is llm

    cli._handle_command("/session new")
    assert cli.session.id not in {original.id, target.id}
    assert cli.loop.messages == []
    assert "已创建新会话" in output.getvalue()


def test_session_info_list_and_unknown_command(tmp_path: Path) -> None:
    cli, manager, session, _, output, _ = fixture_cli(tmp_path)
    manager.save_session(session)

    cli._handle_command("/session info")
    cli._handle_command("/session list")
    cli._handle_command("/not-a-command")

    text = output.getvalue()
    assert session.id in text
    assert "created_at:" in text
    assert "未知命令" in text
    assert "api_key" not in text.lower()


def test_readline_or_eof_loop_exits_after_saving(tmp_path: Path) -> None:
    cli, manager, session, _, output, _ = fixture_cli(tmp_path)
    cli.input_func = lambda prompt: (_ for _ in ()).throw(EOFError())

    assert cli.run() == 0
    assert manager.load_session(session.id).messages == []
    assert "已退出" in output.getvalue()


def test_session_restore_rejects_changed_root(tmp_path: Path) -> None:
    cli, _, session, _, output, config = fixture_cli(tmp_path)
    session.root_dir = tmp_path / "elsewhere"
    session.root_dir.mkdir()

    with pytest.raises(Exception, match="differs"):
        cli._validate_restore_root(session)


def test_main_no_history_resume_creates_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    config = AgentConfig(
        llm=LLMConfig(api_key="dummy", streaming=False),
        work_dir=root,
        session_dir=tmp_path / ".sessions",
    )
    monkeypatch.setattr(app, "load_config", lambda _: config)
    monkeypatch.setattr(app, "LLMClient", lambda _: FakeLLM())
    monkeypatch.setattr(CLI, "run", lambda self: 0)

    assert app.main(["--resume", "--no-stream"]) == 0


def test_main_specified_unknown_session_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    config = AgentConfig(llm=LLMConfig(api_key="dummy"), work_dir=root, session_dir=tmp_path / ".sessions")
    monkeypatch.setattr(app, "load_config", lambda _: config)

    result = app.main(["--session", "missing"])

    assert result == 2
    assert "无法加载指定 Session" in capsys.readouterr().err


def test_main_message_respects_no_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    config = AgentConfig(llm=LLMConfig(api_key="dummy", streaming=True), work_dir=root, session_dir=tmp_path / ".sessions")
    fake = FakeLLM({"role": "assistant", "content": "done"})
    monkeypatch.setattr(app, "load_config", lambda _: config)
    monkeypatch.setattr(app, "LLMClient", lambda _: fake)

    assert app.main(["--message", "question", "--no-stream"]) == 0
    assert fake.chat_calls == 1
    assert fake.stream_calls == 0
