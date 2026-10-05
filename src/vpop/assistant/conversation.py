"""
The tool loop both harnesses share: send the conversation, run the tools the model asks for,
send the results back, until it answers or runs out of rounds.

Providers subclass `Conversation` and implement how a question, a model turn and tool results
are represented in their own message history.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from vpop.assistant.prompt import question_message
from vpop.assistant.tools import MessageTools

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """One tool call the model made and what it got back."""

    name: str
    arguments: dict[str, Any]
    result: str

    @property
    def is_error(self) -> bool:
        """Whether the call failed; `MessageTools.call` reports failures as `error: ...`."""
        return self.result.startswith("error:")


@dataclass
class Trace:  # pylint: disable=too-many-instance-attributes
    """What a conversation did: its tool calls, model rounds and token counts."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    rounds: int = 0
    hit_round_limit: bool = False
    # All input tokens, cached or not.
    prompt_tokens: int = 0
    # The parts of `prompt_tokens` read from and written to the prompt cache (Claude only).
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    # Ollama's own timing, in nanoseconds.
    model_ns: int = 0


@dataclass
class PendingCall:
    """A tool call requested in a model turn, not yet run."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Reply:
    """A model turn: either tool calls to run, or (with no calls) the final answer."""

    answer: str = ""
    calls: list[PendingCall] = field(default_factory=list)


class Conversation(ABC):
    """A multi-turn conversation over the message database with some model."""

    def __init__(
        self, tools: MessageTools, *, max_rounds: int, today: str | None
    ) -> None:
        """
        `today` is a `YYYY-MM-DD` date to tell the model instead of the real one, so
        questions like "last month" have a fixed answer.
        """
        self.tools = tools
        self.max_rounds = max_rounds
        self.today = today
        self.trace = Trace()

    @property
    @abstractmethod
    def history(self) -> list[Any]:
        """The provider's message list; a failed question is rolled back out of it."""

    @abstractmethod
    def add_question(self, text: str) -> None:
        """Append a user question to the history."""

    @abstractmethod
    def chat(self) -> Reply:
        """Send the history, append the model's turn to it, and return that turn."""

    @abstractmethod
    def add_results(self, results: list[tuple[PendingCall, str]]) -> None:
        """Append the results of a turn's tool calls to the history."""

    def ask(self, question: str) -> str:
        """
        Ask a question and return the answer, keeping the exchange in the history.

        If anything fails partway (an API error, Ctrl-C), the question's messages are removed
        again, so the history never ends with a tool call that has no result, which would
        break every later request.
        """
        mark = len(self.history)
        try:
            return self.answer(question)
        except BaseException:
            del self.history[mark:]
            raise

    def answer(self, question: str) -> str:
        """Run the tool loop for a question until the model answers or gives up."""
        self.add_question(question_message(question, self.today))
        for _ in range(self.max_rounds):
            reply = self.chat()
            if not reply.calls:
                return reply.answer.strip() or "(empty response)"
            results = []
            for call in reply.calls:
                args = ", ".join(f"{k}={v!r}" for k, v in call.arguments.items())
                log.info("  → %s(%s)", call.name, args)
                result = self.tools.call(call.name, call.arguments)
                self.trace.tool_calls.append(
                    ToolCall(call.name, call.arguments, result)
                )
                results.append((call, result))
            self.add_results(results)
        self.trace.hit_round_limit = True
        return f"(Stopped after {self.max_rounds} rounds of tool calls without a final answer.)"
