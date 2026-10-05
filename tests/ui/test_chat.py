"""Tests for the chat loop and a question's progress, with a scripted conversation."""

import io
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from helpers import build_db, make_message, message_toolbox
from rich.console import Console

from vpop.assistant.conversation import Conversation, ToolCall
from vpop.assistant.errors import AuthError
from vpop.assistant.toolbox import Toolbox
from vpop.config import Config
from vpop.ui import render
from vpop.ui.chat import Chat, ask, routed_logs

HIT = "2026-08-01 09:00:00 | Sam Smith [+12405551234] | Sam Smith | Biscuit"
DOG_SEARCH = ToolCall("search_messages", {"text": "dog"}, HIT)


class Streamed(str):
    """A script step: text the model streams."""


class Scripted(Conversation):
    """
    A conversation that plays a script per question: `Streamed` text and `ToolCall`s go to
    the listener, an exception is raised, and a plain string is the answer.
    """

    def __init__(self, tools: Toolbox, *scripts: list[Any]) -> None:
        super().__init__(tools, max_rounds=1, today=None)
        self.scripts = list(scripts)
        self.asked: list[str] = []

    @property
    def history(self) -> list[Any]:
        return []

    def add_question(self, text: str) -> None: ...
    def chat(self) -> Any: ...
    def add_results(self, results: Any) -> None: ...

    def ask(self, question: str) -> str:
        self.asked.append(question)
        for step in self.scripts.pop(0):
            if isinstance(step, BaseException):
                raise step
            if isinstance(step, ToolCall):
                self.listener.tool_call(step)
            elif isinstance(step, Streamed):
                self.listener.text(step)
            else:
                return str(step)
        raise AssertionError("script without an answer")


@pytest.fixture
def tools(tmp_path: Path) -> Toolbox:
    return message_toolbox(
        build_db(
            tmp_path / "vpop.db",
            [
                make_message(body="Biscuit", timestamp="2026-08-01 09:00:00"),
                make_message(body="woof", timestamp="2026-08-02 10:00:00"),
            ],
        )
    )


def make_console() -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    console = render.make_console(
        file=out, width=80, color_system=None, force_terminal=False
    )
    return console, out


def lines_of(out: io.StringIO) -> list[str]:
    return [line.rstrip() for line in out.getvalue().splitlines()]


def reader(lines: list[str]) -> Callable[[], str]:
    """Stands in for the editor: returns `lines` in turn, then quits."""
    remaining = iter(lines)

    def read() -> str:
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    return read


def chat_with(
    conversation: Conversation,
    lines: list[str],
    new_conversation: Callable[[], Conversation] | None = None,
) -> list[str]:
    """Run a chat over the typed `lines` and return what it printed."""
    console, out = make_console()
    Chat(
        conversation,
        Config(),
        new_conversation or (lambda: conversation),
        console,
        read=reader(lines),
    ).run()
    return lines_of(out)


def test_chat_opens_with_the_database_summary(tools: Toolbox) -> None:
    printed = chat_with(Scripted(tools), [])
    assert printed[0] == "✻ vPop · 2 messages, newest 2026-08-02"


def test_question_shows_tool_calls_then_the_answer(tools: Toolbox) -> None:
    conversation = Scripted(tools, [DOG_SEARCH, "Sam's dog is **Biscuit**."])
    printed = chat_with(conversation, ["what's sam's dog called?"])
    start = printed.index('● search_messages(text: "dog")')
    assert printed[start - 1 : start + 5] == [
        "",
        '● search_messages(text: "dog")',
        "  ⎿  1 message",
        "",
        "● Sam's dog is Biscuit.",
        "  ✻ 0s",
    ]
    assert conversation.asked == ["what's sam's dog called?"]


def test_streamed_narration_stays_and_the_answer_replaces_the_preview(
    tools: Toolbox,
) -> None:
    conversation = Scripted(
        tools,
        [
            Streamed("Let me look."),
            DOG_SEARCH,
            Streamed("Sam's dog is Bisc"),
            "(Claude declined to answer this question.)",
        ],
    )
    printed = chat_with(conversation, ["q"])
    narration = printed.index("● Let me look.")
    assert printed.index('● search_messages(text: "dog")') > narration
    assert "● (Claude declined to answer this question.)" in printed
    assert not any("Bisc" in line for line in printed)


def test_ctrl_c_ends_only_the_question(tools: Toolbox) -> None:
    conversation = Scripted(
        tools, [Streamed("Partly"), KeyboardInterrupt()], ["Second answer."]
    )
    printed = chat_with(conversation, ["first", "second"])
    partial = printed.index("● Partly")
    assert printed[partial + 1] == "  ⎿  Interrupted"
    assert "● Second answer." in printed
    assert conversation.asked == ["first", "second"]


def test_backend_errors_are_shown_and_the_chat_goes_on(tools: Toolbox) -> None:
    conversation = Scripted(
        tools, [ConnectionError("Failed to connect to Ollama.")], ["ok"]
    )
    printed = chat_with(conversation, ["first", "second"])
    assert (
        "error: Failed to connect to Ollama. Start Ollama with: ollama serve" in printed
    )
    assert "● ok" in printed


def test_rejected_credentials_end_the_chat(tools: Toolbox) -> None:
    conversation = Scripted(tools, [AuthError("rejected")], ["never"])
    with pytest.raises(AuthError):
        chat_with(conversation, ["first", "second"])
    assert conversation.asked == ["first"]


def test_unexpected_errors_surface(tools: Toolbox) -> None:
    with pytest.raises(ZeroDivisionError):
        chat_with(Scripted(tools, [ZeroDivisionError()]), ["q"])


def test_help_lists_the_commands(tools: Toolbox) -> None:
    printed = chat_with(Scripted(tools), ["/help"])
    assert any(line.startswith("  /new") for line in printed)
    assert any("alt+enter" in line for line in printed)


def test_unknown_command_points_to_help(tools: Toolbox) -> None:
    printed = chat_with(Scripted(tools), ["/nope"])
    assert "  unknown command /nope; /help lists them" in printed


@pytest.mark.parametrize("command", ["/new", "/clear"])
def test_new_starts_a_fresh_conversation(tools: Toolbox, command: str) -> None:
    first, second = Scripted(tools), Scripted(tools, ["fresh answer"])
    printed = chat_with(first, [command, "q"], new_conversation=lambda: second)
    assert "  Started a new conversation." in printed
    assert (first.asked, second.asked) == ([], ["q"])


@pytest.mark.parametrize("command", ["/exit", "/quit"])
def test_exit_ends_the_chat(tools: Toolbox, command: str) -> None:
    conversation = Scripted(tools, ["never"])
    chat_with(conversation, [command, "q"])
    assert conversation.asked == []


class Priced(Scripted):
    """A scripted conversation with Claude's cost estimate; each question adds $0.03."""

    def __init__(self, tools: Toolbox, *scripts: list[Any]) -> None:
        super().__init__(tools, *scripts)
        self.cost = 0.10
        self.unpriced: set[str] = set()

    def ask(self, question: str) -> str:
        self.cost += 0.03
        return super().ask(question)


def status_of(conversation: Conversation) -> tuple[str, str]:
    console, _ = make_console()
    return Chat(
        conversation, Config(), lambda: conversation, console, read=reader([])
    ).status()


def test_status_shows_the_model_and_the_tokens_used(tools: Toolbox) -> None:
    conversation = Scripted(tools)
    assert status_of(conversation) == ("qwen3:8b · local", "")
    conversation.trace.prompt_tokens, conversation.trace.output_tokens = 3_100, 780
    assert status_of(conversation) == ("qwen3:8b · local", "↑3.1k ↓780")


def test_status_adds_the_cost_unless_a_model_had_no_price(tools: Toolbox) -> None:
    conversation = Priced(tools)
    conversation.trace.prompt_tokens, conversation.trace.output_tokens = 3_100, 780
    assert status_of(conversation)[1] == "↑3.1k ↓780 · $0.10"
    conversation.unpriced.add("claude-someday-9")
    assert status_of(conversation)[1] == "↑3.1k ↓780"


def test_stats_show_what_the_question_cost(tools: Toolbox) -> None:
    console, out = make_console()
    ask(Priced(tools, ["answer"]), "q", console, after_question=False)
    assert lines_of(out) == ["● answer", "  ✻ 0s · ~$0.03"]


def test_one_question_without_the_chat_starts_straight_away(
    tools: Toolbox,
) -> None:
    console, out = make_console()
    ask(Scripted(tools, [DOG_SEARCH, "Biscuit."]), "q", console, after_question=False)
    assert lines_of(out)[0] == '● search_messages(text: "dog")'


def test_logs_print_through_the_console_without_the_assistants_progress() -> None:
    logger = logging.getLogger("vpop")
    logger.setLevel(logging.INFO)
    handlers = logger.handlers[:]
    console, out = make_console()
    with routed_logs(console):
        logging.getLogger("vpop.assistant.ollama_harness").warning("context is full")
        logging.getLogger("vpop.assistant.conversation").info("  → find_threads()")
        logging.getLogger("vpop.config").info("created a default config")
    assert lines_of(out) == ["warning: context is full", "created a default config"]
    assert logger.handlers == handlers
