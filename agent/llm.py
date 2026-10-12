"""OpenAI-compatible Chat Completions client."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from openai import OpenAI

from .config import AgentConfig


class LLMError(RuntimeError):
    """Raised when an LLM request cannot be made or normalized."""


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


class LLMClient:
    """Send non-streaming and streaming Chat Completions requests."""

    def __init__(self, config: AgentConfig, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            api_key = self.config.llm.api_key
            if not api_key:
                raise LLMError(
                    "AGENT_API_KEY is not configured; set it in the environment or .env before calling the model"
                )
            try:
                self._client = OpenAI(
                    api_key=api_key,
                    base_url=self.config.llm.base_url,
                    timeout=self.config.llm.timeout,
                    max_retries=self.config.llm.max_retries,
                )
            except Exception as exc:
                raise LLMError("Unable to initialize the OpenAI-compatible client") from exc
        return self._client

    def _request(self, messages: Sequence[dict[str, Any]], tools: list[dict[str, Any]] | None, *, stream: bool) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.config.llm.model,
            "messages": list(messages),
            "temperature": self.config.llm.temperature,
            "stream": stream,
        }
        if tools is not None:
            request["tools"] = tools
        return request

    def chat(self, messages: Sequence[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Make one non-streaming request and return a JSON-compatible assistant message."""
        if not self.config.llm.api_key:
            raise LLMError(
                "AGENT_API_KEY is not configured; set it in the environment or .env before calling the model"
            )
        try:
            response = self._get_client().chat.completions.create(**self._request(messages, tools, stream=False))
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError("OpenAI-compatible Chat Completions request failed") from exc
        return self.normalize_response(response)

    def stream_chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Stream deltas while returning one complete assistant message."""
        if not self.config.llm.api_key:
            raise LLMError(
                "AGENT_API_KEY is not configured; set it in the environment or .env before calling the model"
            )
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        try:
            chunks = self._get_client().chat.completions.create(**self._request(messages, tools, stream=True))
            for chunk in chunks:
                choices = _get(chunk, "choices") or []
                if not choices:
                    continue
                delta = _get(choices[0], "delta")
                if delta is None:
                    continue
                content = _get(delta, "content")
                if content:
                    content_parts.append(content)
                    if on_content_delta is not None:
                        on_content_delta(content)
                for raw_call in _get(delta, "tool_calls") or []:
                    index = _get(raw_call, "index", 0)
                    if not isinstance(index, int):
                        raise LLMError("Streaming response returned an invalid tool-call index")
                    assembled = tool_calls.setdefault(
                        index,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    call_id = _get(raw_call, "id")
                    call_type = _get(raw_call, "type")
                    function = _get(raw_call, "function")
                    name = _get(function, "name") if function is not None else None
                    arguments = _get(function, "arguments") if function is not None else None
                    if call_id:
                        assembled["id"] += call_id
                    if call_type:
                        assembled["type"] = call_type
                    if name:
                        assembled["function"]["name"] += name
                    if arguments:
                        assembled["function"]["arguments"] += arguments
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError("OpenAI-compatible streaming request failed") from exc

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
        if tool_calls:
            message["tool_calls"] = self.parse_tool_calls([tool_calls[index] for index in sorted(tool_calls)])
        return message

    @classmethod
    def normalize_response(cls, response: Any) -> dict[str, Any]:
        """Convert an SDK response or equivalent mapping to an assistant message."""
        choices = _get(response, "choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
            raise LLMError("Chat Completions response did not contain a choice")

        message = _get(choices[0], "message")
        if message is None:
            raise LLMError("Chat Completions response did not contain an assistant message")

        content = _get(message, "content")
        normalized: dict[str, Any] = {"role": "assistant", "content": content}
        raw_tool_calls = _get(message, "tool_calls")
        if raw_tool_calls is not None:
            normalized["tool_calls"] = cls.parse_tool_calls(raw_tool_calls)
        return normalized

    @staticmethod
    def parse_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]]:
        """Normalize SDK tool-call objects for later Agent Loop consumption."""
        if not isinstance(raw_tool_calls, Sequence) or isinstance(raw_tool_calls, (str, bytes)):
            raise LLMError("Chat Completions returned malformed tool_calls")

        tool_calls: list[dict[str, Any]] = []
        for raw_call in raw_tool_calls:
            call_id = _get(raw_call, "id")
            call_type = _get(raw_call, "type", "function")
            function = _get(raw_call, "function")
            name = _get(function, "name") if function is not None else None
            arguments = _get(function, "arguments") if function is not None else None
            if not all(isinstance(value, str) and value for value in (call_id, call_type, name)):
                raise LLMError("Chat Completions returned an invalid tool call")
            if not isinstance(arguments, str):
                raise LLMError("Chat Completions returned invalid tool-call arguments")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": call_type,
                    "function": {"name": name, "arguments": arguments},
                }
            )
        return tool_calls
