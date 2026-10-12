from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent.config import AgentConfig, LLMConfig
from agent.tools import (
    DEFAULT_SEARCH_RESULTS,
    MAX_FILE_BYTES,
    TOOL_REGISTRY,
    ToolContext,
    execute_tool,
    get_tool_schemas,
    read_file,
)


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    return ToolContext(work_dir=tmp_path, session_id="test-session")


def test_all_six_tools_are_registered_with_function_schemas() -> None:
    expected = {"read_file", "write_file", "list_dir", "change_dir", "shell", "search_files"}

    assert expected == set(TOOL_REGISTRY)
    schemas = get_tool_schemas()
    assert {item["function"]["name"] for item in schemas} == expected
    assert all(item["type"] == "function" for item in schemas)
    assert all(item["function"]["parameters"]["type"] == "object" for item in schemas)


def test_read_file_reads_utf8_text(context: ToolContext) -> None:
    (context.work_dir / "hello.txt").write_text("你好", encoding="utf-8")

    assert execute_tool("read_file", {"path": "hello.txt"}, context) == "你好"


def test_read_file_missing_file_returns_error(context: ToolContext) -> None:
    result = execute_tool("read_file", {"path": "missing.txt"}, context)

    assert result.startswith("[错误]")
    assert "not exist" in result or "No such file" in result or "不存在" in result


def test_read_file_rejects_path_traversal(context: ToolContext) -> None:
    result = execute_tool("read_file", {"path": "../outside.txt"}, context)

    assert result.startswith("[错误]")
    assert "outside" in result


def test_read_file_rejects_binary_file(context: ToolContext) -> None:
    (context.work_dir / "data.bin").write_bytes(b"\x00\xff")

    result = execute_tool("read_file", {"path": "data.bin"}, context)

    assert result.startswith("[错误]")
    assert "binary" in result or "UTF-8" in result


def test_write_file_overwrites(context: ToolContext) -> None:
    target = context.work_dir / "notes.txt"
    target.write_text("old", encoding="utf-8")

    result = execute_tool("write_file", {"path": "notes.txt", "content": "new"}, context)

    assert result.startswith("已写入")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_appends(context: ToolContext) -> None:
    target = context.work_dir / "notes.txt"
    target.write_text("old", encoding="utf-8")

    execute_tool("write_file", {"path": "notes.txt", "content": "-new", "append": True}, context)

    assert target.read_text(encoding="utf-8") == "old-new"


def test_write_file_creates_parent_directories(context: ToolContext) -> None:
    execute_tool("write_file", {"path": "nested/notes.txt", "content": "text"}, context)

    assert (context.work_dir / "nested/notes.txt").read_text(encoding="utf-8") == "text"


def test_write_file_rejects_content_over_one_mb(context: ToolContext) -> None:
    result = execute_tool("write_file", {"path": "large.txt", "content": "x" * (MAX_FILE_BYTES + 1)}, context)

    assert result.startswith("[错误]")
    assert "1 MB" in result
    assert not (context.work_dir / "large.txt").exists()


def test_write_file_rejects_path_traversal(context: ToolContext) -> None:
    result = execute_tool("write_file", {"path": "../outside.txt", "content": "no"}, context)

    assert result.startswith("[错误]")
    assert "outside" in result


def test_list_dir_lists_entries_without_reading_contents(context: ToolContext) -> None:
    (context.work_dir / "visible.txt").write_text("content", encoding="utf-8")
    (context.work_dir / ".hidden.txt").write_text("hidden", encoding="utf-8")
    (context.work_dir / "folder").mkdir()

    result = execute_tool("list_dir", {}, context)

    assert "visible.txt" in result
    assert "folder/" in result
    assert ".hidden.txt" not in result


def test_list_dir_missing_directory_returns_error(context: ToolContext) -> None:
    result = execute_tool("list_dir", {"path": "missing"}, context)

    assert result.startswith("[错误]")


def test_change_dir_succeeds(context: ToolContext) -> None:
    (context.work_dir / "subdir").mkdir()

    result = execute_tool("change_dir", {"path": "subdir"}, context)

    assert "subdir" in result
    assert context.work_dir == (context.root_dir / "subdir").resolve()


def test_change_dir_failure_keeps_original_directory(context: ToolContext) -> None:
    original = context.work_dir

    result = execute_tool("change_dir", {"path": "missing"}, context)

    assert result.startswith("[错误]")
    assert context.work_dir == original


def test_change_dir_affects_later_relative_paths(context: ToolContext) -> None:
    subdir = context.work_dir / "subdir"
    subdir.mkdir()
    (subdir / "inside.txt").write_text("inside", encoding="utf-8")

    execute_tool("change_dir", {"path": "subdir"}, context)

    assert execute_tool("read_file", {"path": "inside.txt"}, context) == "inside"


def test_change_dir_cannot_leave_root(context: ToolContext) -> None:
    original = context.work_dir

    result = execute_tool("change_dir", {"path": ".."}, context)

    assert result.startswith("[错误]")
    assert context.work_dir == original


def shell_command(*args: str) -> str:
    return subprocess.list2cmdline([sys.executable, *args])


def test_shell_returns_stdout_stderr_and_exit_code(context: ToolContext) -> None:
    command = shell_command("-c", "import sys; print('out'); print('err', file=sys.stderr)")

    result = execute_tool("shell", {"command": command}, context)

    assert "stdout:" in result
    assert "out" in result
    assert "stderr:" in result
    assert "err" in result
    assert "退出码: 0" in result
    assert "不是安全沙箱" in result


def test_shell_returns_nonzero_exit_code(context: ToolContext) -> None:
    command = shell_command("-c", "import sys; print('bad', file=sys.stderr); sys.exit(3)")

    result = execute_tool("shell", {"command": command}, context)

    assert "退出码: 3" in result
    assert "bad" in result


def test_shell_timeout_returns_clear_error(context: ToolContext) -> None:
    command = shell_command("-c", "import time; time.sleep(2)")

    result = execute_tool("shell", {"command": command, "timeout": 0.05}, context)

    assert result.startswith("[错误]")
    assert "超时" in result


def test_search_files_matches_filename(context: ToolContext) -> None:
    (context.work_dir / "one.py").write_text("one", encoding="utf-8")
    (context.work_dir / "nested").mkdir()
    (context.work_dir / "nested" / "two.py").write_text("two", encoding="utf-8")

    result = execute_tool("search_files", {"pattern": "*.py"}, context)

    assert "one.py" in result
    assert "nested" in result and "two.py" in result


def test_search_files_matches_content_keyword(context: ToolContext) -> None:
    (context.work_dir / "match.txt").write_text("needle", encoding="utf-8")
    (context.work_dir / "other.txt").write_text("different", encoding="utf-8")

    result = execute_tool(
        "search_files",
        {"pattern": "*.txt", "content_keyword": "needle"},
        context,
    )

    assert "match.txt" in result
    assert "other.txt" not in result


def test_search_files_no_results_is_explicit(context: ToolContext) -> None:
    result = execute_tool("search_files", {"pattern": "*.does-not-exist"}, context)

    assert result == "未找到匹配文件"


def test_search_files_has_default_limit_of_fifty(context: ToolContext) -> None:
    for index in range(DEFAULT_SEARCH_RESULTS + 1):
        (context.work_dir / f"{index}.txt").write_text("x", encoding="utf-8")

    result = execute_tool("search_files", {"pattern": "*.txt"}, context)

    assert "结果已截断" in result
    assert len(result.splitlines()) == DEFAULT_SEARCH_RESULTS + 1


def test_unknown_tool_returns_error(context: ToolContext) -> None:
    result = execute_tool("does_not_exist", {}, context)

    assert result.startswith("[错误]")
    assert "未知工具" in result


def test_invalid_arguments_return_error(context: ToolContext) -> None:
    result = execute_tool("read_file", "not-json", context)

    assert result.startswith("[错误]")
    assert "JSON" in result


def test_tool_internal_exception_is_converted_to_error(context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    (context.work_dir / "file.txt").write_text("content", encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    result = execute_tool("read_file", {"path": "file.txt"}, context)

    assert result.startswith("[错误]")
    assert "boom" in result or "not exist" in result
