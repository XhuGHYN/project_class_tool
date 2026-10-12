"""Command-line entry point for the teaching Agent."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent.cli import CLI
from agent.config import AgentConfig, ConfigError, load_config
from agent.llm import LLMClient
from agent.loop import AgentLoop
from agent.session import Session, SessionError, SessionManager
from agent.tools import ToolContext


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="教学版 Python Agent")
    parser.add_argument("--message", help="执行一条消息后退出")
    parser.add_argument("--resume", action="store_true", help="恢复最近一次 Session")
    parser.add_argument("--session", help="加载指定 Session ID")
    parser.add_argument("--config", help="指定 YAML 配置文件")
    parser.add_argument("--no-stream", action="store_true", help="禁用流式输出")
    return parser


def _resolve_session(args: argparse.Namespace, manager: SessionManager, config: AgentConfig) -> tuple[Session, str | None]:
    if args.session:
        try:
            session = manager.load_session(args.session)
            _validate_session_root(session, config)
            return session, None
        except SessionError as exc:
            raise SessionError(f"无法加载指定 Session: {exc}") from exc
    if args.resume:
        session = manager.get_latest_session()
        if session is None:
            return manager.new_session(root_dir=config.work_dir, work_dir=config.work_dir), "没有可恢复的历史 Session，已创建新会话。"
        _validate_session_root(session, config)
        return session, None
    return manager.new_session(root_dir=config.work_dir, work_dir=config.work_dir), None


def _validate_session_root(session: Session, config: AgentConfig) -> None:
    configured_root = config.work_dir.resolve()
    if session.root_dir.resolve() != configured_root:
        raise SessionError("Session root directory differs from the configured work directory")
    if not session.root_dir.exists() or not session.root_dir.is_dir():
        raise SessionError("Session root directory does not exist")
    if not session.work_dir.exists() or not session.work_dir.is_dir():
        raise SessionError("Session current work directory does not exist")
    try:
        session.work_dir.resolve().relative_to(session.root_dir.resolve())
    except ValueError as exc:
        raise SessionError("Session current work directory is outside the allowed root") from exc


def _build_cli(args: argparse.Namespace, config: AgentConfig) -> CLI:
    manager = SessionManager(config.session_dir, root_dir=config.work_dir)
    session, notice = _resolve_session(args, manager, config)
    context = ToolContext(
        work_dir=session.work_dir,
        root_dir=session.root_dir,
        session_id=session.id,
        config=config,
    )
    llm = LLMClient(config)
    loop = AgentLoop(llm, context, messages=session.messages, max_iterations=config.max_iterations)
    return CLI(
        loop,
        session,
        manager,
        config,
        stream=config.streaming and not args.no_stream,
        startup_notice=notice,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.resume and args.session:
        parser.error("--resume 与 --session 不能同时使用")
    try:
        config = load_config(args.config)
        cli = _build_cli(args, config)
        if args.message is not None:
            return 0 if cli.run_message(args.message) else 1
        return cli.run()
    except (ConfigError, SessionError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
