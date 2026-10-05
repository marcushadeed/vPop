"""Write `Message`s into the `messages` table."""

import sqlite3
from collections.abc import Iterable

from vpop.db import refresh_threads
from vpop.sources.android_messages.model import Message

# On a repeat of a stored message:
# - an MMS copy takes over the thread and sender, so an RCS group message ends up in its group
#   thread whichever copy is imported first;
# - the contact name follows the latest import (backups are imported oldest first, so that's
#   the newest name), except that an SMS copy never renames an MMS copy's group thread, and an
#   empty name never replaces a known one.
UPSERT = (
    "INSERT INTO messages (id, epoch_ms, timestamp, direction, was_sent, thread_key, sender, "
    "contact_name, body, attachments, rcs_message_id, from_mms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (id) DO UPDATE SET "
    "thread_key = CASE WHEN excluded.from_mms THEN excluded.thread_key ELSE thread_key END, "
    "sender = CASE WHEN excluded.from_mms THEN excluded.sender ELSE sender END, "
    "contact_name = CASE WHEN excluded.contact_name != '' "
    "AND (excluded.from_mms OR NOT from_mms) "
    "THEN excluded.contact_name ELSE contact_name END, "
    "from_mms = from_mms OR excluded.from_mms"
)


def row(message: Message) -> tuple[object, ...]:
    """The UPSERT parameters for a message."""
    return (
        message.hash(),
        message.epoch_ms,
        message.timestamp,
        message.direction.value,
        message.was_sent,
        message.thread_key,
        message.sender,
        message.contact_name,
        message.body,
        message.attachments,
        message.rcs_message_id,
        message.from_mms,
    )


def insert_messages(conn: sqlite3.Connection, messages: Iterable[Message]) -> int:
    """
    Add messages in one transaction, updating ones already stored (see `UPSERT`), then
    refresh the `threads` table. Returns how many messages were new.
    """
    count = "SELECT COUNT(*) FROM messages"
    with conn:
        before = conn.execute(count).fetchone()[0]
        conn.executemany(UPSERT, (row(message) for message in messages))
        added = int(conn.execute(count).fetchone()[0] - before)
    refresh_threads(conn)
    return added
