"""
The machinery every set of query tools shares, and the `Toolbox` the harnesses use.

Each source contributes a `Toolset`: a class whose tool methods return compact text rather
than JSON, so tool results stay small, plus a section of the system prompt. Result sets are
capped with a trailing `… N more` line saying how to page, and no result is longer than
`MAX_RESULT_CHARS`. The tool definitions the models see are built from the methods' signatures
and docstrings, so the docstrings are written for the model.

A `Toolbox` gathers the toolsets for one conversation, routes calls to them and assembles the
system prompt from their sections.
"""

import calendar
import inspect
import re
import sqlite3
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Union, get_args, get_origin, get_type_hints

from vpop.assistant.prompt import assemble
from vpop.db import DatabaseError, connect_readonly

BODY_CHARS = 300
LABEL_CHARS = 200
MAX_LIMIT = 500
# About 3k tokens: big enough for a page of messages, small enough that one call can't fill a
# local model's context window.
MAX_RESULT_CHARS = 12_000

TIME_PATTERN = re.compile(
    r"(\d{4})-(\d{2})(?:-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?)?"
)


class ToolError(ValueError):
    """A tool call failed in a way the model should hear about, e.g. an API that's down."""


def one_line(text: str, limit: int = BODY_CHARS) -> str:
    """Collapse newlines and cut `text` to `limit` characters."""
    flat = re.sub(r"\s*\n\s*", " ⏎ ", text.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def clamp_limit(limit: int) -> int:
    """Keep a caller-supplied limit between 1 and `MAX_LIMIT`."""
    return max(1, min(int(limit), MAX_LIMIT))


def parse_time_range(value: str, name: str) -> tuple[datetime, datetime]:
    """
    The first and last second a time covers: a month (`2026-08`), a day (`2026-08-01`), a
    minute (`2026-08-01 14:30`) or a second. Raises a `ValueError` naming the accepted forms
    for anything else, so a misformatted bound is an error the model can fix rather than a
    silent "no results".
    """
    match = TIME_PATTERN.fullmatch(value.strip())
    try:
        if match is None:
            raise ValueError
        year, month, day, hour, minute, second = match.groups()
        if day is None:
            last_day = calendar.monthrange(int(year), int(month))[1]
            first = datetime(int(year), int(month), 1)
            last = datetime(int(year), int(month), last_day, 23, 59, 59)
        elif hour is None:
            first = datetime(int(year), int(month), int(day))
            last = first.replace(hour=23, minute=59, second=59)
        else:
            first = datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second or 0),
            )
            last = first if second else first.replace(second=59)
    except ValueError:
        raise ValueError(
            f'{name} must look like "2026-08", "2026-08-01", "2026-08-01 14:30" or '
            f'"2026-08-01 14:30:00", not {value!r}'
        ) from None
    return first, last


def time_range(value: str, name: str) -> tuple[str, str]:
    """`parse_time_range` as `YYYY-MM-DD HH:MM:SS` text, the form timestamps are stored in."""
    fmt = "%Y-%m-%d %H:%M:%S"
    first, last = parse_time_range(value, name)
    return first.strftime(fmt), last.strftime(fmt)


def cap_result(text: str) -> str:
    """Cut a tool result to `MAX_RESULT_CHARS` at a line break, saying how to narrow it."""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    cut = text.rfind("\n", 0, MAX_RESULT_CHARS)
    return (
        text[: cut if cut > 0 else MAX_RESULT_CHARS]
        + f"\n… output cut at {MAX_RESULT_CHARS:,} characters; narrow the query (add "
        "filters, lower the limit, or aggregate)"
    )


def json_type(annotation: Any) -> str:
    """The JSON Schema type for a parameter annotation such as `int` or `str | None`."""
    if get_origin(annotation) in (Union, types.UnionType):
        annotation = next(
            arg for arg in get_args(annotation) if arg is not types.NoneType
        )
    return {str: "string", int: "integer", float: "number", bool: "boolean"}[annotation]


def split_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """A Google-style docstring's description, and its `Args:` entries by name."""
    description, _, args = inspect.cleandoc(doc).partition("\nArgs:\n")
    params: dict[str, str] = {}
    current = None
    for line in args.splitlines():
        match = re.match(r"\s{2,}(\w+): (.*)", line)
        if match:
            current, text = match.groups()
            params[current] = text
        elif current and line.strip():
            params[current] += " " + line.strip()
    return description.strip(), params


def tool_schema(func: Callable[..., str]) -> dict[str, Any]:
    """
    A provider-neutral tool definition (`name`, `description`, JSON Schema `parameters`)
    from a function's signature and docstring. Only parameters without a default are
    required, so the model isn't pushed to fill in every optional filter.
    """
    description, docs = split_docstring(func.__doc__ or "")
    hints = get_type_hints(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in inspect.signature(func).parameters.items():
        properties[name] = {"type": json_type(hints[name])}
        if name in docs:
            properties[name]["description"] = docs[name]
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "name": func.__name__,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


class Toolset:  # pylint: disable=too-few-public-methods
    """
    A group of tools from one source. Subclasses name their tool methods in `tool_names`,
    describe their data in `data` (for the prompt's first line, empty to leave it out) and
    say how to use the tools in `prompt`, their section of the system prompt.
    """

    tool_names: ClassVar[tuple[str, ...]] = ()
    data: ClassVar[str] = ""
    prompt: ClassVar[str] = ""

    @property
    def functions(self) -> dict[str, Callable[..., str]]:
        """The tools by name, as bound methods."""
        return {name: getattr(self, name) for name in self.tool_names}


class DatabaseToolset(Toolset):
    """A toolset that reads the database."""

    def __init__(self, db: Path) -> None:
        self.db = db

    def connect(self) -> sqlite3.Connection:
        """A read-only connection to the database."""
        return connect_readonly(self.db)


class Toolbox:
    """
    The tools for one conversation. `db` is the database the toolsets read, or None when
    only live sources are available.
    """

    def __init__(self, toolsets: Sequence[Toolset], db: Path | None = None) -> None:
        self.toolsets = list(toolsets)
        self.db = db
        self.functions: dict[str, Callable[..., str]] = {}
        for toolset in self.toolsets:
            for name, func in toolset.functions.items():
                if name in self.functions:
                    raise ValueError(f"two toolsets define a tool named {name!r}")
                self.functions[name] = func
        self.system_prompt = assemble(
            [toolset.data for toolset in self.toolsets],
            [toolset.prompt for toolset in self.toolsets],
        )

    @property
    def names(self) -> list[str]:
        """Every tool's name, in the order the models see them."""
        return list(self.functions)

    def schemas(self) -> list[dict[str, Any]]:
        """Provider-neutral definitions of every tool (see `tool_schema`)."""
        return [tool_schema(func) for func in self.functions.values()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> str:
        """
        Run a tool call and return its result, or an `error: ...` the model can correct.

        Small models often pass `null` or `""` for arguments they don't mean to set, so those
        are dropped rather than treated as filters. Arguments are checked against the
        signature first, so a `TypeError` from inside a tool is a bug, not a bad call.
        """
        func = self.functions.get(name)
        if func is None:
            return f"error: unknown tool {name!r}; use one of {', '.join(self.names)}"
        kwargs = {k: v for k, v in arguments.items() if v not in (None, "")}
        try:
            inspect.signature(func).bind(**kwargs)
        except TypeError as exc:
            return f"error: {exc}"
        try:
            return cap_result(func(**kwargs))
        except (ValueError, sqlite3.Error, DatabaseError) as exc:
            return f"error: {exc}"
