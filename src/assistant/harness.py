"""
Answer plain-language questions about the message database with a local Ollama model.

Nothing leaves the machine. The model never sees the database directly: it calls the
read-only functions in `message_queries` as tools and pulls in only the rows it needs.
"""

import inspect
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import ollama
from ollama._utils import convert_function_to_tool

from assistant import message_queries

# Any Ollama model with tool support works; override with VPOP_OLLAMA_MODEL.
MODEL = os.environ.get("VPOP_OLLAMA_MODEL", "llama3.1:8b")
# Context window in tokens. Ollama's default is small, and a few tool results fill it quickly.
NUM_CTX = int(os.environ.get("VPOP_OLLAMA_NUM_CTX", "16384"))
# Tool-call rounds allowed per question before giving up.
MAX_ROUNDS = 12

SYSTEM_PROMPT = """\
You answer questions about the user's own text messages (SMS, MMS and RCS from their Android \
phone, 2020 onward). You can't see the messages directly; use the tools to read them.

The data is one SQLite table, `messages`:
- `timestamp`: local time as text, `YYYY-MM-DD HH:MM:SS`. It sorts and compares as a string and \
works with date() and strftime().
- `direction`: `incoming` or `outgoing`. Outgoing messages were sent by the user ("me").
- `thread_key`: the conversation, and the column to group by for anything "per person". A \
single E.164 number (`+1` then ten digits) for a 1:1 thread, a sorted comma-separated list of \
numbers for a group, or an `...@rcs.google.com` id for an RCS group.
- `sender`: the number of whoever sent an incoming message; always empty for outgoing ones, \
so never group outgoing messages by sender.
- `contact_name`: the contact name(s) the phone showed for the thread. It can be empty or \
`(Unknown)`, and a group lists several names.
- `body`: the message text. Reactions look like `❤️ to “...”` or `Liked “...”`.
- `id`, `was_sent`, `rcs_message_id`: bookkeeping, rarely useful.

How to work:
- When a question names a person, resolve them with find_threads first. A person can have \
more than one thread (a new number, group chats), so look at every match that fits.
- Use search_messages to find candidate messages, then read_thread with `around` to check the \
surrounding conversation before drawing conclusions from a single message.
- Use run_sql for counts, rankings and other aggregates. Examples:
  - Who I texted most in 2025: `SELECT thread_key, MAX(contact_name) AS name, COUNT(*) AS n \
FROM messages WHERE timestamp >= '2025-01-01' AND timestamp < '2026-01-01' GROUP BY thread_key \
ORDER BY n DESC LIMIT 10`
  - Messages per month in one thread: `SELECT strftime('%Y-%m', timestamp) AS month, COUNT(*) \
FROM messages WHERE thread_key = '<thread_key>' GROUP BY month ORDER BY month`
- Only report names, numbers and messages that appear in a tool result. Never invent them.
- Ground every claim in messages you actually read: cite the timestamp and who said it. If \
the messages don't answer the question, say so plainly rather than guessing.
- Keep the final answer short and direct.
"""

QUERY_FUNCTIONS: dict[str, Callable[..., str]] = {
    func.__name__: func
    for func in (
        message_queries.find_threads,
        message_queries.search_messages,
        message_queries.read_thread,
        message_queries.run_sql,
    )
}


def tool_schema(func: Callable[..., str]) -> ollama.Tool:
    """
    Build a tool definition from a query function's signature and docstring.

    Ollama's converter marks every non-Optional argument as required, including ones with
    defaults like `limit`, which pushes the model to fill them in on every call. Only arguments
    without a default are required here.
    """
    tool = convert_function_to_tool(func)
    assert tool.function is not None and tool.function.parameters is not None
    tool.function.parameters.required = [
        name
        for name, param in inspect.signature(func).parameters.items()
        if param.default is inspect.Parameter.empty
    ]
    return tool


TOOLS = [tool_schema(func) for func in QUERY_FUNCTIONS.values()]


def call_tool(name: str, arguments: Mapping[str, Any]) -> str:
    """
    Run a tool call and return its result, or an error the model can correct.

    Small models often pass `null` or `""` for arguments they don't mean to set, so those are
    dropped rather than treated as filters.
    """
    func = QUERY_FUNCTIONS.get(name)
    if func is None:
        return f"error: unknown tool {name!r}; use one of {', '.join(QUERY_FUNCTIONS)}"
    kwargs = {k: v for k, v in arguments.items() if v not in (None, "")}
    try:
        return func(**kwargs)
    except (TypeError, ValueError, sqlite3.Error) as exc:
        return f"error: {exc}"


def log_tool_call(name: str, arguments: Mapping[str, Any]) -> None:
    """Print a tool call to stderr so a long answer shows what it's doing."""
    args = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    print(f"  → {name}({args})", file=sys.stderr)


class Conversation:
    """A multi-turn conversation over the database."""

    def __init__(self, client: ollama.Client | None = None) -> None:
        self.client = client or ollama.Client()
        self.messages: list[dict[str, Any] | ollama.Message] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def chat(self) -> ollama.Message:
        """Send the conversation so far and return the model's reply."""
        response = self.client.chat(
            model=MODEL,
            messages=self.messages,
            tools=TOOLS,
            options={"num_ctx": NUM_CTX, "temperature": 0},
        )
        return response.message

    def ask(self, question: str) -> str:
        """Ask a question and return the answer, keeping the exchange in the history."""
        today = datetime.now().astimezone().strftime("%A %Y-%m-%d")
        self.messages.append(
            {"role": "user", "content": f"(Today is {today}.)\n\n{question}"}
        )
        for _ in range(MAX_ROUNDS):
            reply = self.chat()
            self.messages.append(reply)
            if not reply.tool_calls:
                return (reply.content or "").strip() or "(empty response)"
            for call in reply.tool_calls:
                log_tool_call(call.function.name, call.function.arguments)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_name": call.function.name,
                        "content": call_tool(
                            call.function.name, call.function.arguments
                        ),
                    }
                )
        return (
            f"(Stopped after {MAX_ROUNDS} rounds of tool calls without a final answer.)"
        )


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
