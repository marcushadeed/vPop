"""Builders shared by the tests."""

from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ollama

from vpop import db
from vpop.assistant.conversation import Listener, ToolCall
from vpop.assistant.sql_tools import SqlTools
from vpop.assistant.toolbox import Toolbox
from vpop.sources.android_messages.model import Direction, Message
from vpop.sources.android_messages.store import insert_messages
from vpop.sources.android_messages.tools import MessageTools

SAM = "+12405551234"


def epoch(timestamp: str) -> int:
    """Epoch milliseconds for a `YYYY-MM-DD HH:MM:SS` string read as UTC."""
    return int(datetime.fromisoformat(timestamp).replace(tzinfo=UTC).timestamp() * 1000)


def make_message(**overrides: Any) -> Message:
    """An incoming message from Sam, with `epoch_ms` following `timestamp` unless given."""
    fields: dict[str, Any] = {
        "direction": Direction.INCOMING,
        "was_sent": True,
        "thread_key": SAM,
        "sender": SAM,
        "contact_name": "Sam Smith",
        "body": "hello",
        "timestamp": "2026-08-01 09:00:00",
    }
    fields.update(overrides)
    fields.setdefault("epoch_ms", epoch(fields["timestamp"]))
    return Message(**fields)


def outgoing(**overrides: Any) -> Message:
    """A message the user sent to Sam."""
    return make_message(direction=Direction.OUTGOING, sender="", **overrides)


def build_db(path: Path, messages: Iterable[Message]) -> Path:
    """A database at `path` holding `messages`."""
    with closing(db.connect(path)) as conn:
        insert_messages(conn, messages)
    return path


def message_toolbox(path: Path) -> Toolbox:
    """The tools a messages-only setup gets: the message tools and run_sql."""
    return Toolbox([MessageTools(path), SqlTools(path)], db=path)


class ScriptedOllama:
    """Stands in for `ollama.Client`, replaying canned replies and recording requests."""

    def __init__(self, replies: list[Any], prompt_tokens: int = 100) -> None:
        self.replies = replies
        self.prompt_tokens = prompt_tokens
        self.requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> ollama.ChatResponse:
        self.requests.append(kwargs | {"messages": list(kwargs["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return ollama.ChatResponse(
            message=reply,
            prompt_eval_count=self.prompt_tokens,
            eval_count=10,
            total_duration=5,
        )


def tool_reply(name: str, **arguments: Any) -> ollama.Message:
    """An Ollama turn calling one tool."""
    call = ollama.Message.ToolCall(
        function=ollama.Message.ToolCall.Function(name=name, arguments=arguments)
    )
    return ollama.Message(role="assistant", tool_calls=[call])


def text_reply(content: str) -> ollama.Message:
    """An Ollama turn answering in text."""
    return ollama.Message(role="assistant", content=content)


class RecordingListener(Listener):
    """
    Records what a conversation reports as it goes: streamed text, with consecutive pieces
    joined, and each tool call with the first line of its result.
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    def text(self, delta: str) -> None:
        if self.events and self.events[-1].startswith("text: "):
            self.events[-1] += delta
        else:
            self.events.append(f"text: {delta}")

    def tool_call(self, call: ToolCall) -> None:
        self.events.append(f"tool: {call.name} -> {call.result.splitlines()[0]}")
