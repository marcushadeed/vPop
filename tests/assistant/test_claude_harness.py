"""Tests for the Claude harness with a scripted client. No API call is made."""

from collections.abc import Iterator
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage

from assistant.benchmark import run
from assistant.claude_harness import ClaudeConversation, claude_tools
from assistant.harness import QUERY_FUNCTIONS
from config import ClaudeConfig


@pytest.fixture(scope="module", autouse=True)
def fixture_db() -> Iterator[None]:
    with run.fixture_data_dir():
        yield


def message(content: list[dict[str, Any]], stop_reason: str) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
    )


def tool_use(name: str, **arguments: Any) -> BetaMessage:
    block = {"type": "tool_use", "id": "toolu_1", "name": name, "input": arguments}
    return message([block], "tool_use")


def text(content: str, stop_reason: str = "end_turn") -> BetaMessage:
    return message([{"type": "text", "text": content}], stop_reason)


class ScriptedMessages:
    def __init__(self, replies: list[BetaMessage]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> BetaMessage:
        # Copy the history: the conversation keeps appending to the same list.
        self.requests.append(kwargs | {"messages": list(kwargs["messages"])})
        return self.replies.pop(0)


class ScriptedClient:
    """Stands in for `anthropic.Anthropic`, replaying canned replies."""

    def __init__(self, replies: list[BetaMessage]) -> None:
        self.messages = ScriptedMessages(replies)
        self.beta = self


def converse(
    replies: list[BetaMessage], **kwargs: Any
) -> tuple[ClaudeConversation, ScriptedMessages]:
    client = ScriptedClient(replies)
    conversation = ClaudeConversation(
        client=client,  # type: ignore[arg-type]
        today="2026-09-15",
        verbose=False,
        **kwargs,
    )
    return conversation, client.messages


def test_tools_match_query_functions() -> None:
    tools = claude_tools()
    assert [t["name"] for t in tools] == list(QUERY_FUNCTIONS)
    find: dict[str, Any] = dict(tools[0]["input_schema"])
    assert find["required"] == ["name_or_number"]
    assert "limit" in find["properties"]


def test_tool_loop_runs_tools_and_returns_answer() -> None:
    conversation, api = converse(
        [tool_use("find_threads", name_or_number="Mom"), text("Mom's thread.")],
        settings=ClaudeConfig(model="claude-sonnet-5-5", effort="low"),
    )
    assert conversation.ask("who is mom?") == "Mom's thread."

    first = api.requests[0]
    assert first["model"] == "claude-sonnet-5-5"
    assert first["output_config"] == {"effort": "low"}
    assert "(Today is Tuesday 2026-09-15.)" in first["messages"][0]["content"]

    tool_results = api.requests[1]["messages"][-1]["content"]
    assert tool_results[0]["tool_use_id"] == "toolu_1"
    assert "Mom" in tool_results[0]["content"]
    assert tool_results[0]["is_error"] is False

    trace = conversation.trace
    assert trace.rounds == 2
    assert [c.name for c in trace.tool_calls] == ["find_threads"]
    assert trace.prompt_tokens == 200


def test_tool_errors_are_flagged() -> None:
    conversation, api = converse([tool_use("drop_table"), text("sorry")])
    conversation.ask("q")
    result = api.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "unknown tool" in result["content"]


def test_round_limit() -> None:
    conversation, _ = converse(
        [tool_use("find_threads", name_or_number="x")] * 2, max_rounds=2
    )
    assert "Stopped after 2 rounds" in conversation.ask("q")
    assert conversation.trace.hit_round_limit


def test_refusal_and_max_tokens() -> None:
    conversation, _ = converse([message([], "refusal"), text("partial", "max_tokens")])
    assert "declined" in conversation.ask("q")
    assert "max_tokens" in conversation.ask("q2")
