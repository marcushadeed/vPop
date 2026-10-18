"""
How a message source's tables are read: a `MessageStore` names a messages table (with
`timestamp`, `epoch_ms`, `direction`, `sender`, `body` and `attachments` columns, a
conversation key column, and an FTS5 index on `body` named `<table>_fts`) and its
conversations table, and builds the queries the message tools run over it: a search's
filters, its matches in columns every source shares, and a window of one conversation.
"""

import re
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any, ClassVar, NamedTuple

from vpop.db import connect_readonly
from vpop.sources.android_messages.model import Direction, normalize_address
from vpop.sources.base import Store
from vpop.tools.toolbox import fts_query, like_pattern, time_range

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


# The columns `MessageStore.matches` selects, the same for every source.
MATCH_COLUMNS = (
    "source, conversation, timestamp, epoch_ms, direction, was_sent, sender, body, "
    "attachments, rid"
)


@dataclass(frozen=True)
class MessageStore(Store):  # pylint: disable=too-many-instance-attributes
    """
    One source's messages. Its conversations are named `<prefix>:<key>` to the model, e.g.
    `text:+12405551234`, so an id says which source to read.
    """

    kind: ClassVar[str] = "messages"

    # The source's name in conversation ids and the `source` filter, e.g. "text".
    prefix: str
    # What the messages are, e.g. "text messages".
    description: str
    # The messages table and its conversation key column.
    table: str
    key: str
    # How a conversation key and a sender are given in any form, e.g. a phone number.
    normalize_key: Callable[[str], str]
    normalize_sender: Callable[[str], str]
    # The conversations table: `key`, `label`, `message_count`, `first` and `last`.
    conversations: str
    # A condition on the conversations table matching a name, with one LIKE pattern.
    name_match: str
    # A query of (key, name) pairs, naming the senders of group messages.
    names: str
    # SQL on the conversations table: whether a conversation is a group.
    is_group: str = "0"
    # SQL on the messages table: 0 for an outgoing message that never went out.
    was_sent: str = "1"

    def summary(self, db: Path) -> str:
        with closing(connect_readonly(db)) as conn:
            count, first, last = conn.execute(
                f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM {self.table}"
            ).fetchone()
        if not count:
            return "no messages"
        return f"{count:,} message{'' if count == 1 else 's'}, {first} → {last}"

    def has_messages(self, conn: Connection) -> bool:
        """Whether a sync has imported any of this source's messages."""
        return bool(
            conn.execute(f"SELECT EXISTS (SELECT 1 FROM {self.table})").fetchone()[0]
        )

    def matches(self, where: str) -> str:
        """A query for the messages `where` selects, in the `MATCH_COLUMNS` every source has."""
        return (
            f"SELECT '{self.prefix}' AS source, {self.key} AS conversation, timestamp, "
            f"epoch_ms, direction, {self.was_sent} AS was_sent, sender, body, attachments, "
            f"rowid AS rid FROM {self.table} {where}"
        )

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
