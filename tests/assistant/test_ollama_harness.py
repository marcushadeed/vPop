"""Tests for the Ollama harness and the shared tool loop, with a scripted client."""

from pathlib import Path

import pytest
from helpers import (
    RecordingListener,
    ScriptedOllama,
    build_db,
    make_message,
    message_toolbox,
    text_reply,
    tool_reply,
)

from vpop.assistant.ollama_harness import OllamaConversation, ollama_tools
from vpop.assistant.toolbox import Toolbox
from vpop.config import OllamaConfig


@pytest.fixture
def tools(tmp_path: Path) -> Toolbox:
    return message_toolbox(
        build_db(tmp_path / "vpop.db", [make_message(body="Biscuit")])
    )


def converse(
    tools: Toolbox, replies: list[object], **kwargs: object
) -> tuple[OllamaConversation, ScriptedOllama]:
    client = ScriptedOllama(replies)
    conversation = OllamaConversation(
        tools,
        client=client,
        today="2026-09-15",
        **kwargs,  # type: ignore[arg-type]
    )
    return conversation, client


def test_tool_definitions_cover_every_tool(tools: Toolbox) -> None:
    definitions = ollama_tools(tools)
    assert [d["function"]["name"] for d in definitions] == tools.names
    assert all(d["type"] == "function" for d in definitions)


def test_uses_settings_and_today(tools: Toolbox) -> None:
    conversation, client = converse(
        tools,
        [text_reply("hi")],
        settings=OllamaConfig(model="m:1b", num_ctx=4096, think="off"),
    )
    conversation.ask("hello")
    request = client.requests[0]
    assert request["model"] == "m:1b"
    assert request["think"] is False
    assert request["options"] == {"num_ctx": 4096, "temperature": 0}
    assert "(Today is Tuesday 2026-09-15.)" in request["messages"][1]["content"]


def test_tool_loop_runs_tools_and_answers(
    tools: Toolbox, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    conversation, client = converse(
        tools,
        [tool_reply("search_messages", text="biscuit"), text_reply("Biscuit.")],
    )
    assert conversation.ask("dog?") == "Biscuit."
    tool_message = client.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "Biscuit" in tool_message["content"]
    assert "→ search_messages(text='biscuit')" in caplog.text
    assert conversation.trace.rounds == 2


def test_listener_hears_tool_calls_but_no_streamed_text(tools: Toolbox) -> None:
    conversation, _ = converse(
        tools,
        [tool_reply("search_messages", text="biscuit"), text_reply("Biscuit.")],
    )
    listener = RecordingListener()
    conversation.listener = listener
    assert conversation.ask("dog?") == "Biscuit."
    hit = "2026-08-01 09:00:00 | Sam Smith [+12405551234] | Sam Smith | Biscuit"
    assert listener.events == [f"tool: search_messages -> {hit}"]


def test_round_limit(tools: Toolbox) -> None:
    conversation, _ = converse(
        tools, [tool_reply("find_threads", name_or_number="x")] * 2, max_rounds=2
    )
    assert "Stopped after 2 rounds" in conversation.ask("q")
    assert conversation.trace.hit_round_limit


def test_failed_question_is_rolled_back(tools: Toolbox) -> None:
    conversation, client = converse(
        tools,
        [
            tool_reply("find_threads", name_or_number="sam"),
            ConnectionError("Ollama went away"),
            text_reply("fine"),
        ],
    )
    with pytest.raises(ConnectionError):
        conversation.ask("first")
    assert len(conversation.messages) == 1  # just the system prompt
    assert conversation.ask("second") == "fine"
    assert [m["role"] for m in client.requests[-1]["messages"]] == ["system", "user"]


def test_warns_when_context_is_nearly_full(
    tools: Toolbox, caplog: pytest.LogCaptureFixture
) -> None:
    client = ScriptedOllama([text_reply("ok")], prompt_tokens=3900)
    conversation = OllamaConversation(
        tools,
        OllamaConfig(num_ctx=4096),
        client=client,  # type: ignore[arg-type]
    )
    conversation.ask("q")
    assert "3900 of 4096 context tokens" in caplog.text
