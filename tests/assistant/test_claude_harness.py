"""Tests for the Claude harness with a scripted client. No API call is made."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import anthropic
import httpx2
import pytest
from anthropic.lib.streaming import ParsedBetaTextEvent
from anthropic.types.beta import (
    BetaMessage,
    BetaRawContentBlockStartEvent,
    BetaTextBlock,
)
from helpers import RecordingListener, message_toolbox

from vpop.assistant.claude_harness import (
    ClaudeConversation,
    Spend,
    claude_tools,
    make_client,
    price_for,
    response_cost,
    usage_line,
)
from vpop.assistant.errors import AuthError
from vpop.assistant.toolbox import Toolbox
from vpop.benchmark import run
from vpop.config import ClaudeConfig


@pytest.fixture(scope="module")
def tools() -> Iterator[Toolbox]:
    with run.fixture_db() as path:
        yield message_toolbox(path)


def message(
    content: list[dict[str, Any]],
    stop_reason: str,
    model: str = "claude-sonnet-5-5",
    usage: dict[str, int] | None = None,
    **extra: Any,
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
        | extra
    )


def tool_use(name: str, **arguments: Any) -> BetaMessage:
    block = {"type": "tool_use", "id": "toolu_1", "name": name, "input": arguments}
    return message([block], "tool_use")


def text(content: str, stop_reason: str = "end_turn", **usage: int) -> BetaMessage:
    return message([{"type": "text", "text": content}], stop_reason, usage=usage)


class ScriptedStream:
    def __init__(self, reply: BetaMessage | BaseException) -> None:
        self.reply = reply

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Iterator[object]:
        """
        The events a real stream yields for the reply: a start event per content block,
        then a text block's text in two pieces.
        """
        if isinstance(self.reply, BaseException):
            raise self.reply
        for index, block in enumerate(self.reply.content):
            start = (
                BetaTextBlock(type="text", text="") if block.type == "text" else block
            )
            yield BetaRawContentBlockStartEvent(
                type="content_block_start", index=index, content_block=start
            )
            if block.type == "text":
                middle = len(block.text) // 2
                first, rest = block.text[:middle], block.text[middle:]
                yield ParsedBetaTextEvent(type="text", text=first, snapshot=first)
                yield ParsedBetaTextEvent(type="text", text=rest, snapshot=block.text)

    def get_final_message(self) -> BetaMessage:
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


class ScriptedMessages:
    def __init__(self, replies: list[BetaMessage | BaseException]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> ScriptedStream:
        # Copy the history: the conversation keeps appending to the same list.
        self.requests.append(kwargs | {"messages": list(kwargs["messages"])})
        return ScriptedStream(self.replies.pop(0))


class ScriptedClient:
    """Stands in for `anthropic.Anthropic`, replaying canned replies."""

    def __init__(self, replies: list[BetaMessage | BaseException]) -> None:
        self.messages = ScriptedMessages(replies)
        self.beta = self


def converse(
    tools: Toolbox, replies: list[BetaMessage | BaseException], **kwargs: Any
) -> tuple[ClaudeConversation, ScriptedMessages]:
    client = ScriptedClient(replies)
    conversation = ClaudeConversation(
        tools,
        client=client,  # type: ignore[arg-type]
        today="2026-09-15",
        **kwargs,
    )
    return conversation, client.messages


def test_tools_match_query_functions(tools: Toolbox) -> None:
    definitions = claude_tools(tools)
    assert [t["name"] for t in definitions] == tools.names
    find: dict[str, Any] = dict(definitions[0]["input_schema"])
    assert find["required"] == ["name_or_number"]
    assert "limit" in find["properties"]


def test_tool_loop_runs_tools_and_returns_answer(tools: Toolbox) -> None:
    conversation, api = converse(
        tools,
        [tool_use("find_threads", name_or_number="Mom"), text("Mom's thread.")],
        settings=ClaudeConfig(model="claude-sonnet-5-5", effort="low"),
    )
    assert conversation.ask("who is mom?") == "Mom's thread."

    first = api.requests[0]
    assert first["model"] == "claude-sonnet-5-5"
    assert first["output_config"] == {"effort": "low"}
    assert first["fallbacks"] == "default"
    assert first["thinking"] == {"type": "adaptive"}
    assert "(Today is Tuesday 2026-09-15.)" in first["messages"][0]["content"]

    tool_results = api.requests[1]["messages"][-1]["content"]
    assert tool_results[0]["tool_use_id"] == "toolu_1"
    assert "Mom" in tool_results[0]["content"]
    assert tool_results[0]["is_error"] is False

    trace = conversation.trace
    assert trace.rounds == 2
    assert [c.name for c in trace.tool_calls] == ["find_threads"]
    assert trace.prompt_tokens == 200


def test_streams_narration_tool_calls_and_answer_in_order(tools: Toolbox) -> None:
    narration = message(
        [
            {"type": "text", "text": "Let me look."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "find_threads",
                "input": {"name_or_number": "Mom"},
            },
        ],
        "tool_use",
    )
    conversation, _ = converse(tools, [narration, text("Mom's thread.")])
    listener = RecordingListener()
    conversation.listener = listener
    assert conversation.ask("who is mom?") == "Mom's thread."
    assert listener.events == [
        "text: Let me look.",
        "tool: find_threads -> thread_key | contact name(s) | messages | first → last",
        "text: Mom's thread.",
    ]


def test_text_blocks_stream_with_a_line_break_between(tools: Toolbox) -> None:
    reply = message(
        [{"type": "text", "text": "First."}, {"type": "text", "text": "Second."}],
        "end_turn",
    )
    conversation, _ = converse(tools, [reply])
    listener = RecordingListener()
    conversation.listener = listener
    assert conversation.ask("q") == "First.\nSecond."
    assert listener.events == ["text: First.\nSecond."]


def test_tool_errors_are_flagged(tools: Toolbox) -> None:
    conversation, api = converse(tools, [tool_use("drop_table"), text("sorry")])
    conversation.ask("q")
    result = api.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "unknown tool" in result["content"]


def test_round_limit(tools: Toolbox) -> None:
    conversation, _ = converse(
        tools, [tool_use("find_threads", name_or_number="x")] * 2, max_rounds=2
    )
    assert "Stopped after 2 rounds" in conversation.ask("q")
    assert conversation.trace.hit_round_limit


def test_refusal_and_max_tokens(tools: Toolbox) -> None:
    refusal = message(
        [], "refusal", stop_details={"type": "refusal", "category": "cyber"}
    )
    conversation, _ = converse(tools, [refusal, text("partial", "max_tokens")])
    assert conversation.ask("q") == (
        "(Claude declined to answer this question; category: cyber)"
    )
    assert "max_tokens" in conversation.ask("q2")


def test_tool_call_cut_off_by_max_tokens_is_not_run(tools: Toolbox) -> None:
    cut = message(
        [
            {"type": "text", "text": "Let me look"},
            {"type": "tool_use", "id": "t", "name": "find_threads", "input": {}},
        ],
        "max_tokens",
    )
    conversation, _ = converse(tools, [cut])
    assert conversation.ask("q").endswith("(Cut off at the max_tokens limit.)")
    assert conversation.trace.tool_calls == []


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


def test_dated_snapshot_ids_are_priced() -> None:
    assert price_for("claude-haiku-4-5-20251001") == price_for("claude-haiku-4-5")
    assert price_for("claude-opus-5") != price_for("claude-opus-5-5")


def test_unknown_model_has_no_price() -> None:
    reply = message([], "end_turn", model="claude-someday-9")
    assert response_cost(reply) is None
    spend = Spend(100, 0, 0, 10, 0.0, frozenset({"claude-someday-9"}))
    line = usage_line(spend, spend)
    assert line.endswith("(no price for claude-someday-9)")
    assert "$" not in line


def test_trace_splits_cached_input(tools: Toolbox) -> None:
    conversation, _ = converse(
        tools, [text("a", cache_read_input_tokens=500, cache_creation_input_tokens=50)]
    )
    conversation.ask("q")
    trace = conversation.trace
    assert (trace.prompt_tokens, trace.cache_read_tokens, trace.cache_write_tokens) == (
        650,
        500,
        50,
    )


def test_ask_logs_question_and_session_cost(
    tools: Toolbox, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    replies = [
        text("a", input_tokens=10_000, output_tokens=1_000),
        text("b", input_tokens=20_000, output_tokens=1_000),
    ]
    conversation, _ = converse(tools, replies)
    conversation.ask("q1")
    conversation.ask("q2")
    lines = [r.getMessage() for r in caplog.records if "usage:" in r.getMessage()]
    # $0.02 + $0.01 for the first question, $0.04 + $0.01 for the second.
    tokens = "in + 0 cached + 0 cache write, 1,000 out"
    assert lines == [
        f"  usage: 10,000 {tokens} · ~$0.03 (session ~$0.03)",
        f"  usage: 20,000 {tokens} · ~$0.05 (session ~$0.08)",
    ]


def test_unpriced_model_is_flagged_on_every_question(
    tools: Toolbox, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    unknown = "claude-someday-9"
    replies = [
        message([{"type": "text", "text": t}], "end_turn", model=unknown) for t in "ab"
    ]
    conversation, _ = converse(tools, replies)
    conversation.ask("q1")
    conversation.ask("q2")
    lines = [r.getMessage() for r in caplog.records if "usage:" in r.getMessage()]
    assert all(line.endswith(f"(no price for {unknown})") for line in lines)
    assert len(lines) == 2


def test_failed_question_is_rolled_back(tools: Toolbox) -> None:
    error = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    conversation, api = converse(
        tools, [tool_use("find_threads", name_or_number="Mom"), error, text("ok")]
    )
    with pytest.raises(anthropic.APIConnectionError):
        conversation.ask("q1")
    assert conversation.messages == []
    assert conversation.ask("q2") == "ok"
    assert len(api.requests[-1]["messages"]) == 1


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

    def stream(self, **_: Any) -> BetaMessage:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        body = {"type": "error", "error": {"message": "API key is invalid."}}
        response = httpx2.Response(self.status, json=body, request=request)
        raise self.error("rejected", response=response, body=body)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, anthropic.AuthenticationError), (403, anthropic.PermissionDeniedError)],
)
def test_rejected_credentials_raise_auth_error(
    tools: Toolbox, status: int, error: type[anthropic.APIStatusError]
) -> None:
    client = ScriptedClient([])
    client.messages = RejectingMessages(status, error)  # type: ignore[assignment]
    conversation = ClaudeConversation(tools, client=client)  # type: ignore[arg-type]
    with pytest.raises(AuthError, match=f"\\({status}\\): API key is invalid"):
        conversation.ask("hi")
