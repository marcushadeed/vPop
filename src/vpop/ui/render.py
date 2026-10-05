"""
How the chat looks: its colours, and the pieces of the transcript (the header, tool calls,
answers, timings) as rich renderables or plain strings. Nothing here reads input or keeps
state, so every piece can be rendered to a string and checked.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Markdown
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from vpop.assistant.conversation import ToolCall, Trace
from vpop.config import Config

# Plain ANSI colours, so the chat suits light and dark terminals alike. `muted` is the grey
# prompt_toolkit can also draw (it has no dim attribute), so the input box matches.
THEME = Theme(
    {
        "accent": "cyan",
        "muted": "bright_black",
        "ok": "green",
        "warning": "yellow",
        "error": "red",
    }
)

# Slash commands and what they do; `/clear` and `/quit` also work.
COMMANDS = {
    "/help": "show commands and keys",
    "/new": "start a new conversation (also /clear)",
    "/exit": "quit (also /quit, ctrl+d)",
}

KEYS = (
    "enter sends · alt+enter adds a line · ↑ ↓ history · tab completes a command",
    "ctrl+c cancels a question or clears the line; twice on an empty line quits",
    "`vpop --help` lists the other commands (sync, config, auth, bench)",
)

# The `… 12 more (…)` line a tool adds when it leaves rows out.
MORE = re.compile(r"… ([\d,]+) more")


def make_console(file: IO[str] | None = None, **kwargs: Any) -> Console:
    """A console with the chat's theme, that doesn't highlight numbers in plain text."""
    return Console(file=file, theme=THEME, highlight=False, **kwargs)


def plural(n: int, noun: str) -> str:
    """`1 thread`, `2 threads`, `1,204 messages`."""
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def count(n: int) -> str:
    """A token count in a few characters: `780`, `3.1k`, `41.8k`, `312k`, `1.3M`."""
    for size, suffix in ((1_000_000, "M"), (1_000, "k")):
        if n >= size:
            value = n / size
            text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
            return text.removesuffix(".0") + suffix
    return str(n)


def duration(seconds: float) -> str:
    """`6s`, or `1m 05s` from a minute on."""
    total = round(seconds)
    return f"{total}s" if total < 60 else f"{total // 60}m {total % 60:02d}s"


def header(messages: int, newest: str | None) -> Group:
    """The lines the chat opens with: what's in the database, and how to get help."""
    if messages:
        data = f"{plural(messages, 'message')}, newest {(newest or '')[:10]}"
    else:
        data = "no messages yet; run `vpop sync`"
    return Group(
        Text.assemble(("✻ ", "accent"), ("vPop", "bold"), (f" · {data}", "muted")),
        Text("  /help for commands · ctrl+c cancels · ctrl+d quits", style="muted"),
    )


def arguments(values: Mapping[str, Any]) -> str:
    """Tool arguments as `key: value`, with values as JSON (`text: "june", limit: 5`)."""
    return ", ".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in values.items()
    )


def result_summary(call: ToolCall) -> str:  # pylint: disable=too-many-return-statements
    """
    A few words on what a tool call returned: `2 threads`, `14 messages (+36 more)`,
    `30 of 812 messages`, `10 rows`. Errors and empty results (`No messages match.`) are
    shown as they are. This reads the text formats the tools write.
    """
    lines = call.result.splitlines() or [""]
    if call.is_error or (len(lines) == 1 and lines[0].startswith("No ")):
        return lines[0]
    rows = [line for line in lines if not line.startswith("…")]
    if call.name == "read_thread":
        # `thread <key> (<label>): 30 of 812 messages`
        return lines[0].rpartition("): ")[2]
    if call.name == "run_sql":
        if lines[1] == "(no rows)":
            return "no rows"
        # The first line names the columns; a cut-off result ends `… more rows`.
        if len(rows) < len(lines):
            return f"{len(rows) - 1:,}+ rows"
        return plural(len(rows) - 1, "row")
    if call.name == "read_event":
        return lines[0]  # the event's title
    if call.name == "search_events":
        # After a line naming the columns; a cut-off result ends `… more events`.
        events = plural(len(rows) - 1, "event")
        return f"{events} (+more)" if len(rows) < len(lines) else events
    if call.name == "find_threads":
        summary = plural(len(rows) - 1, "thread")  # after a line naming the columns
    elif call.name == "search_messages":
        summary = plural(len(rows), "message")
    else:
        summary = plural(len(lines), "line")
    more = next((match[1] for line in lines if (match := MORE.match(line))), None)
    return f"{summary} (+{more} more)" if more else summary


def tool_call(call: ToolCall) -> Group:
    """`● name(key: value)` and, under it, `⎿  summary`; each cut to one line."""
    status = "error" if call.is_error else "ok"
    return Group(
        Text.assemble(
            ("● ", status),
            (call.name, "bold"),
            f"({arguments(call.arguments)})",
            no_wrap=True,
            overflow="ellipsis",
        ),
        Text.assemble(
            ("  ⎿  ", "muted"),
            (result_summary(call), "error" if call.is_error else "muted"),
            no_wrap=True,
            overflow="ellipsis",
        ),
    )


def answer(text: str) -> Table:
    """The model's text as markdown, beside a `●` like a tool call's."""
    grid = Table.grid()
    grid.add_column(no_wrap=True)
    grid.add_column()
    grid.add_row("● ", Markdown(text))
    return grid


@dataclass
class Tail:
    """
    A renderable cut to its last lines that fit on the screen, leaving `reserve` lines free,
    so a growing preview keeps its newest text in view.
    """

    renderable: RenderableType
    reserve: int = 2

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        lines = console.render_lines(self.renderable, options, pad=False)
        height = max(1, options.max_height - self.reserve)
        for line in lines[-height:]:
            yield from line
            yield Segment.line()


def stats(seconds: float, cost: float | None) -> Text:
    """How long a question took and, when known, what it cost: `✻ 6s · ~$0.03`."""
    parts = [duration(seconds)]
    if cost is not None:
        parts.append(f"~${cost:.2f}")
    return Text("  ✻ " + " · ".join(parts), style="muted")


def usage(trace: Trace, cost: float | None) -> str:
    """The conversation's tokens in and out, and its cost when known: `↑3.1k ↓780 · $0.11`."""
    if not (trace.prompt_tokens or trace.output_tokens):
        return ""
    tokens = f"↑{count(trace.prompt_tokens)} ↓{count(trace.output_tokens)}"
    return tokens if cost is None else f"{tokens} · ${cost:.2f}"


def model_label(config: Config) -> str:
    """Which model answers: `claude-sonnet-5-5 · medium`, or `qwen3:8b · local`."""
    if not config.assistant.local_model:
        return f"{config.claude.model} · {config.claude.effort}"
    label = f"{config.ollama.model} · local"
    if config.ollama.think != "default":
        label += f" · think {config.ollama.think}"
    return label


def spread(left: str, right: str, width: int) -> str:
    """`left` and `right` at either end of `width` columns; `right` is dropped if it can't fit."""
    gap = width - cell_len(left) - cell_len(right)
    if not right or gap < 2:
        return left
    return left + " " * gap + right


def slash_matches(text: str) -> list[tuple[str, str]]:
    """The commands, with what they do, that start with `text` while it's a `/command`."""
    if not text.startswith("/") or any(c.isspace() for c in text):
        return []
    return [(name, about) for name, about in COMMANDS.items() if name.startswith(text)]


def help_text() -> Group:
    """What `/help` prints: the commands, then the keys."""
    width = max(len(name) for name in COMMANDS)
    commands = [
        Text.assemble(f"  {name:<{width}}  ", (about, "muted"))
        for name, about in COMMANDS.items()
    ]
    return Group(
        *commands, Text(), *(Text(f"  {keys}", style="muted") for keys in KEYS)
    )
