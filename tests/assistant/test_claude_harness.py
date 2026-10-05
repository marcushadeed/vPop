"""Tests for the Claude harness with a scripted client. No API call is made."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from vpop.assistant.claude_harness import (
    ClaudeConversation,
    Spend,
    claude_tools,
    make_client,
    response_cost,
    usage_line,
)
from vpop.assistant.harness import QUERY_FUNCTIONS, AuthError
from vpop.benchmark import run
from vpop.config import ClaudeConfig


@pytest.fixture(scope="module")
def fixture_data_home() -> Iterator[str]:
    with run.fixture_data_dir():
        yield os.environ["XDG_DATA_HOME"]


@pytest.fixture(autouse=True)
def use_fixture_data(
    isolated: None, fixture_data_home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the data directory at the fixture database, not the test's empty one."""
    monkeypatch.setenv("XDG_DATA_HOME", fixture_data_home)


def message(
    content: list[dict[str, Any]],
    stop_reason: str,
    model: str = "claude-sonnet-5-5",
    usage: dict[str, int] | None = None,
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 100, "output_tokens": 10} | (usage or {}),
        }
    )


def tool_use(name: str, **arguments: Any) -> BetaMessage:
    block = {"type": "tool_use", "id": "toolu_1", "name": name, "input": arguments}
    return message([block], "tool_use")


def text(content: str, stop_reason: str = "end_turn", **usage: int) -> BetaMessage:
    return message([{"type": "text", "text": content}], stop_reason, usage=usage)


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


def test_cost_counts_every_kind_of_token() -> None:
    reply = message(
        [],
        "end_turn",
        usage={
            "input_tokens": 1_000,
            "cache_read_input_tokens": 40_000,
            "cache_creation_input_tokens": 2_000,
            "output_tokens": 500,
        },
    )
    # Sonnet 5.5: $2 in, $0.20 cache read, $2.50 cache write, $10 out per million.
    expected = (1_000 * 2 + 40_000 * 0.20 + 2_000 * 2.50 + 500 * 10) / 1_000_000
    assert response_cost(reply) == pytest.approx(expected)


def test_fallback_model_is_priced_at_its_own_rates() -> None:
    opus = message(
        [], "end_turn", model="claude-opus-5-5", usage={"input_tokens": 1_000_000}
    )
    assert response_cost(opus) == pytest.approx(4 + 10 * 20 / 1_000_000)


def test_unknown_model_has_no_price() -> None:
    reply = message([], "end_turn", model="claude-someday-9")
    assert response_cost(reply) is None
    spend = Spend(100, 0, 0, 10, 0.0, frozenset({"claude-someday-9"}))
    line = usage_line(spend, spend)
    assert line.endswith("(no price for claude-someday-9)")
    assert "$" not in line


def test_trace_splits_cached_input() -> None:
    conversation, _ = converse(
        [text("a", cache_read_input_tokens=500, cache_creation_input_tokens=50)]
    )
    conversation.ask("q")
    trace = conversation.trace
    assert (trace.prompt_tokens, trace.cache_read_tokens, trace.cache_write_tokens) == (
        650,
        500,
        50,
    )


def test_verbose_ask_logs_question_and_session_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    replies = [
        text("a", input_tokens=10_000, output_tokens=1_000),
        text("b", input_tokens=20_000, output_tokens=1_000),
    ]
    conversation, _ = converse(replies)
    conversation.verbose = True
    conversation.ask("q1")
    conversation.ask("q2")
    lines = [r.getMessage() for r in caplog.records if "usage:" in r.getMessage()]
    # $0.02 + $0.01 for the first question, $0.04 + $0.01 for the second.
    tokens = "in + 0 cached + 0 cache write, 1,000 out"
    assert lines == [
        f"  usage: 10,000 {tokens} · ~$0.03 (session ~$0.03)",
        f"  usage: 20,000 {tokens} · ~$0.05 (session ~$0.08)",
    ]


def test_missing_credentials_say_where_to_put_a_key(no_credentials: Path) -> None:
    with pytest.raises(AuthError) as error:
        make_client()
    assert str(no_credentials) in str(error.value)
    assert "ant auth login" in str(error.value)


def test_broken_cli_profile_is_an_auth_error(
    monkeypatch: pytest.MonkeyPatch, no_credentials: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_PROFILE", "missing")
    with pytest.raises(AuthError, match="ant auth login"):
        make_client()


def test_key_file_beside_config_is_used(no_credentials: Path) -> None:
    no_credentials.parent.mkdir(parents=True)
    no_credentials.write_text("# my key\nANTHROPIC_API_KEY='sk-ant-test'\n")
    client, source = make_client()
    assert client.api_key == "sk-ant-test"
    assert source == f"ANTHROPIC_API_KEY from {no_credentials}"


def test_exported_key_wins_over_key_file(
    monkeypatch: pytest.MonkeyPatch, no_credentials: Path
) -> None:
    no_credentials.parent.mkdir(parents=True)
    no_credentials.write_text("ANTHROPIC_API_KEY=sk-ant-file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    client, source = make_client()
    assert client.api_key == "sk-ant-env"
    assert source == "ANTHROPIC_API_KEY from the environment"


class RejectingMessages:
    def __init__(self, status: int, error: type[anthropic.APIStatusError]) -> None:
        self.status = status
        self.error = error

    def create(self, **_: Any) -> BetaMessage:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        body = {"type": "error", "error": {"message": "API key is invalid."}}
        response = httpx2.Response(self.status, json=body, request=request)
        raise self.error("rejected", response=response, body=body)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, anthropic.AuthenticationError), (403, anthropic.PermissionDeniedError)],
)
def test_rejected_credentials_raise_auth_error(
    status: int, error: type[anthropic.APIStatusError]
) -> None:
    client = ScriptedClient([])
    client.messages = RejectingMessages(status, error)  # type: ignore[assignment]
    conversation = ClaudeConversation(client=client, verbose=False)  # type: ignore[arg-type]
    with pytest.raises(AuthError, match=f"\\({status}\\): API key is invalid"):
        conversation.ask("hi")
