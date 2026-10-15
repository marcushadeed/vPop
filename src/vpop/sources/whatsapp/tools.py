"""
The WhatsApp tools: find chats, search messages and read a chat, over the
`whatsapp_messages` and `whatsapp_chats` tables. Results have one line per message
(`timestamp | chat | from | body`), with bodies cut to `BODY_CHARS`.
"""

# The tools share their queries with the text message tools (`message_queries`), but each
# keeps its own signature and docstring, which is what the model reads, so they look alike.
# pylint: disable=duplicate-code

from collections.abc import Mapping
from contextlib import closing
from sqlite3 import Connection

from vpop.assistant.toolbox import (
    LABEL_CHARS,
    DatabaseToolset,
    clamp_limit,
    like_pattern,
    message_text,
    one_line,
)
from vpop.sources.android_messages.model import Direction, normalize_address
from vpop.sources.message_queries import MessageTable, more_lines, name_patterns

PROMPT = """\
WhatsApp messages are in SQLite too. Table `whatsapp_messages`, one row per message:
- `timestamp`: local time as text, `YYYY-MM-DD HH:MM:SS`, like the text messages.
- `direction`: `incoming` or `outgoing`. Outgoing messages were written by the user ("me").
- `chat_key`: the chat, and the column to group by for anything "per person". An E.164 \
number (`+` then digits) for a 1:1 chat, the same key as that person's text thread_key, or \
`...@g.us` for a group. A `...@lid` key is a person WhatsApp hides the number of.
- `sender`: who sent an incoming message (a number, or `...@lid`); empty for outgoing ones.
- `body`: the message text, or a media caption.
- `attachments`: the content type of attached media (e.g. `image/jpeg`).
- `epoch_ms`, `id`, `key_id`: bookkeeping, rarely useful.
Table `whatsapp_chats`, one row per chat: `chat_key`, `label` (the group subject, or the \
person's name from their text thread), `subject`, `is_group`, `message_count`, `first` and \
`last` (timestamps).

How to work with WhatsApp:
- When a question names a person or group, resolve them with find_chats. Someone known by \
name from texts may also have a WhatsApp chat under the same number; for "all my messages \
with X", check both find_threads and find_chats.
- Use search_whatsapp to find candidate messages, then read_chat with `around` to check the \
surrounding conversation.
- For counts across both, run_sql can combine the tables, e.g. `SELECT thread_key AS who, \
timestamp FROM messages UNION ALL SELECT chat_key, timestamp FROM whatsapp_messages`.
- When you rely on a WhatsApp message, say it was on WhatsApp, with its timestamp and who \
said it.
"""


def normalize_chat(key: str) -> str:
    """Accept a chat key as given, or a phone number in any format."""
    key = key.strip()
    return key.lower() if "@" in key else normalize_address(key)


MESSAGES = MessageTable("whatsapp_messages", "chat_key", normalize_chat, normalize_chat)


def sender_label(direction: str, sender: str, names: Mapping[str, str]) -> str:
    """`me` for outgoing messages, else the sender's name or key."""
    if direction == Direction.OUTGOING.value:
        return "me"
    return names.get(sender) or sender or "unknown"


class WhatsAppTools(DatabaseToolset):
    """The WhatsApp tools, bound to one database file."""

    tool_names = ("find_chats", "search_whatsapp", "read_chat")
    data = "WhatsApp messages"
    prompt = PROMPT

    @staticmethod
    def contact_names(conn: Connection) -> dict[str, str]:
        """
        Each number mapped to a name: from the text threads, then from the WhatsApp 1:1
        chats. Group messages only carry the sender's number, so this is how they get a name.
        """
        names = dict(
            conn.execute(
                "SELECT thread_key, label FROM threads "
                "WHERE thread_key NOT LIKE '%,%' AND label != ''"
            ).fetchall()
        )
        names.update(
            conn.execute(
                "SELECT chat_key, label FROM whatsapp_chats "
                "WHERE NOT is_group AND label != ''"
            ).fetchall()
        )
        return names

    @staticmethod
    def chat_labels(conn: Connection) -> dict[str, str]:
        """Every chat's label, by key, where it has one."""
        return dict(
            conn.execute(
                "SELECT chat_key, label FROM whatsapp_chats WHERE label != ''"
            ).fetchall()
        )

    def find_chats(self, name_or_number: str, limit: int = 20) -> str:
        """
        Find WhatsApp chats by contact name, group name or phone number.

        Use this first to turn a person or group into a chat_key. Matches part of a name
        ("sam", "book club") or part of a phone number in any format ("240-555-1234").

        Args:
            name_or_number: Part of a name, a group's name, or part of a phone number.
            limit: Maximum number of chats to return, most recently active first.
        """
        query, patterns = name_patterns(name_or_number)
        limit = clamp_limit(limit)
        key_match = " OR ".join("chat_key LIKE ? ESCAPE '\\'" for _ in patterns)

        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT chat_key, label, is_group, message_count, first, last "
                "FROM whatsapp_chats WHERE label LIKE ? ESCAPE '\\' "
                f"OR {key_match} ORDER BY last DESC",
                (like_pattern(query), *patterns),
            ).fetchall()

        if not rows:
            return f"No WhatsApp chats match {query!r}."
        lines = ["chat_key | name | messages | first → last"]
        lines += [
            f"{key} | {one_line(label or 'unknown', LABEL_CHARS)}"
            f"{' (group)' if group else ''} | {count} msgs | {first} → {last}"
            for key, label, group, count, first, last in rows[:limit]
        ]
        if len(rows) > limit:
            lines.append(f"… {len(rows) - limit} more (raise limit)")
        return "\n".join(lines)

    def search_whatsapp(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        *,
        text: str | None = None,
        contains: str | None = None,
        chat_key: str | None = None,
        sender: str | None = None,
        since: str | None = None,
        until: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """
        Search WhatsApp messages with any combination of filters, newest first.

        Args:
            text: Words that must all appear in the message, in any order. Matches word
                forms and prefixes ("move" finds "moving").
            contains: Exact substring of the message (case-insensitive), for codes,
                numbers, links, emoji or text inside a word.
            chat_key: Only messages in this chat (from find_chats).
            sender: Only messages from this phone number, or "me" for messages I sent.
            since: Earliest time: "2026-08" (a month), "2026-08-01" or "2026-08-01 14:30".
            until: Latest time, same forms; a month or day includes all of it.
            direction: "incoming" or "outgoing".
            limit: Maximum number of messages to return.
            offset: Number of newest matches to skip, for paging.
        """
        where, params = MESSAGES.filters(
            text=text,
            contains=contains,
            key=chat_key,
            sender=sender,
            since=since,
            until=until,
            direction=direction,
        )
        limit = clamp_limit(limit)
        offset = max(0, int(offset))

        with closing(self.connect()) as conn:
            total, rows = MESSAGES.page(
                conn,
                "timestamp, chat_key, direction, sender, body, attachments",
                where,
                params,
                limit=limit,
                offset=offset,
            )
            names = self.contact_names(conn) if rows else {}
            chats = self.chat_labels(conn) if rows else {}

        if not rows:
            return (
                "No WhatsApp messages match."
                if total == 0
                else f"No WhatsApp messages past offset {offset}."
            )
        lines = []
        for ts, key, direc, snd, body, attachments in rows:
            if chat_key:
                chat_col = ""
            elif key in chats:
                chat_col = f"{one_line(chats[key], LABEL_CHARS)} [{key}] | "
            else:
                chat_col = f"{key} | "
            lines.append(
                f"{ts} | {chat_col}{sender_label(direc, snd, names)} | "
                f"{message_text(body, attachments)}"
            )
        remaining = total - offset - len(rows)
        if remaining > 0:
            lines.append(f"… {remaining} more (use offset={offset + len(rows)})")
        return "\n".join(lines)

    def read_chat(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        chat_key: str,
        *,
        around: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 60,
    ) -> str:
        """
        Read a WhatsApp chat in chronological order.

        With `around`, returns the messages on either side of that time, which is how to see
        the context of a search hit. With `since`, reads forward from it. Otherwise returns
        the latest messages (before `until`, if given).

        Args:
            chat_key: The chat to read (from find_chats or search_whatsapp).
            around: A time to center the window on, e.g. "2026-08-01 14:30:05".
            since: Earliest time to include: "2026-08", "2026-08-01" or "2026-08-01 14:30".
            until: Latest time to include, same forms; a month or day includes all of it.
            limit: Maximum number of messages to return.
        """
        key = normalize_chat(chat_key)
        with closing(self.connect()) as conn:
            window = MESSAGES.window(
                conn,
                "timestamp, direction, sender, body, attachments",
                key,
                around=around,
                since=since,
                until=until,
                limit=clamp_limit(limit),
            )
            if not window.total:
                return (
                    f"No WhatsApp messages in chat {key!r} for that range. "
                    "Check the key with find_chats."
                )
            names = self.contact_names(conn)
            label = self.chat_labels(conn).get(key, "unknown")

        lines = more_lines(
            window,
            [
                f"{ts} | {sender_label(direc, snd, names)} | {message_text(body, attachments)}"
                for ts, direc, snd, body, attachments in window.rows
            ],
        )
        heading = (
            f"WhatsApp chat {key} ({one_line(label, LABEL_CHARS)}): "
            f"{len(window.rows)} of {window.total} messages"
        )
        return "\n".join([heading, *lines])
