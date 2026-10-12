from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent.config import AgentConfig, LLMConfig
from agent.loop import MAX_ITERATIONS_WARNING, AgentLoop, AgentLoopError
from agent.tools import ToolContext, get_tool_schemas


class MockLLM:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.calls.append((list(messages), tools))
        if not self.responses:
            raise AssertionError("MockLLM ran out of responses")
        return self.responses.pop(0)


class StreamingMockLLM:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def stream_chat(self, messages, tools=None, on_content_delta=None):
        self.calls += 1
        return self.responses.pop(0)


def test_stream_run_executes_only_complete_tool_message(context: ToolContext) -> None:
    (context.work_dir / "stream.txt").write_text("stream content", encoding="utf-8")
    llm = StreamingMockLLM(
        assistant(tool_calls=[call("stream-call", "read_file", {"path": "stream.txt"})]),
        assistant("stream final"),
    )
    loop = AgentLoop(llm, context)

    result = loop.stream_run("stream this")

    assert result == "stream final"
    assert llm.calls == 2
    assert loop.messages[2] == {
        "role": "tool",
        "tool_call_id": "stream-call",
        "content": "stream content",
    }


def test_system_prompt_is_injected_once_and_formats_work_dir(context: ToolContext) -> None:
    context.config = AgentConfig(
        llm=LLMConfig(api_key="test"),
        work_dir=context.work_dir,
        system_prompt="Work directory: {work_dir}",
    )
    loop = AgentLoop(MockLLM(assistant("one"), assistant("two")), context)

    loop.run("first")
    loop.run("second")

    assert [message["role"] for message in loop.messages].count("system") == 1
    assert str(context.work_dir) in loop.messages[0]["content"]


def assistant(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        result["tool_calls"] = tool_calls
    return result


def call(call_id: str, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    config = AgentConfig(llm=LLMConfig(api_key="test"), work_dir=tmp_path)
    return ToolContext(work_dir=tmp_path, session_id="loop-test", config=config)


def run_loop(context: ToolContext, *responses: dict[str, Any]) -> tuple[AgentLoop, MockLLM, str]:
    llm = MockLLM(*responses)
    loop = AgentLoop(llm, context)
    return loop, llm, loop.run("请完成任务")


def test_direct_answer(context: ToolContext) -> None:
    loop, llm, result = run_loop(context, assistant("直接回答"))

    assert result == "直接回答"
    assert len(llm.calls) == 1
    assert loop.messages == [
        {"role": "user", "content": "请完成任务"},
        {"role": "assistant", "content": "直接回答"},
    ]
    assert llm.calls[0][1] == get_tool_schemas()


def test_single_read_file_call(context: ToolContext) -> None:
    (context.work_dir / "note.txt").write_text("hello", encoding="utf-8")
    loop, llm, result = run_loop(
        context,
        assistant(tool_calls=[call("read-1", "read_file", {"path": "note.txt"})]),
        assistant("文件内容是 hello"),
    )

    assert result == "文件内容是 hello"
    assert llm.calls[1][0][-1] == {"role": "tool", "tool_call_id": "read-1", "content": "hello"}
    assert loop.messages[1]["tool_calls"][0]["id"] == "read-1"


def test_read_file_then_write_file(context: ToolContext) -> None:
    (context.work_dir / "source.txt").write_text("source", encoding="utf-8")
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("read-1", "read_file", {"path": "source.txt"})]),
        assistant(tool_calls=[call("write-1", "write_file", {"path": "copy.txt", "content": "source"})]),
        assistant("已复制"),
    )

    assert result == "已复制"
    assert (context.work_dir / "copy.txt").read_text(encoding="utf-8") == "source"
    assert [message["role"] for message in loop.messages] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant"
    ]


def test_list_dir_then_read_file(context: ToolContext) -> None:
    (context.work_dir / "listed.txt").write_text("listed", encoding="utf-8")
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("list-1", "list_dir", {})]),
        assistant(tool_calls=[call("read-1", "read_file", {"path": "listed.txt"})]),
        assistant("已读取"),
    )

    assert result == "已读取"
    assert "listed.txt" in loop.messages[2]["content"]
    assert loop.messages[4]["content"] == "listed"


def test_change_dir_then_use_relative_path(context: ToolContext) -> None:
    subdir = context.work_dir / "subdir"
    subdir.mkdir()
    (subdir / "inside.txt").write_text("inside", encoding="utf-8")
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("cd-1", "change_dir", {"path": "subdir"})]),
        assistant(tool_calls=[call("read-1", "read_file", {"path": "inside.txt"})]),
        assistant("已读取子目录文件"),
    )

    assert result == "已读取子目录文件"
    assert context.work_dir == subdir.resolve()
    assert loop.messages[4]["content"] == "inside"


def shell_command(*args: str) -> str:
    return subprocess.list2cmdline([sys.executable, *args])


def test_shell_call(context: ToolContext) -> None:
    command = shell_command("-c", "print('shell-result')")
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("shell-1", "shell", {"command": command})]),
        assistant("shell 已完成"),
    )

    assert result == "shell 已完成"
    assert "shell-result" in loop.messages[2]["content"]


def test_search_files_call(context: ToolContext) -> None:
    (context.work_dir / "found.md").write_text("needle", encoding="utf-8")
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("search-1", "search_files", {"pattern": "*.md", "content_keyword": "needle"})]),
        assistant("找到文件"),
    )

    assert result == "找到文件"
    assert "found.md" in loop.messages[2]["content"]


def test_multiple_tool_calls_execute_in_order(context: ToolContext) -> None:
    (context.work_dir / "a.txt").write_text("a", encoding="utf-8")
    loop, llm, result = run_loop(
        context,
        assistant(
            tool_calls=[
                call("first", "read_file", {"path": "a.txt"}),
                call("second", "write_file", {"path": "b.txt", "content": "b"}),
            ]
        ),
        assistant("两个工具已完成"),
    )

    assert result == "两个工具已完成"
    assert loop.messages[2:4] == [
        {"role": "tool", "tool_call_id": "first", "content": "a"},
        {"role": "tool", "tool_call_id": "second", "content": "已写入文件: b.txt"},
    ]
    assert (context.work_dir / "b.txt").read_text(encoding="utf-8") == "b"
    assert len(llm.calls) == 2


def test_unknown_tool_becomes_tool_error_and_loop_continues(context: ToolContext) -> None:
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("unknown-1", "not_registered", {})]),
        assistant("已处理错误"),
    )

    assert result == "已处理错误"
    assert loop.messages[2]["tool_call_id"] == "unknown-1"
    assert "未知工具" in loop.messages[2]["content"]


def test_invalid_json_arguments_become_tool_error(context: ToolContext) -> None:
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("bad-json", "read_file", "{not-json")]),
        assistant("参数有误"),
    )

    assert result == "参数有误"
    assert loop.messages[2]["tool_call_id"] == "bad-json"
    assert "JSON" in loop.messages[2]["content"]


def test_tool_execution_error_becomes_tool_message(context: ToolContext) -> None:
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[call("missing-file", "read_file", {"path": "missing.txt"})]),
        assistant("文件不存在"),
    )

    assert result == "文件不存在"
    assert loop.messages[2]["tool_call_id"] == "missing-file"
    assert loop.messages[2]["content"].startswith("[错误]")


def test_max_iterations_stops_after_ten_requests(context: ToolContext) -> None:
    responses = [assistant(tool_calls=[call(f"call-{index}", "list_dir", {})]) for index in range(10)]
    loop, llm, result = run_loop_with_max_iterations(context, 10, responses)

    assert result == MAX_ITERATIONS_WARNING
    assert len(llm.calls) == 10
    assert len([message for message in loop.messages if message["role"] == "assistant"]) == 10
    assert loop.messages[-1]["role"] == "tool"


def run_loop_with_max_iterations(
    context: ToolContext, max_iterations: int, responses: list[dict[str, Any]]
) -> tuple[AgentLoop, MockLLM, str]:
    llm = MockLLM(*responses)
    loop = AgentLoop(llm, context, max_iterations=max_iterations)
    return loop, llm, loop.run("循环")


def test_tool_call_ids_are_preserved_and_messages_are_ordered(context: ToolContext) -> None:
    (context.work_dir / "ordered.txt").write_text("ordered", encoding="utf-8")
    loop, _, _ = run_loop(
        context,
        assistant(tool_calls=[call("id-123", "read_file", {"path": "ordered.txt"})]),
        assistant("完成"),
    )

    assert [message["role"] for message in loop.messages] == ["user", "assistant", "tool", "assistant"]
    assert loop.messages[1]["tool_calls"][0]["id"] == "id-123"
    assert loop.messages[2]["tool_call_id"] == "id-123"


def test_malformed_tool_call_still_gets_a_paired_error(context: ToolContext) -> None:
    loop, _, result = run_loop(
        context,
        assistant(tool_calls=[{"id": "malformed", "type": "function", "function": {}}]),
        assistant("调用格式错误"),
    )

    assert result == "调用格式错误"
    assert loop.messages[2]["tool_call_id"] == "malformed"
    assert loop.messages[2]["content"].startswith("[错误]")


def test_model_error_is_wrapped(context: ToolContext) -> None:
    class FailingLLM:
        def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
            raise RuntimeError("network")

    loop = AgentLoop(FailingLLM(), context)

    with pytest.raises(AgentLoopError, match="模型请求失败"):
        loop.run("失败")
