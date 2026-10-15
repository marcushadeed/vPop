"""
The queries the text message and WhatsApp tools share, over a table with `timestamp`,
`epoch_ms`, `direction`, `sender` and `body` columns, a conversation key column, and an
FTS5 index on `body` named `<table>_fts`: building a search's filters, reading a page of
matches, and reading a window of one conversation.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Any, NamedTuple

from vpop.assistant.toolbox import fts_query, like_pattern, time_range
from vpop.sources.android_messages.model import Direction, normalize_address

Params = list[str | int]
# Rows hold whatever columns the tool selects.
Row = tuple[Any, ...]


def name_patterns(query: str) -> tuple[str, set[str]]:
    """
    For finding a conversation: `query` trimmed (raising if empty), and LIKE patterns for
    its key: the text as given, as a normalized number, and its digits when there are
    enough to mean a number.
    """
    query = query.strip()
    if not query:
        raise ValueError("name_or_number is empty")
    patterns = {like_pattern(query), like_pattern(normalize_address(query))}
    digits = re.sub(r"\D", "", query)
    if len(digits) >= 4:
        patterns.add(like_pattern(digits))
    return query, patterns


def time_clauses(since: str | None, until: str | None) -> tuple[list[str], Params]:
    """The clauses and parameters for `since` and `until` bounds."""
    clauses: list[str] = []
    params: Params = []
    if since:
        clauses.append("timestamp >= ?")
        params.append(time_range(since, "since")[0])
    if until:
        clauses.append("timestamp <= ?")
        params.append(time_range(until, "until")[1])
    return clauses, params


class Window(NamedTuple):
    """Part of a conversation: its rows in order, and how many come before and after."""

    total: int
    rows: list[Row]
    earlier: int
    later: int


@dataclass(frozen=True)
class MessageTable:
    """A messages table, its conversation key column, and how keys and senders are given."""

    table: str
    key: str
    normalize_key: Callable[[str], str]
    normalize_sender: Callable[[str], str]

    def filters(  # pylint: disable=too-many-arguments
        self,
        *,
        text: str | None,
        contains: str | None,
        key: str | None,
        sender: str | None,
        since: str | None,
        until: str | None,
        direction: str | None,
    ) -> tuple[str, Params]:
        """A search's `WHERE` clause (empty without filters) and its parameters."""
        clauses: list[str] = []
        params: Params = []
        if text:
            clauses.append(
                f"rowid IN (SELECT rowid FROM {self.table}_fts "
                f"WHERE {self.table}_fts MATCH ?)"
            )
            params.append(fts_query(text))
        if contains:
            clauses.append("body LIKE ? ESCAPE '\\'")
            params.append(like_pattern(contains))
        if key:
            clauses.append(f"{self.key} = ?")
            params.append(self.normalize_key(key))
        if sender:
            if sender.strip().lower() == "me":
                if direction == Direction.INCOMING.value:
                    raise ValueError(
                        'sender="me" means outgoing messages; drop direction="incoming"'
                    )
                direction = Direction.OUTGOING.value
            else:
                clauses.append("sender = ?")
                params.append(self.normalize_sender(sender))
        if direction:
            if direction not in {d.value for d in Direction}:
                raise ValueError('direction must be "incoming" or "outgoing"')
            clauses.append("direction = ?")
            params.append(direction)
        times, time_params = time_clauses(since, until)
        clauses += times
        params += time_params
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def page(  # pylint: disable=too-many-arguments
        self,
        conn: Connection,
        columns: str,
        where: str,
        params: Params,
        *,
        limit: int,
        offset: int,
    ) -> tuple[int, list[Row]]:
        """How many messages match, and a page of them (`columns`), newest first."""
        total = conn.execute(
            f"SELECT COUNT(*) FROM {self.table} {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {columns} FROM {self.table} {where} "
            "ORDER BY epoch_ms DESC, rowid DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return int(total), rows

    def window(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        conn: Connection,
        columns: str,
        key: str,
        *,
        around: str | None,
        since: str | None,
        until: str | None,
        limit: int,
    ) -> Window:
        """
        Part of one conversation in chronological order: centered on `around`, forward from
        `since`, or else the latest messages (before `until`, if given).
        """
        times, time_params = time_clauses(since, until)
        where = " AND ".join([f"{self.key} = ?", *times])
        params: Params = [key, *time_params]
        select = f"SELECT {columns}, epoch_ms, rowid FROM {self.table} WHERE {where}"
        newest_first = "ORDER BY epoch_ms DESC, rowid DESC"
        oldest_first = "ORDER BY epoch_ms ASC, rowid ASC"
        total = conn.execute(
            f"SELECT COUNT(*) FROM {self.table} WHERE {where}", params
        ).fetchone()[0]
        if total == 0:
            return Window(0, [], 0, 0)
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
        count = f"SELECT COUNT(*) FROM {self.table} WHERE {where} AND (epoch_ms, rowid)"
        earlier = conn.execute(
            f"{count} < (?, ?)", (*params, *rows[0][-2:])
        ).fetchone()[0]
        later = conn.execute(f"{count} > (?, ?)", (*params, *rows[-1][-2:])).fetchone()[
            0
        ]
        return Window(int(total), [row[:-2] for row in rows], int(earlier), int(later))


def more_lines(window: Window, lines: Sequence[str]) -> list[str]:
    """A window's message lines with the `… N earlier` and `… N later` lines around them."""
    out = []
    if window.earlier:
        out.append(f"… {window.earlier} earlier (use until or around)")
    out += lines
    if window.later:
        out.append(f"… {window.later} later (use since or around)")
    return out
