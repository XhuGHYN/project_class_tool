from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.config import AgentConfig, LLMConfig
from agent.session import SessionError, SessionManager


def make_manager(tmp_path: Path) -> tuple[SessionManager, Path]:
    root = tmp_path / "workspace"
    root.mkdir()
    return SessionManager(tmp_path / ".sessions", root_dir=root), root


def test_new_session_has_unique_id_and_initial_state(tmp_path: Path) -> None:
    manager, root = make_manager(tmp_path)

    first = manager.new_session()
    second = manager.new_session()

    assert first.id != second.id
    assert len(first.id.split("-")) == 3
    assert first.messages == []
    assert first.root_dir == root.resolve()
    assert first.work_dir == root.resolve()


def test_save_and_load_round_trip_preserves_messages_and_metadata(tmp_path: Path) -> None:
    manager, root = make_manager(tmp_path)
    session = manager.new_session()
    session.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "读取文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "内容"},
        {"role": "assistant", "content": "完成"},
    ]
    session.title = "测试会话"
    manager.save_session(session)

    loaded = manager.load_session(session.id)

    assert loaded.id == session.id
    assert loaded.title == "测试会话"
    assert loaded.created_at == session.created_at
    assert loaded.updated_at == session.updated_at
    assert loaded.root_dir == root.resolve()
    assert loaded.work_dir == root.resolve()
    assert loaded.messages == session.messages
    assert (tmp_path / ".sessions" / f"{session.id}.json").exists()


def test_title_defaults_to_first_user_message_summary(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    session = manager.new_session()
    session.messages = [{"role": "user", "content": "这是首条用户消息，用于生成会话标题"}]

    manager.save_session(session)

    assert session.title == "这是首条用户消息，用于生成会话标题"
    assert manager.list_sessions()[0].title == session.title


def test_current_work_dir_is_saved_and_restored(tmp_path: Path) -> None:
    manager, root = make_manager(tmp_path)
    subdir = root / "subdir"
    subdir.mkdir()
    session = manager.new_session(work_dir=subdir)
    manager.save_session(session)

    loaded = manager.load_session(session.id)

    assert loaded.root_dir == root.resolve()
    assert loaded.work_dir == subdir.resolve()


def test_missing_work_dir_falls_back_to_root(tmp_path: Path) -> None:
    manager, root = make_manager(tmp_path)
    session = manager.new_session()
    session.work_dir = root / "removed"
    session.work_dir.parent.mkdir(parents=True, exist_ok=True)
    manager.save_session(session)
    session_file = tmp_path / ".sessions" / f"{session.id}.json"
    payload = json.loads(session_file.read_text(encoding="utf-8"))
    payload["work_dir"] = str(root / "removed")
    session_file.write_text(json.dumps(payload), encoding="utf-8")

    loaded = manager.load_session(session.id)

    assert loaded.work_dir == root.resolve()


def test_api_key_is_not_written_to_session(tmp_path: Path) -> None:
    manager, root = make_manager(tmp_path)
    config = AgentConfig(llm=LLMConfig(api_key="secret-that-must-not-be-saved"), work_dir=root)
    session = manager.new_session()
    session.messages = [{"role": "user", "content": "hello"}]
    manager.save_session(session)

    raw = (tmp_path / ".sessions" / f"{session.id}.json").read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert "secret-that-must-not-be-saved" not in raw
    assert "api_key" not in payload
    assert config.llm.api_key not in raw


def test_get_latest_session_returns_most_recently_saved(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    first = manager.new_session()
    first.messages = [{"role": "user", "content": "first"}]
    manager.save_session(first)
    time.sleep(0.01)
    second = manager.new_session()
    second.messages = [{"role": "user", "content": "second"}]
    manager.save_session(second)

    latest = manager.get_latest_session()

    assert latest is not None
    assert latest.id == second.id


def test_list_sessions_is_sorted_by_updated_time(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    older = manager.new_session()
    older.messages = [{"role": "user", "content": "older"}]
    manager.save_session(older)
    time.sleep(0.01)
    newer = manager.new_session()
    newer.messages = [{"role": "user", "content": "newer"}]
    manager.save_session(newer)

    sessions = manager.list_sessions()

    assert [item.id for item in sessions] == [newer.id, older.id]
    index = json.loads((tmp_path / ".sessions" / "index.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in index["sessions"]] == [newer.id, older.id]


def test_delete_session_removes_file_and_index_entry(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    session = manager.new_session()
    manager.save_session(session)

    assert manager.delete_session(session.id) is True
    assert manager.delete_session(session.id) is False
    assert manager.list_sessions() == []
    assert not (tmp_path / ".sessions" / f"{session.id}.json").exists()


def test_auto_save_persists_session(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    session = manager.new_session()
    session.messages.append({"role": "user", "content": "auto"})

    manager.auto_save(session)

    assert manager.load_session(session.id).messages == session.messages


def test_corrupt_session_file_raises_clear_error(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    session = manager.new_session()
    manager.directory.mkdir(parents=True)
    session_file = manager.directory / f"{session.id}.json"
    session_file.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(SessionError, match="damaged"):
        manager.load_session(session.id)


def test_invalid_session_id_cannot_escape_session_directory(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)

    with pytest.raises(SessionError, match="Invalid Session ID"):
        manager.load_session("../outside")

    with pytest.raises(SessionError, match="Invalid Session ID"):
        manager.delete_session("nested/session")


def test_missing_session_raises_clear_error(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)

    with pytest.raises(SessionError, match="not found"):
        manager.load_session("missing-session")


def test_empty_manager_has_no_latest_session(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)

    assert manager.get_latest_session() is None
    assert manager.list_sessions() == []
