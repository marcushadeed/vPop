"""
Read-only query tools over the message database, used by the LLM harnesses.

Every tool returns compact text rather than JSON, one line per message
(`timestamp | thread | from | body`), so tool results stay small. Bodies are cut to
`BODY_CHARS`, result sets are capped with a trailing `… N more` line saying how to page, and
no result is longer than `MAX_RESULT_CHARS`.

`MessageTools` binds the tools to one database file. The tool definitions the models see are
built from the methods' signatures and docstrings, so the docstrings are written for the model.
"""

import calendar
import inspect
import re
import sqlite3
import time
import types
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from vpop.db import DatabaseError, connect_readonly
from vpop.sources.android_messages.model import Direction, normalize_address
from vpop.sources.android_messages.model import thread_key as normalize_thread_key

BODY_CHARS = 300
SQL_CELL_CHARS = 300
LABEL_CHARS = 200
MAX_LIMIT = 500
# About 3k tokens: big enough for a page of messages, small enough that one call can't fill a
# local model's context window.
MAX_RESULT_CHARS = 12_000
SQL_TIMEOUT_SECONDS = 5.0

# Authorizer actions a `run_sql` query may perform: reading and calling functions only.
ALLOWED_SQL_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}

TOOL_NAMES = ("find_threads", "search_messages", "read_thread", "run_sql")

TIME_PATTERN = re.compile(
    r"(\d{4})-(\d{2})(?:-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?)?"
)


def one_line(text: str, limit: int = BODY_CHARS) -> str:
    """Collapse newlines and cut `text` to `limit` characters."""
    flat = re.sub(r"\s*\n\s*", " ⏎ ", text.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def clamp_limit(limit: int) -> int:
    """Keep a caller-supplied limit between 1 and `MAX_LIMIT`."""
    return max(1, min(int(limit), MAX_LIMIT))


def time_range(value: str, name: str) -> tuple[str, str]:
    """
    The first and last second a time covers, as `YYYY-MM-DD HH:MM:SS`: a month
    (`2026-08`), a day (`2026-08-01`), a minute (`2026-08-01 14:30`) or a second. Raises a
    `ValueError` naming the accepted forms for anything else, so a misformatted bound is an
    error the model can fix rather than a silent "no messages".
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
    fmt = "%Y-%m-%d %H:%M:%S"
    return first.strftime(fmt), last.strftime(fmt)


def like_pattern(text: str) -> str:
    """A `LIKE ... ESCAPE '\\'` pattern matching `text` anywhere, with wildcards escaped."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def fts_query(text: str) -> str:
    """
    An FTS5 query requiring every word in `text`, each as a prefix of its stem, so "move"
    finds "moving" and "pupp" finds "puppy".
    """
    words = re.findall(r"\w+", text)
    if not words:
        raise ValueError(
            "text has no words to search for; use contains for symbols, emoji or codes"
        )
    return " ".join(f'"{word}"*' for word in words)


def normalize_thread(key: str) -> str:
    """Accept a thread key in any phone format, with or without spaces after commas."""
    return normalize_thread_key(key.strip())


def message_text(body: str, attachments: str) -> str:
    """A message body for display, with its attachments' types in front."""
    tags = "".join(f"[{kind}] " for kind in attachments.split(",") if kind)
    return one_line(tags + body)


def sender_label(
    direction: str, was_sent: int, sender: str, names: Mapping[str, str]
) -> str:
    """`me` for outgoing messages (flagged if never sent), else the sender's name or number."""
    if direction == Direction.OUTGOING.value:
        return "me" if was_sent else "me (not sent)"
    return names.get(sender) or sender or "unknown"


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


class MessageTools:
    """The query tools, bound to one database file."""

    def __init__(self, db: Path) -> None:
        self.db = db

    @property
    def functions(self) -> dict[str, Callable[..., str]]:
        """The tools by name, as bound methods."""
        return {name: getattr(self, name) for name in TOOL_NAMES}

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
            return f"error: unknown tool {name!r}; use one of {', '.join(TOOL_NAMES)}"
        kwargs = {k: v for k, v in arguments.items() if v not in (None, "")}
        try:
            inspect.signature(func).bind(**kwargs)
        except TypeError as exc:
            return f"error: {exc}"
        try:
            return cap_result(func(**kwargs))
        except (ValueError, sqlite3.Error, DatabaseError) as exc:
            return f"error: {exc}"

    def connect(self) -> sqlite3.Connection:
        """A read-only connection to the database."""
        return connect_readonly(self.db)

    @staticmethod
    def contact_names(conn: sqlite3.Connection) -> dict[str, str]:
        """
        Each 1:1 thread's number mapped to its contact name. Group messages only carry the
        sender's number, so this is how a sender in a group gets a name.
        """
        rows = conn.execute(
            "SELECT thread_key, label FROM threads "
            "WHERE thread_key NOT LIKE '%,%' AND label != ''"
        )
        return dict(rows.fetchall())

    @staticmethod
    def thread_label(conn: sqlite3.Connection, key: str) -> str:
        """A thread's most recent contact name, or `unknown`."""
        row = conn.execute(
            "SELECT label FROM threads WHERE thread_key = ?", (key,)
        ).fetchone()
        return row[0] if row and row[0] else "unknown"

    def find_threads(self, name_or_number: str, limit: int = 20) -> str:
        """
        Find conversations by contact name or phone number.

        Use this first to turn a person's name into a thread_key. Matches part of a contact
        name ("sam") or part of a phone number in any format ("240-555-1234", "5551234").
        Group threads containing the person are included.

        Args:
            name_or_number: Part of a contact name, or part of a phone number.
            limit: Maximum number of threads to return, most recently active first.
        """
        query = name_or_number.strip()
        if not query:
            raise ValueError("name_or_number is empty")
        limit = clamp_limit(limit)
        patterns = {like_pattern(query), like_pattern(normalize_address(query))}
        digits = re.sub(r"\D", "", query)
        if len(digits) >= 4:
            patterns.add(like_pattern(digits))
        key_match = " OR ".join("thread_key LIKE ? ESCAPE '\\'" for _ in patterns)

        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT thread_key, label, message_count, first, last FROM threads "
                "WHERE thread_key IN (SELECT thread_key FROM messages "
                "WHERE contact_name LIKE ? ESCAPE '\\') "
                f"OR {key_match} ORDER BY last DESC",
                (like_pattern(query), *patterns),
            ).fetchall()

        if not rows:
            return f"No threads match {query!r}."
        lines = ["thread_key | contact name(s) | messages | first → last"]
        lines += [
            f"{key} | {one_line(label or 'unknown', LABEL_CHARS)} | {count} msgs | "
            f"{first} → {last}"
            for key, label, count, first, last in rows[:limit]
        ]
        if len(rows) > limit:
            lines.append(f"… {len(rows) - limit} more (raise limit)")
        return "\n".join(lines)

    def search_messages(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
        self,
        *,
        text: str | None = None,
        contains: str | None = None,
        thread_key: str | None = None,
        sender: str | None = None,
        since: str | None = None,
        until: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """
        Search messages with any combination of filters, newest first.

        Args:
            text: Words that must all appear in the message body, in any order. Matches word
                forms and prefixes ("move" finds "moving", "pupp" finds "puppy").
            contains: Exact substring of the body (case-insensitive), for codes, numbers,
                links, emoji or text inside a word.
            thread_key: Only messages in this thread (from find_threads).
            sender: Only messages from this phone number, or "me" for messages I sent.
            since: Earliest time: "2026-08" (a month), "2026-08-01" or "2026-08-01 14:30".
            until: Latest time, same forms; a month or day includes all of it.
            direction: "incoming" or "outgoing".
            limit: Maximum number of messages to return.
            offset: Number of newest matches to skip, for paging.
        """
        clauses: list[str] = []
        params: list[str | int] = []
        if text:
            clauses.append(
                "rowid IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)"
            )
            params.append(fts_query(text))
        if contains:
            clauses.append("body LIKE ? ESCAPE '\\'")
            params.append(like_pattern(contains))
        if thread_key:
            clauses.append("thread_key = ?")
            params.append(normalize_thread(thread_key))
        if sender:
            if sender.strip().lower() == "me":
                if direction == Direction.INCOMING.value:
                    raise ValueError(
                        'sender="me" means outgoing messages; drop direction="incoming"'
                    )
                direction = Direction.OUTGOING.value
            else:
                clauses.append("sender = ?")
                params.append(normalize_address(sender))
        if direction:
            if direction not in {d.value for d in Direction}:
                raise ValueError('direction must be "incoming" or "outgoing"')
            clauses.append("direction = ?")
            params.append(direction)
        if since:
            clauses.append("timestamp >= ?")
            params.append(time_range(since, "since")[0])
        if until:
            clauses.append("timestamp <= ?")
            params.append(time_range(until, "until")[1])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = clamp_limit(limit)
        offset = max(0, int(offset))

        with closing(self.connect()) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM messages {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT timestamp, thread_key, direction, was_sent, sender, body, "
                f"attachments FROM messages {where} "
                "ORDER BY epoch_ms DESC, rowid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            names = self.contact_names(conn) if rows else {}

        if not rows:
            return (
                "No messages match."
                if total == 0
                else f"No messages past offset {offset}."
            )
        lines = []
        for ts, key, direc, sent, snd, body, attachments in rows:
            if thread_key:
                thread_col = ""
            else:
                thread_col = f"{names[key]} [{key}] | " if key in names else f"{key} | "
            lines.append(
                f"{ts} | {thread_col}{sender_label(direc, sent, snd, names)} | "
                f"{message_text(body, attachments)}"
            )
        remaining = total - offset - len(rows)
        if remaining > 0:
            lines.append(f"… {remaining} more (use offset={offset + len(rows)})")
        return "\n".join(lines)

    def read_thread(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        thread_key: str,
        *,
        around: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 60,
    ) -> str:
        """
        Read a conversation in chronological order.

        With `around`, returns the messages on either side of that time, which is how to see
        the context of a search hit. With `since`, reads forward from it. Otherwise returns
        the latest messages (before `until`, if given).

        Args:
            thread_key: The thread to read (from find_threads or search_messages).
            around: A time to center the window on, e.g. "2026-08-01 14:30:05".
            since: Earliest time to include: "2026-08", "2026-08-01" or "2026-08-01 14:30".
            until: Latest time to include, same forms; a month or day includes all of it.
            limit: Maximum number of messages to return.
        """
        key = normalize_thread(thread_key)
        limit = clamp_limit(limit)
        clauses = ["thread_key = ?"]
        params: list[str | int] = [key]
        if since:
            clauses.append("timestamp >= ?")
            params.append(time_range(since, "since")[0])
        if until:
            clauses.append("timestamp <= ?")
            params.append(time_range(until, "until")[1])
        where = " AND ".join(clauses)
        select = (
            "SELECT timestamp, direction, was_sent, sender, body, attachments, epoch_ms, "
            f"rowid FROM messages WHERE {where}"
        )
        newest_first = "ORDER BY epoch_ms DESC, rowid DESC"
        oldest_first = "ORDER BY epoch_ms ASC, rowid ASC"

        with closing(self.connect()) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where}", params
            ).fetchone()[0]
            if total == 0:
                return (
                    f"No messages in thread {key!r} for that range. "
                    "Check the key with find_threads."
                )
            if around:
                center = time_range(around, "around")[0]
                before = conn.execute(
                    f"{select} AND timestamp <= ? {newest_first} LIMIT ?",
                    (*params, center, (limit + 1) // 2),
                ).fetchall()
                after = conn.execute(
                    f"{select} AND timestamp > ? {oldest_first} LIMIT ?",
                    (*params, center, limit - len(before)),
                ).fetchall()
                rows = [*reversed(before), *after]
            elif since:
                rows = conn.execute(
                    f"{select} {oldest_first} LIMIT ?", (*params, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"{select} {newest_first} LIMIT ?", (*params, limit)
                ).fetchall()
                rows.reverse()
            # (epoch_ms, rowid) orders messages that share a second the same way as above.
            earlier = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where} AND (epoch_ms, rowid) < (?, ?)",
                (*params, *rows[0][-2:]),
            ).fetchone()[0]
            later = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where} AND (epoch_ms, rowid) > (?, ?)",
                (*params, *rows[-1][-2:]),
            ).fetchone()[0]
            names = self.contact_names(conn)
            label = self.thread_label(conn, key)

        lines = [
            f"thread {key} ({one_line(label, LABEL_CHARS)}): {len(rows)} of {total} messages"
        ]
        if earlier:
            lines.append(f"… {earlier} earlier (use until or around)")
        lines += [
            f"{ts} | {sender_label(direc, sent, snd, names)} | "
            f"{message_text(body, attachments)}"
            for ts, direc, sent, snd, body, attachments, _, _ in rows
        ]
        if later:
            lines.append(f"… {later} later (use since or around)")
        return "\n".join(lines)

    def run_sql(self, query: str, limit: int = 200) -> str:
        """
        Run one read-only SQL query against the database, for counts and aggregates.

        Only reading is allowed (SELECT or WITH), one statement per call, and a query that
        runs longer than a few seconds is stopped. Prefer the other tools for reading
        messages; use this for questions like "how many texts per month with X" or "who did
        I text most in 2025". Results are cut to `limit` rows.

        Args:
            query: A single SQLite SELECT or WITH statement over the messages and threads
                tables.
            limit: Maximum number of rows to return.
        """
        statement = query.strip()
        if not statement:
            raise ValueError("query is empty")
        limit = clamp_limit(limit)
        denied: list[int] = []
        deadline = time.monotonic() + SQL_TIMEOUT_SECONDS
        timed_out = False

        def authorize(action: int, *_: object) -> int:
            if action in ALLOWED_SQL_ACTIONS:
                return sqlite3.SQLITE_OK
            denied.append(action)
            return sqlite3.SQLITE_DENY

        def past_deadline() -> int:
            nonlocal timed_out
            timed_out = time.monotonic() > deadline
            return int(timed_out)

        with closing(self.connect()) as conn:
            conn.set_authorizer(authorize)
            conn.set_progress_handler(past_deadline, 10_000)
            try:
                cursor = conn.execute(statement)
                rows = cursor.fetchmany(limit + 1)
            except sqlite3.DatabaseError as exc:
                if denied:
                    raise ValueError(
                        "only read-only queries are allowed (SELECT or WITH)"
                    ) from exc
                if timed_out:
                    raise ValueError(
                        f"query stopped after {SQL_TIMEOUT_SECONDS:g}s; add filters, "
                        "aggregate further, or bound any recursive CTE"
                    ) from exc
                raise
            columns = [col[0] for col in cursor.description or []]

        if not rows:
            return " | ".join(columns) + "\n(no rows)"
        lines = [" | ".join(columns)]
        lines += [" | ".join(sql_cell(value) for value in row) for row in rows[:limit]]
        if len(rows) > limit:
            lines.append(
                f"… more rows (raise limit or aggregate further, max {MAX_LIMIT})"
            )
        return "\n".join(lines)


def sql_cell(value: object) -> str:
    """Render one result cell, making NULL and empty strings visible."""
    if value is None:
        return "NULL"
    if value == "":
        return '""'
    return one_line(str(value), SQL_CELL_CHARS)
