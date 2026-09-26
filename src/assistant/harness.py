"""
Answer plain-language questions about the message database with Claude.

The model never sees the database directly. It calls the read-only functions in
`message_queries` as tools and pulls in only the rows it needs.
"""

import functools
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime

import anthropic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError
from anthropic.types.beta import BetaMessage, BetaMessageParam

from assistant import message_queries
from paths import ANTHROPIC_ENV

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
# Tool calls allowed per question before the runner stops.
MAX_ITERATIONS = 25
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You answer questions about the user's own text messages (SMS, MMS and RCS from their Android \
phone, 2020 onward). You can't see the messages directly; use the tools to read them.

The data is one SQLite table, `messages`:
- `timestamp`: local time as text, `YYYY-MM-DD HH:MM:SS`. It sorts and compares as a string and \
works with date() and strftime().
- `direction`: `incoming` or `outgoing`. Outgoing messages were sent by the user ("me").
- `thread_key`: the conversation. A single E.164 number (`+12405551234`) for a 1:1 thread, a \
sorted comma-separated list of numbers for a group, or an `...@rcs.google.com` id for an RCS group.
- `sender`: the number of whoever sent an incoming message; empty for outgoing ones.
- `contact_name`: the contact name(s) the phone showed for the thread. It can be empty or \
`(Unknown)`, and a group lists several names.
- `body`: the message text. Reactions look like `❤️ to “...”` or `Liked “...”`.
- `id`, `was_sent`, `rcs_message_id`: bookkeeping, rarely useful.

How to work:
- When a question names a person, resolve them with find_threads first. A person can have \
more than one thread (a new number, group chats), so look at every match that fits.
- Use search_messages to find candidate messages, then read_thread with `around` to check the \
surrounding conversation before drawing conclusions from a single message.
- Use run_sql for counts, rankings and other aggregates.
- Ground every claim in messages you actually read: cite the timestamp and who said it. If \
the messages don't answer the question, say so plainly rather than guessing.
- Keep the final answer short and direct.
"""


def load_anthropic_env() -> None:
    """
    Load KEY=VALUE pairs from `secrets/anthropic.env`, if present (existing vars win).

    The file is optional: the SDK also finds an exported `ANTHROPIC_API_KEY` or an
    `ant auth login` profile.
    """
    if not ANTHROPIC_ENV.exists():
        return
    for raw_line in ANTHROPIC_ENV.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def as_tool(func: Callable[..., str]):
    """
    Expose a query function as a tool, with its docstring as the tool description.

    Bad input and SQL errors come back to the model as error results it can correct, instead
    of being logged as crashes.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> str:
        try:
            return func(*args, **kwargs)
        except (ValueError, sqlite3.Error) as exc:
            raise ToolError(str(exc)) from exc

    return beta_tool(wrapper)


TOOLS = [
    as_tool(message_queries.find_threads),
    as_tool(message_queries.search_messages),
    as_tool(message_queries.read_thread),
    as_tool(message_queries.run_sql),
]


def answer_text(message: BetaMessage) -> str:
    """Return the text of the final assistant message."""
    return "\n".join(
        block.text for block in message.content if block.type == "text"
    ).strip()


def log_tool_calls(message: BetaMessage) -> None:
    """Print each tool call to stderr so a long answer shows what it's doing."""
    for block in message.content:
        if block.type == "tool_use":
            args = ", ".join(f"{k}={v!r}" for k, v in dict(block.input).items())
            print(f"  → {block.name}({args})", file=sys.stderr)


class Conversation:
    """
    A multi-turn conversation over the database.

    The history is only ever appended to, which keeps the cached prefix valid from one
    request to the next.
    """

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        if client is None:
            load_anthropic_env()
            client = anthropic.Anthropic()
        self.client = client
        self.messages: list[BetaMessageParam] = []

    def ask(self, question: str) -> str:
        """Ask a question and return the answer, keeping the exchange in the history."""
        turn_start = len(self.messages)
        # The date goes in the user turn, not the system prompt, so the system prompt stays
        # byte-identical and cacheable.
        today = datetime.now().astimezone().strftime("%A %Y-%m-%d")
        self.messages.append(
            {"role": "user", "content": f"(Today is {today}.)\n\n{question}"}
        )

        runner = self.client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=list(self.messages),
            cache_control={"type": "ephemeral"},
            fallbacks="default",
            betas=[FALLBACK_BETA],
            max_iterations=MAX_ITERATIONS,
        )
        final: BetaMessage | None = None
        for message in runner:
            final = message
            log_tool_calls(message)
            # The runner keeps its own copy of the history, so mirror it for the next question.
            self.messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)

        if final is None:
            del self.messages[turn_start:]
            return "(no response)"
        if final.stop_reason == "refusal":
            # Drop the refused turn so it doesn't carry into the next question.
            del self.messages[turn_start:]
            return "(The model declined to answer that question.)"
        text = answer_text(final)
        if final.stop_reason == "tool_use":
            text += f"\n\n(Stopped after {MAX_ITERATIONS} tool calls without a final answer.)"
        elif final.stop_reason == "max_tokens":
            text += "\n\n(Answer cut off at the output token limit.)"
        return text or "(empty response)"


def ask(question: str) -> str:
    """Answer a single question."""
    return Conversation().ask(question)


def repl() -> None:
    """Interactive question loop that keeps the conversation history. Exit with Ctrl-D."""
    conversation = Conversation()
    while True:
        try:
            question = input("ask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if question:
            print(conversation.ask(question), end="\n\n")
