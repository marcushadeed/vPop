"""Tests for how the chat draws its transcript, rendered to plain text."""

import io
from pathlib import Path
from typing import Any

import pytest
from helpers import SAM, build_db, make_message, outgoing
from rich.console import RenderableType
from rich.text import Text

from vpop.assistant.conversation import ToolCall, Trace
from vpop.assistant.tools import MessageTools
from vpop.config import parse_config
from vpop.ui import render

ALEX = "+13015550000"


def plain(renderable: RenderableType, width: int = 60, height: int = 25) -> str:
    """What the renderable looks like on a terminal of that size, without colours."""
    out = io.StringIO()
    console = render.make_console(
        file=out, width=width, height=height, color_system=None
    )
    console.print(renderable)
    return "".join(line.rstrip() + "\n" for line in out.getvalue().splitlines())


@pytest.fixture
def tools(tmp_path: Path) -> MessageTools:
    return MessageTools(
        build_db(
            tmp_path / "vpop.db",
            [
                make_message(body="moving in June", timestamp="2026-03-01 10:00:00"),
                outgoing(body="which day in June?", timestamp="2026-03-01 10:05:00"),
                make_message(body="June 3rd", timestamp="2026-03-01 10:06:00"),
                make_message(
                    thread_key=ALEX,
                    sender=ALEX,
                    contact_name="Alex Jones",
                    body="lunch?",
                    timestamp="2026-04-02 12:00:00",
                ),
                make_message(
                    thread_key=f"{SAM},{ALEX}",
                    sender=ALEX,
                    contact_name="Sam Smith, Alex Jones",
                    body="group hello",
                    timestamp="2026-05-03 18:00:00",
                ),
            ],
        )
    )


def run(tools: MessageTools, name: str, **arguments: Any) -> ToolCall:
    """A tool call as the conversation records it, with the tool's real result."""
    return ToolCall(name, arguments, tools.call(name, arguments))


@pytest.mark.parametrize(
    ("name", "arguments", "summary"),
    [
        ("find_threads", {"name_or_number": "sam"}, "2 threads"),
        ("find_threads", {"name_or_number": "sam", "limit": 1}, "1 thread (+1 more)"),
        ("find_threads", {"name_or_number": "nobody"}, "No threads match 'nobody'."),
        ("search_messages", {"text": "june"}, "3 messages"),
        ("search_messages", {"text": "june", "limit": 1}, "1 message (+2 more)"),
        ("search_messages", {"text": "paris"}, "No messages match."),
        ("read_thread", {"thread_key": SAM, "limit": 2}, "2 of 3 messages"),
        ("run_sql", {"query": "SELECT COUNT(*) FROM messages"}, "1 row"),
        ("run_sql", {"query": "SELECT body FROM messages", "limit": 2}, "2+ rows"),
        ("run_sql", {"query": "SELECT 1 WHERE 0"}, "no rows"),
    ],
)
def test_result_summary(
    tools: MessageTools, name: str, arguments: dict[str, Any], summary: str
) -> None:
    assert render.result_summary(run(tools, name, **arguments)) == summary


def test_result_summary_of_an_error_is_the_error(tools: MessageTools) -> None:
    call = run(tools, "search_messages", since="August")
    assert render.result_summary(call).startswith("error: since must look like")


def test_tool_call_shows_arguments_and_summary(tools: MessageTools) -> None:
    call = run(tools, "search_messages", text="june", limit=1)
    assert plain(render.tool_call(call)) == (
        '● search_messages(text: "june", limit: 1)\n  ⎿  1 message (+2 more)\n'
    )


def test_tool_call_stays_on_one_line_when_narrow(tools: MessageTools) -> None:
    call = run(tools, "run_sql", query="SELECT body FROM messages WHERE body != ''")
    first, second = plain(render.tool_call(call), width=30).splitlines()
    assert first == '● run_sql(query: "SELECT body…'
    assert second == "  ⎿  5 rows"


def test_answer_renders_markdown_beside_a_bullet() -> None:
    text = "Jordan moved to **Denver** in June, then to Boulder a year later."
    assert plain(render.answer(text), width=40) == (
        "● Jordan moved to Denver in June, then\n  to Boulder a year later.\n"
    )


def test_tail_keeps_the_newest_lines_that_fit() -> None:
    long = Text("\n".join(f"line {i}" for i in range(20)))
    lines = plain(render.Tail(long), height=6).splitlines()
    assert lines == ["line 16", "line 17", "line 18", "line 19"]


@pytest.mark.parametrize(
    ("messages", "newest", "first_line"),
    [
        (12345, "2026-10-04 21:13:00", "✻ vPop · 12,345 messages, newest 2026-10-04"),
        (1, "2026-10-04 21:13:00", "✻ vPop · 1 message, newest 2026-10-04"),
        (0, None, "✻ vPop · no messages yet; run `vpop sync`"),
    ],
)
def test_header(messages: int, newest: str | None, first_line: str) -> None:
    lines = plain(render.header(messages, newest)).splitlines()
    assert lines[0] == first_line
    assert "/help" in lines[1]


@pytest.mark.parametrize(
    ("seconds", "cost", "expected"),
    [(6.2, 0.031, "  ✻ 6s · ~$0.03"), (65, None, "  ✻ 1m 05s")],
)
def test_stats(seconds: float, cost: float | None, expected: str) -> None:
    assert plain(render.stats(seconds, cost)) == expected + "\n"


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (780, "780"),
        (3_100, "3.1k"),
        (12_000, "12k"),
        (41_800, "41.8k"),
        (312_400, "312k"),
        (1_260_000, "1.3M"),
    ],
)
def test_count(n: int, expected: str) -> None:
    assert render.count(n) == expected


def test_usage_shows_tokens_and_cost_when_known() -> None:
    trace = Trace(prompt_tokens=3_100, output_tokens=780)
    assert render.usage(trace, 0.114) == "↑3.1k ↓780 · $0.11"
    assert render.usage(trace, None) == "↑3.1k ↓780"
    assert render.usage(Trace(), None) == ""


@pytest.mark.parametrize(
    ("toml", "label"),
    [
        ("[assistant]\nlocal_model = false\n", "claude-sonnet-5-5 · medium"),
        ("", "qwen3:8b · local"),
        ('[ollama]\nthink = "off"\n', "qwen3:8b · local · think off"),
    ],
)
def test_model_label(toml: str, label: str) -> None:
    assert render.model_label(parse_config(toml)) == label


def test_spread_puts_text_at_both_ends() -> None:
    assert render.spread("left", "right", 15) == "left      right"
    assert render.spread("left", "right", 10) == "left"


@pytest.mark.parametrize(
    ("typed", "names"),
    [
        ("/", ["/help", "/new", "/exit"]),
        ("/n", ["/new"]),
        ("/nope", []),
        ("/new now", []),
        ("hello", []),
    ],
)
def test_slash_matches(typed: str, names: list[str]) -> None:
    assert [name for name, _ in render.slash_matches(typed)] == names
