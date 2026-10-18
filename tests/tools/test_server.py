"""Tests for the MCP server, through an MCP client: in-process, and over stdio as Claude runs it."""

import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import mcp_types as types
import pytest
from helpers import build_db, make_message
from mcp import Client, StdioServerParameters

from vpop.config import Config, WhatsAppConfig
from vpop.paths import config_path, db_path
from vpop.sources.registry import SOURCES, enabled_sources
from vpop.tools.server import (
    MAX_INSTRUCTIONS,
    RULES,
    build_server,
    build_toolbox,
    instructions,
)

BOTH = Config(whatsapp=WhatsAppConfig(enabled=True))


def connected[T](config: Config, use: Callable[[Client], Awaitable[T]]) -> T:
    """Run `use` with a client connected in-process to the server for `config`."""
    server = build_server(enabled_sources(config), db_path())

    async def run() -> T:
        async with Client(server) as client:
            return await use(client)

    return anyio.run(run)


def text_of(result: types.CallToolResult) -> str:
    """A tool result's text."""
    (content,) = result.content
    assert isinstance(content, types.TextContent)
    return content.text


@pytest.fixture
def synced() -> Path:
    return build_db(db_path(), [make_message(body="see you at 5")])


def test_tools_are_the_toolbox_tools_and_read_only() -> None:
    async def tools(client: Client) -> list[types.Tool]:
        return list((await client.list_tools()).tools)

    listed = connected(BOTH, tools)
    schemas = build_toolbox(enabled_sources(BOTH), db_path()).schemas()
    assert [tool.name for tool in listed] == [
        "find_conversations",
        "search_messages",
        "read_conversation",
        "run_sql",
        "list_sources",
    ]
    for tool, schema in zip(listed, schemas, strict=True):
        assert tool.name == schema["name"]
        assert tool.description == schema["description"]
        assert tool.input_schema == schema["parameters"]
        assert tool.annotations == types.ToolAnnotations(
            read_only_hint=True, open_world_hint=False
        )
    assert listed[1].title == "Search messages"


def test_tools_are_listed_before_anything_is_synced() -> None:
    async def names(client: Client) -> list[str]:
        return [tool.name for tool in (await client.list_tools()).tools]

    assert "search_messages" in connected(Config(), names)
    assert not db_path().exists()


def test_instructions_fit_and_start_with_the_rules() -> None:
    async def read(client: Client) -> str | None:
        return client.instructions

    text = connected(BOTH, read)
    assert text == instructions(enabled_sources(BOTH))
    assert text.startswith(RULES)
    assert (
        "- WhatsApp messages from an Android phone, as `whatsapp:` conversations"
        in text
    )
    assert text.endswith("use Claude's own connectors for calendar and email.")
    every_source = [cls(BOTH) for cls in SOURCES]
    assert len(instructions(every_source)) <= MAX_INSTRUCTIONS


def test_a_call_returns_the_toolbox_result(synced: Path) -> None:
    arguments = {"text": "see you"}

    async def call(client: Client) -> tuple[str, bool]:
        result = await client.call_tool("search_messages", arguments)
        return text_of(result), result.is_error

    text, is_error = connected(Config(), call)
    toolbox = build_toolbox(enabled_sources(Config()), synced)
    assert text == toolbox.call("search_messages", arguments)
    assert "see you at 5" in text
    assert is_error is False


def test_errors_are_flagged(synced: Path) -> None:
    async def call(client: Client) -> list[tuple[str, bool]]:
        results = [
            await client.call_tool("search_messages", {"since": "last week"}),
            await client.call_tool("drop_everything", None),
        ]
        return [(text_of(r), r.is_error) for r in results]

    (bad_date, flagged), (unknown, unknown_flagged) = connected(Config(), call)
    assert bad_date.startswith("error: since must look like") and flagged
    assert unknown.startswith("error: unknown tool") and unknown_flagged


def test_a_call_without_arguments_uses_the_defaults(synced: Path) -> None:
    async def call(client: Client) -> str:
        result = await client.call_tool("search_messages", None)
        return text_of(result)

    assert "see you at 5" in connected(Config(), call)


def test_over_stdio_stdout_carries_only_the_protocol(synced: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "vpop", "-v", "mcp"], env=dict(os.environ)
    )

    async def run() -> tuple[int, str]:
        # Claude Code connects with the initialize handshake.
        async with Client(params, mode="legacy") as client:
            tools = (await client.list_tools()).tools
            result = await client.call_tool("list_sources", {})
            return len(tools), text_of(result)

    count, text = anyio.run(run)
    assert count == 5
    assert "messages: 1 message, 2026-08-01 09:00:00 → 2026-08-01 09:00:00" in text


def test_a_bad_config_stops_the_server_with_a_message() -> None:
    config_path().parent.mkdir(parents=True)
    config_path().write_text("[whatsapp]\nenabled = 'yes'\n")
    done = subprocess.run(
        [sys.executable, "-m", "vpop", "mcp"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 1
    assert done.stdout == ""
    assert "bad config" in done.stderr
