"""JSON-backed conversation Session persistence."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_SCHEMA_VERSION = 1
_SESSION_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class SessionError(RuntimeError):
    """Raised when a Session cannot be read, validated, or persisted."""


@dataclass
class Session:
    id: str
    created_at: str
    updated_at: str
    title: str
    messages: list[dict[str, Any]]
    root_dir: Path
    work_dir: Path


@dataclass(frozen=True)
class SessionMeta:
    id: str
    title: str
    created_at: str
    updated_at: str


class SessionManager:
    """Manage JSON Session files and their lightweight index."""

    def __init__(self, directory: str | Path = ".sessions", root_dir: str | Path | None = None) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.root_dir = Path(root_dir).expanduser().resolve() if root_dir is not None else Path.cwd().resolve()

    @property
    def index_path(self) -> Path:
        return self.directory / "index.json"

    def new_session(
        self,
        root_dir: str | Path | None = None,
        work_dir: str | Path | None = None,
    ) -> Session:
        resolved_root = Path(root_dir).expanduser().resolve() if root_dir is not None else self.root_dir
        resolved_work = Path(work_dir).expanduser().resolve() if work_dir is not None else resolved_root
        if not _is_within(resolved_work, resolved_root):
            raise SessionError("work_dir must be inside root_dir")
        now = _timestamp()
        session_id = self._new_id()
        return Session(
            id=session_id,
            created_at=now,
            updated_at=now,
            title="",
            messages=[],
            root_dir=resolved_root,
            work_dir=resolved_work,
        )

    def save_session(self, session: Session) -> None:
        self._validate_session_id(session.id)
        self._validate_session(session)
        self.directory.mkdir(parents=True, exist_ok=True)
        session.title = session.title or _title_from_messages(session.messages)
        session.updated_at = _timestamp()
        payload = self._session_payload(session)
        try:
            serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise SessionError("Session contains data that cannot be serialized as JSON") from exc
        try:
            self._session_path(session.id).write_text(serialized + "\n", encoding="utf-8")
            self._update_index(SessionMeta(session.id, session.title, session.created_at, session.updated_at))
        except OSError as exc:
            raise SessionError("Unable to save Session") from exc

    def auto_save(self, session: Session) -> None:
        """Save a Session after a conversation turn."""
        self.save_session(session)

    def load_session(self, session_id: str) -> Session:
        path = self._session_path(session_id)
        if not path.exists():
            raise SessionError(f"Session not found: {session_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionError(f"Session file is damaged: {session_id}") from exc
        return self._session_from_payload(payload, session_id)

    def get_latest_session(self) -> Session | None:
        sessions = self._session_files()
        if not sessions:
            return None
        latest: Session | None = None
        for path in sessions:
            try:
                session = self.load_session(path.stem)
            except SessionError:
                continue
            if latest is None or session.updated_at > latest.updated_at:
                latest = session
        return latest

    def list_sessions(self) -> list[SessionMeta]:
        metadata: list[SessionMeta] = []
        for path in self._session_files():
            try:
                session = self.load_session(path.stem)
            except SessionError:
                continue
            metadata.append(SessionMeta(session.id, session.title, session.created_at, session.updated_at))
        return sorted(metadata, key=lambda item: item.updated_at, reverse=True)

    def delete_session(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            self._remove_from_index(session_id)
        except OSError as exc:
            raise SessionError(f"Unable to delete Session: {session_id}") from exc
        return True

    def _new_id(self) -> str:
        for _ in range(100):
            candidate = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
            if not self._session_path(candidate).exists():
                return candidate
        raise SessionError("Unable to generate a unique Session ID")

    def _session_path(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        path = self.directory / f"{session_id}.json"
        if path.parent != self.directory:
            raise SessionError("Invalid Session path")
        return path

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id or "/" in session_id or "\\" in session_id:
            raise SessionError("Invalid Session ID")
        if session_id in {".", ".."} or not _SAFE_ID_PATTERN.fullmatch(session_id):
            raise SessionError("Invalid Session ID")

    @staticmethod
    def _validate_session(session: Session) -> None:
        if not isinstance(session.messages, list) or not all(isinstance(message, dict) for message in session.messages):
            raise SessionError("Session messages must be a list of objects")
        if not isinstance(session.root_dir, Path) or not isinstance(session.work_dir, Path):
            raise SessionError("Session directories must be Path values")
        if not _is_within(session.work_dir, session.root_dir):
            raise SessionError("Session work_dir must be inside root_dir")

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": session.id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "title": session.title,
            "root_dir": str(session.root_dir),
            "work_dir": str(session.work_dir),
            "messages": session.messages,
        }

    def _session_from_payload(self, payload: Any, expected_id: str) -> Session:
        if not isinstance(payload, dict):
            raise SessionError(f"Session file is invalid: {expected_id}")
        if payload.get("id") != expected_id:
            raise SessionError(f"Session ID mismatch: {expected_id}")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
            raise SessionError(f"Session messages are invalid: {expected_id}")
        try:
            root_dir = Path(payload.get("root_dir", self.root_dir)).expanduser().resolve()
            work_dir = Path(payload.get("work_dir", root_dir)).expanduser().resolve()
        except (TypeError, ValueError) as exc:
            raise SessionError(f"Session directories are invalid: {expected_id}") from exc
        if not _is_within(work_dir, root_dir):
            raise SessionError(f"Session work_dir is outside root_dir: {expected_id}")
        if not work_dir.exists() or not work_dir.is_dir():
            work_dir = root_dir
        return Session(
            id=expected_id,
            created_at=_required_string(payload, "created_at", expected_id),
            updated_at=_required_string(payload, "updated_at", expected_id),
            title=str(payload.get("title", "")),
            messages=messages,
            root_dir=root_dir,
            work_dir=work_dir,
        )

    def _session_files(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(
            path
            for path in self.directory.glob("*.json")
            if path.name != "index.json" and _SAFE_ID_PATTERN.fullmatch(path.stem)
        )

    def _read_index(self) -> list[dict[str, str]]:
        if not self.index_path.exists():
            return []
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionError("Session index is damaged") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
            raise SessionError("Session index is invalid")
        records = payload["sessions"]
        if not all(isinstance(record, dict) for record in records):
            raise SessionError("Session index is invalid")
        return records

    def _write_index(self, records: list[dict[str, str]]) -> None:
        payload = {"schema_version": SESSION_SCHEMA_VERSION, "sessions": records}
        try:
            self.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise SessionError("Unable to save Session index") from exc

    def _update_index(self, metadata: SessionMeta) -> None:
        records = [record for record in self._read_index() if record.get("id") != metadata.id]
        records.append({
            "id": metadata.id,
            "title": metadata.title,
            "created_at": metadata.created_at,
            "updated_at": metadata.updated_at,
        })
        records.sort(key=lambda record: str(record.get("updated_at", "")), reverse=True)
        self._write_index(records)

    def _remove_from_index(self, session_id: str) -> None:
        if not self.index_path.exists():
            return
        records = [record for record in self._read_index() if record.get("id") != session_id]
        self._write_index(records)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _title_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"][:30]
    return ""


def _required_string(payload: dict[str, Any], key: str, session_id: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SessionError(f"Session field '{key}' is invalid: {session_id}")
    return value
