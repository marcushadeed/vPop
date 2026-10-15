"""
The message tools: find threads, search messages and read a conversation, over the `messages`
and `threads` tables. Results have one line per message (`timestamp | thread | from | body`),
with bodies cut to `BODY_CHARS`.
"""

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
from vpop.sources.android_messages.model import thread_key as normalize_thread_key
from vpop.sources.message_queries import MessageTable, more_lines, name_patterns

PROMPT = """\
Text messages are in SQLite. Table `messages`, one row per message:
- `timestamp`: local time as text, `YYYY-MM-DD HH:MM:SS`. It sorts and compares as a string and \
works with date() and strftime().
- `direction`: `incoming` or `outgoing`. Outgoing messages were written by the user ("me").
- `was_sent`: 0 for an outgoing message that never went out (failed or still queued).
- `thread_key`: the conversation, and the column to group by for anything "per person". A \
single E.164 number (`+1` then ten digits) for a 1:1 thread, a sorted comma-separated list of \
numbers for a group, or an `...@rcs.google.com` id for an RCS group.
- `sender`: the number of whoever sent an incoming message; always empty for outgoing ones, \
so never group outgoing messages by sender.
- `contact_name`: the contact name(s) the phone showed for the thread. It can be empty or \
`(Unknown)`, and a group lists several names.
- `body`: the message text. Reactions look like `❤️ to “...”` or `Liked “...”`.
- `attachments`: content types of attached files (e.g. `image/jpeg`), comma-separated.
- `epoch_ms`, `id`, `rcs_message_id`, `from_mms`: bookkeeping, rarely useful.
Table `threads`, one row per conversation: `thread_key`, `label` (its latest contact name), \
`message_count`, `first` and `last` (timestamps).

How to work with messages:
- When a question names a person, resolve them with find_threads first. A person can have \
more than one thread (a new number, group chats), so look at every match that fits.
- Use search_messages to find candidate messages (`text` for words, `contains` for exact \
codes, numbers or emoji), then read_thread with `around` to check the surrounding \
conversation before drawing conclusions from a single message.
- Use run_sql for counts, rankings and other aggregates. Examples:
  - Who I texted most in 2025: `SELECT m.thread_key, t.label, COUNT(*) AS n FROM messages m \
JOIN threads t USING (thread_key) WHERE m.timestamp >= '2025-01-01' AND m.timestamp < \
'2026-01-01' GROUP BY m.thread_key ORDER BY n DESC LIMIT 10`
  - Messages per month in one thread: `SELECT strftime('%Y-%m', timestamp) AS month, COUNT(*) \
FROM messages WHERE thread_key = '<thread_key>' GROUP BY month ORDER BY month`
- When you rely on a message, cite its timestamp and who said it.
"""


def normalize_thread(key: str) -> str:
    """Accept a thread key in any phone format, with or without spaces after commas."""
    return normalize_thread_key(key.strip())


MESSAGES = MessageTable("messages", "thread_key", normalize_thread, normalize_address)


def sender_label(
    direction: str, was_sent: int, sender: str, names: Mapping[str, str]
) -> str:
    """`me` for outgoing messages (flagged if never sent), else the sender's name or number."""
    if direction == Direction.OUTGOING.value:
        return "me" if was_sent else "me (not sent)"
    return names.get(sender) or sender or "unknown"


class MessageTools(DatabaseToolset):
    """The message tools, bound to one database file."""

    tool_names = ("find_threads", "search_messages", "read_thread")
    data = "text messages (SMS, MMS and RCS from their Android phone)"
    prompt = PROMPT

    @staticmethod
    def contact_names(conn: Connection) -> dict[str, str]:
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
    def thread_label(conn: Connection, key: str) -> str:
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
        query, patterns = name_patterns(name_or_number)
        limit = clamp_limit(limit)
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

    def search_messages(  # pylint: disable=too-many-arguments,too-many-locals
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
        where, params = MESSAGES.filters(
            text=text,
            contains=contains,
            key=thread_key,
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
                "timestamp, thread_key, direction, was_sent, sender, body, attachments",
                where,
                params,
                limit=limit,
                offset=offset,
            )
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
        with closing(self.connect()) as conn:
            window = MESSAGES.window(
                conn,
                "timestamp, direction, was_sent, sender, body, attachments",
                key,
                around=around,
                since=since,
                until=until,
                limit=clamp_limit(limit),
            )
            if not window.total:
                return (
                    f"No messages in thread {key!r} for that range. "
                    "Check the key with find_threads."
                )
            names = self.contact_names(conn)
            label = self.thread_label(conn, key)

        lines = more_lines(
            window,
            [
                f"{ts} | {sender_label(direc, sent, snd, names)} | "
                f"{message_text(body, attachments)}"
                for ts, direc, sent, snd, body, attachments in window.rows
            ],
        )
        heading = (
            f"thread {key} ({one_line(label, LABEL_CHARS)}): "
            f"{len(window.rows)} of {window.total} messages"
        )
        return "\n".join([heading, *lines])
