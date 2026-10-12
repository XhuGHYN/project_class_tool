from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.config import AgentConfig, LLMConfig
from agent.llm import LLMClient, LLMError


def make_config(api_key: str | None = "test-key") -> AgentConfig:
    return AgentConfig(llm=LLMConfig(api_key=api_key))


def test_chat_sends_non_streaming_request_and_normalizes_text() -> None:
    sdk_client = Mock()
    sdk_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Hello", tool_calls=None))]
    )
    llm = LLMClient(make_config(), client=sdk_client)

    result = llm.chat([{"role": "user", "content": "Hi"}])

    assert result == {"role": "assistant", "content": "Hello"}
    sdk_client.chat.completions.create.assert_called_once_with(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hi"}],
        temperature=0.7,
        stream=False,
    )


def test_chat_passes_tools_when_supplied() -> None:
    sdk_client = Mock()
    sdk_client.chat.completions.create.return_value = {
        "choices": [{"message": {"content": "ready", "tool_calls": None}}]
    }
    tools = [{"type": "function", "function": {"name": "example"}}]

    result = LLMClient(make_config(), client=sdk_client).chat([], tools=tools)

    assert result["content"] == "ready"
    assert sdk_client.chat.completions.create.call_args.kwargs["tools"] == tools
    assert sdk_client.chat.completions.create.call_args.kwargs["stream"] is False


def test_normalize_response_preserves_tool_calls() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                        }
                    ],
                }
            }
        ]
    }

    assert LLMClient.normalize_response(response) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
            }
        ],
    }


def test_stream_chat_emits_text_and_reassembles_tool_calls() -> None:
    sdk_client = Mock()
    sdk_client.chat.completions.create.return_value = iter(
        [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hi ", tool_calls=None))]),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="there",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-",
                                    type="function",
                                    function=SimpleNamespace(name="read_", arguments='{\"path\":'),
                                )
                            ],
                        )
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="1",
                                    type=None,
                                    function=SimpleNamespace(name="file", arguments='\"a.txt\"}'),
                                )
                            ],
                        )
                    )
                ]
            ),
        ]
    )
    deltas: list[str] = []

    result = LLMClient(make_config(), client=sdk_client).stream_chat(
        [{"role": "user", "content": "read"}],
        tools=[],
        on_content_delta=deltas.append,
    )

    assert deltas == ["Hi ", "there"]
    assert result == {
        "role": "assistant",
        "content": "Hi there",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
            }
        ],
    }
    assert sdk_client.chat.completions.create.call_args.kwargs["stream"] is True


    sdk_client = Mock()

    with pytest.raises(LLMError, match="AGENT_API_KEY"):
        LLMClient(make_config(None), client=sdk_client).chat([])

    sdk_client.chat.completions.create.assert_not_called()


def test_empty_response_is_reported() -> None:
    with pytest.raises(LLMError, match="did not contain a choice"):
        LLMClient.normalize_response({"choices": []})


def test_malformed_tool_call_is_reported() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"id": "missing-function", "type": "function"}],
                }
            }
        ]
    }

    with pytest.raises(LLMError, match="invalid tool call"):
        LLMClient.normalize_response(response)


def test_api_error_is_wrapped_without_echoing_secret() -> None:
    sdk_client = Mock()
    sdk_client.chat.completions.create.side_effect = RuntimeError("test-key leaked")

    with pytest.raises(LLMError, match="request failed") as error:
        LLMClient(make_config(), client=sdk_client).chat([])

    assert "test-key" not in str(error.value)
