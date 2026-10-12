"""Agent Loop for non-streaming and streaming Function Calling conversations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .tools import ToolContext, execute_tool, get_tool_schemas

MAX_ITERATIONS = 10
MAX_ITERATIONS_WARNING = "[警告] Agent Loop 已达到最大迭代次数（10），未获得最终回答。"


class AgentLoopError(RuntimeError):
    """Raised when the model cannot produce a usable assistant message."""


class AgentLoop:
    """Coordinate model responses and sequential tool execution."""

    def __init__(
        self,
        llm: Any,
        context: ToolContext,
        messages: list[dict[str, Any]] | None = None,
        max_iterations: int = MAX_ITERATIONS,
    ) -> None:
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
            raise ValueError("max_iterations must be an integer")
        if not 1 <= max_iterations <= MAX_ITERATIONS:
            raise ValueError(f"max_iterations must be between 1 and {MAX_ITERATIONS}")
        self.llm = llm
        self.context = context
        self.messages = messages if messages is not None else []
        self.max_iterations = max_iterations
        self.requests_made = 0

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        return get_tool_schemas()

    def run(
        self,
        user_input: str,
        on_tool_call: Callable[[str, str], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
    ) -> str:
        """Run one user turn using a complete non-streaming response per request."""
        return self._run(
            user_input,
            self.llm.chat,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
        )

    def stream_run(
        self,
        user_input: str,
        on_text: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, str], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
    ) -> str:
        """Run one turn through the LLM streaming interface."""
        stream_chat = getattr(self.llm, "stream_chat", None)
        if not callable(stream_chat):
            raise AgentLoopError("当前模型客户端不支持真实流式输出")

        def request(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
            return stream_chat(messages, tools=tools, on_content_delta=on_text)

        return self._run(user_input, request, on_tool_call=on_tool_call, on_tool_result=on_tool_result)

    def _run(
        self,
        user_input: str,
        request: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]],
        *,
        on_tool_call: Callable[[str, str], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
    ) -> str:
        if not isinstance(user_input, str):
            raise ValueError("user_input must be a string")
        self._ensure_system_prompt()
        self.messages.append({"role": "user", "content": user_input})
        self.requests_made = 0

        for _ in range(self.max_iterations):
            self.requests_made += 1
            try:
                assistant = request(self.messages, self.tool_schemas)
            except AgentLoopError:
                raise
            except Exception as exc:
                raise AgentLoopError("模型请求失败") from exc

            assistant_message = self._normalize_assistant(assistant)
            self.messages.append(assistant_message)
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                return self._content_text(assistant_message.get("content"))

            for index, tool_call in enumerate(tool_calls):
                name, arguments = self._tool_call_details(tool_call)
                if on_tool_call is not None:
                    on_tool_call(name, arguments)
                result_message = self._run_tool_call(tool_call, index)
                self.messages.append(result_message)
                if on_tool_result is not None:
                    on_tool_result(result_message["tool_call_id"], result_message["content"])

        return MAX_ITERATIONS_WARNING

    def _ensure_system_prompt(self) -> None:
        config = getattr(self.context, "config", None)
        prompt = getattr(config, "system_prompt", "") if config is not None else ""
        if prompt and not any(message.get("role") == "system" for message in self.messages):
            prompt = prompt.format(work_dir=self.context.work_dir)
            self.messages.insert(0, {"role": "system", "content": prompt})

    @staticmethod
    def _content_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        return str(content)

    @staticmethod
    def _normalize_assistant(assistant: Any) -> dict[str, Any]:
        if not isinstance(assistant, Mapping):
            raise AgentLoopError("模型返回的 assistant 消息不是对象")
        if assistant.get("role", "assistant") != "assistant":
            raise AgentLoopError("模型返回的消息不是 assistant 消息")
        message = dict(assistant)
        message["role"] = "assistant"
        if "content" not in message:
            message["content"] = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls is None:
            message.pop("tool_calls", None)
        elif not isinstance(raw_tool_calls, Sequence) or isinstance(raw_tool_calls, (str, bytes)):
            raise AgentLoopError("模型返回的 tool_calls 格式无效")
        else:
            message["tool_calls"] = [dict(call) if isinstance(call, Mapping) else call for call in raw_tool_calls]
        return message

    def _run_tool_call(self, tool_call: Any, index: int) -> dict[str, str]:
        call_id = self._tool_call_value(tool_call, "id") or f"invalid-call-{self.requests_made}-{index}"
        if not isinstance(tool_call, Mapping):
            return self._tool_error(call_id, "工具调用格式无效")

        function = tool_call.get("function")
        if not isinstance(function, Mapping):
            return self._tool_error(call_id, "工具调用缺少 function")
        name = function.get("name")
        arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not name:
            return self._tool_error(call_id, "工具调用缺少工具名称")
        if not isinstance(arguments, str):
            return self._tool_error(call_id, "工具 arguments 必须是 JSON 字符串")

        result = execute_tool(name, arguments, self.context)
        return {"role": "tool", "tool_call_id": str(call_id), "content": result}

    @staticmethod
    def _tool_call_details(tool_call: Any) -> tuple[str, str]:
        if not isinstance(tool_call, Mapping):
            return "<invalid>", ""
        function = tool_call.get("function")
        if not isinstance(function, Mapping):
            return "<invalid>", ""
        return str(function.get("name") or "<invalid>"), str(function.get("arguments") or "")

    @staticmethod
    def _tool_call_value(tool_call: Any, key: str) -> Any:
        if isinstance(tool_call, Mapping):
            return tool_call.get(key)
        return getattr(tool_call, key, None)

    @staticmethod
    def _tool_error(call_id: str, message: str) -> dict[str, str]:
        return {"role": "tool", "tool_call_id": str(call_id), "content": f"[错误] {message}"}
