"""
The message tools: find conversations, search messages and read a conversation, over every
message source at once (text messages, WhatsApp). Each source keeps its own tables; a
`MessageStore` says how to read them. Conversations are named `<source>:<key>`, e.g.
`text:+12405551234` or `whatsapp:1203…@g.us`. Results have one line per message
(`timestamp | conversation | from | body`), with bodies cut to `BODY_CHARS`.
"""

from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from sqlite3 import Connection

from vpop.sources.android_messages.model import Direction
from vpop.sources.message_queries import (
    MATCH_COLUMNS,
    MessageStore,
    Params,
    more_lines,
    name_patterns,
)
from vpop.tools.toolbox import (
    LABEL_CHARS,
    DatabaseToolset,
    clamp_limit,
    like_pattern,
    message_text,
    one_line,
)


def sender_label(
    direction: str, was_sent: int, sender: str, names: Mapping[str, str]
) -> str:
    """`me` for outgoing messages (flagged if never sent), else the sender's name or key."""
    if direction == Direction.OUTGOING.value:
        return "me" if was_sent else "me (not sent)"
    return names.get(sender) or sender or "unknown"


def join_names(names: Sequence[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class MessageTools(DatabaseToolset):
    """The message tools over the stores of every enabled message source."""

    tool_names = ("find_conversations", "search_messages", "read_conversation")

    def __init__(self, db: Path, stores: Sequence[MessageStore]) -> None:
        super().__init__(db)
        self.stores = {store.prefix: store for store in stores}

    def pick(self, source: str | None) -> list[MessageStore]:
        """The stores a `source` filter selects: all of them without one."""
        if source is None:
            return list(self.stores.values())
        store = self.stores.get(source.strip().lower())
        if store is None:
            raise ValueError(
                f"source must be one of {', '.join(self.stores)}, not {source!r}"
            )
        return [store]

    def conversation(self, conversation: str) -> tuple[MessageStore, str]:
        """A conversation id's store and its key, normalized."""
        prefix, colon, key = conversation.strip().partition(":")
        store = self.stores.get(prefix.lower()) if colon else None
        if store is None:
            raise ValueError(
                "conversation must start with its source, one of "
                f"{', '.join(p + ':' for p in self.stores)} (e.g. "
                f'"text:+12405551234", from find_conversations), not {conversation!r}'
            )
        return store, store.normalize_key(key)

    @staticmethod
    def synced(conn: Connection, stores: Sequence[MessageStore]) -> list[MessageStore]:
        """The stores holding messages, raising if none of them does."""
        found = [store for store in stores if store.has_messages(conn)]
        if not found:
            raise ValueError(
                f"no {join_names([s.description for s in stores])} have been synced yet; "
                "run `vpop sync`"
            )
        return found

    def names(self, conn: Connection) -> dict[str, str]:
        """
        Each number mapped to a name, from every source in turn (text threads, then
        WhatsApp chats). Group messages only carry the sender's number, so this is how they
        get a name.
        """
        names: dict[str, str] = {}
        for store in self.stores.values():
            names.update(conn.execute(store.names).fetchall())
        return names

    @staticmethod
    def labels(conn: Connection, store: MessageStore) -> dict[str, str]:
        """Every conversation's label in one store, by key, where it has one."""
        return dict(
            conn.execute(
                f"SELECT {store.key}, label FROM {store.conversations} WHERE label != ''"
            ).fetchall()
        )

    def find_conversations(
        self, name_or_number: str, source: str | None = None, limit: int = 20
    ) -> str:
        """
        Find conversations by contact name, group name or phone number, in every source.

        Use this first to turn a person or group into conversation ids. Matches part of a
        name ("sam", "book club") or part of a phone number in any format ("240-555-1234",
        "5551234"). Group conversations containing the person are included, and a person
        can have one conversation per source and number: look at every match that fits.

        Args:
            name_or_number: Part of a contact name, a group's name, or part of a phone
                number.
            source: Only this source: "text" or "whatsapp".
            limit: Maximum number of conversations to return, most recently active first.
        """
        query, patterns = name_patterns(name_or_number)
        limit = clamp_limit(limit)
        rows = []
        with closing(self.connect()) as conn:
            for store in self.synced(conn, self.pick(source)):
                key_match = " OR ".join(
                    f"{store.key} LIKE ? ESCAPE '\\'" for _ in patterns
                )
                rows += [
                    (f"{store.prefix}:{key}", *rest)
                    for key, *rest in conn.execute(
                        f"SELECT {store.key}, label, {store.is_group}, message_count, "
                        f"first, last FROM {store.conversations} "
                        f"WHERE {store.name_match} OR {key_match}",
                        (like_pattern(query), *patterns),
                    ).fetchall()
                ]
        if not rows:
            return f"No conversations match {query!r}."
        rows.sort(key=lambda row: row[-1], reverse=True)
        lines = ["conversation | name | messages | first → last"]
        lines += [
            f"{conversation} | {one_line(label or 'unknown', LABEL_CHARS)}"
            f"{' (group)' if group else ''} | {count} msgs | {first} → {last}"
            for conversation, label, group, count, first, last in rows[:limit]
        ]
        if len(rows) > limit:
            lines.append(f"… {len(rows) - limit} more (raise limit)")
        return "\n".join(lines)

    def search_messages(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        *,
        text: str | None = None,
        contains: str | None = None,
        conversation: str | None = None,
        sender: str | None = None,
        since: str | None = None,
        until: str | None = None,
        direction: str | None = None,
        source: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """
        Search messages in every source at once with any combination of filters, newest
        first.

        Args:
            text: Words that must all appear in the message, in any order. Matches word
                forms and prefixes ("move" finds "moving", "pupp" finds "puppy").
            contains: Exact substring of the message (case-insensitive), for codes,
                numbers, links, emoji or text inside a word.
            conversation: Only messages in this conversation (an id from
                find_conversations, e.g. "text:+12405551234").
            sender: Only messages from this phone number, or "me" for messages I sent.
            since: Earliest time: "2026-08" (a month), "2026-08-01" or "2026-08-01 14:30".
            until: Latest time, same forms; a month or day includes all of it.
            direction: "incoming" or "outgoing".
            source: Only this source: "text" or "whatsapp".
            limit: Maximum number of messages to return.
            offset: Number of newest matches to skip, for paging.
        """
        key = None
        if conversation:
            store, key = self.conversation(conversation)
            if source and self.pick(source) != [store]:
                raise ValueError(
                    f"conversation {conversation!r} is in {store.prefix}, not {source}"
                )
            stores = [store]
        else:
            stores = self.pick(source)
        limit = clamp_limit(limit)
        offset = max(0, int(offset))

        with closing(self.connect()) as conn:
            selects: list[str] = []
            params: Params = []
            for store in self.synced(conn, stores):
                where, store_params = store.filters(
                    text=text,
                    contains=contains,
                    key=key,
                    sender=sender,
                    since=since,
                    until=until,
                    direction=direction,
                )
                selects.append(store.matches(where))
                params += store_params
            union = " UNION ALL ".join(selects)
            total = conn.execute(f"SELECT COUNT(*) FROM ({union})", params).fetchone()[
                0
            ]
            rows = conn.execute(
                f"SELECT {MATCH_COLUMNS} FROM ({union}) "
                "ORDER BY epoch_ms DESC, rid DESC, source LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            names = self.names(conn) if rows else {}
            labels = (
                {s.prefix: self.labels(conn, s) for s in stores}
                if rows and not conversation
                else {}
            )

        if not rows:
            return (
                "No messages match."
                if total == 0
                else f"No messages past offset {offset}."
            )
        lines = []
        for prefix, key, ts, _, direc, sent, snd, body, attachments, _ in rows:
            if conversation:
                where_col = ""
            elif label := labels[prefix].get(key):
                where_col = f"{one_line(label, LABEL_CHARS)} [{prefix}:{key}] | "
            else:
                where_col = f"{prefix}:{key} | "
            lines.append(
                f"{ts} | {where_col}{sender_label(direc, sent, snd, names)} | "
                f"{message_text(body, attachments)}"
            )
        remaining = total - offset - len(rows)
        if remaining > 0:
            lines.append(f"… {remaining} more (use offset={offset + len(rows)})")
        return "\n".join(lines)

    def read_conversation(  # pylint: disable=too-many-arguments
        self,
        conversation: str,
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
            conversation: The conversation to read, e.g. "text:+12405551234" (from
                find_conversations or search_messages).
            around: A time to center the window on, e.g. "2026-08-01 14:30:05".
            since: Earliest time to include: "2026-08", "2026-08-01" or "2026-08-01 14:30".
            until: Latest time to include, same forms; a month or day includes all of it.
            limit: Maximum number of messages to return.
        """
        store, key = self.conversation(conversation)
        name = f"{store.prefix}:{key}"
        with closing(self.connect()) as conn:
            self.synced(conn, [store])
            window = store.window(
                conn,
                f"timestamp, direction, {store.was_sent}, sender, body, attachments",
                key,
                around=around,
                since=since,
                until=until,
                limit=clamp_limit(limit),
            )
            if not window.total:
                return (
                    f"No messages in {name} for that range. "
                    "Check the id with find_conversations."
                )
            names = self.names(conn)
            label = self.labels(conn, store).get(key, "unknown")

        lines = more_lines(
            window,
            [
                f"{ts} | {sender_label(direc, sent, snd, names)} | "
                f"{message_text(body, attachments)}"
                for ts, direc, sent, snd, body, attachments in window.rows
            ],
        )
        heading = (
            f"{name} ({one_line(label, LABEL_CHARS)}): "
            f"{len(window.rows)} of {window.total} messages"
        )
        return "\n".join([heading, *lines])
