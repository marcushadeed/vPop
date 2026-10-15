"""Write `WhatsAppMessage`s into `whatsapp_messages`, and recompute `whatsapp_chats`."""

import sqlite3
from collections.abc import Iterable, Mapping

from vpop.sources.whatsapp.model import WhatsAppMessage

# On a repeat of a stored message (every full backup repeats them all):
# - its time stays as first stored: an increment only has it to the second;
# - a newer body or attachment replaces the old one, but an empty one never does, so a
#   message deleted on the phone keeps its text;
# - the chat and sender take the newer key, unless it's a privacy id the older one had
#   already resolved to a number.
UPSERT = (
    "INSERT INTO whatsapp_messages (id, key_id, epoch_ms, timestamp, direction, chat_key, "
    "sender, body, attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (id) DO UPDATE SET "
    "body = CASE WHEN excluded.body != '' THEN excluded.body ELSE body END, "
    "attachments = CASE WHEN excluded.attachments != '' "
    "THEN excluded.attachments ELSE attachments END, "
    "chat_key = CASE WHEN excluded.chat_key LIKE '%@lid' AND chat_key NOT LIKE '%@lid' "
    "THEN chat_key ELSE excluded.chat_key END, "
    "sender = CASE WHEN excluded.sender LIKE '%@lid' AND sender NOT LIKE '%@lid' "
    "THEN sender ELSE excluded.sender END"
)


def row(message: WhatsAppMessage) -> tuple[object, ...]:
    """The UPSERT parameters for a message."""
    return (
        message.hash(),
        message.key_id,
        message.epoch_ms,
        message.timestamp,
        message.direction.value,
        message.chat_key,
        message.sender,
        message.body,
        message.attachments,
    )


def insert_messages(
    conn: sqlite3.Connection, messages: Iterable[WhatsAppMessage]
) -> int:
    """Add messages in one transaction, updating ones already stored. Returns how many were new."""
    count = "SELECT COUNT(*) FROM whatsapp_messages"
    with conn:
        before = conn.execute(count).fetchone()[0]
        conn.executemany(UPSERT, (row(message) for message in messages))
        return int(conn.execute(count).fetchone()[0] - before)


def refresh_chats(conn: sqlite3.Connection, subjects: Mapping[str, str]) -> None:
    """
    Recompute `whatsapp_chats` from the messages, keeping each group's stored subject unless
    `subjects` (from the latest backup) has a newer one. A chat's label is its group subject,
    or for a 1:1 chat the name of the text thread with the same number.
    """
    with conn:
        conn.execute(
            "INSERT INTO whatsapp_chats "
            "(chat_key, label, is_group, message_count, first, last) "
            "SELECT chat_key, '', chat_key LIKE '%@g.us', COUNT(*), MIN(timestamp), "
            "MAX(timestamp) FROM whatsapp_messages GROUP BY chat_key "
            "ON CONFLICT (chat_key) DO UPDATE SET message_count = excluded.message_count, "
            "first = excluded.first, last = excluded.last"
        )
        conn.execute(
            "DELETE FROM whatsapp_chats "
            "WHERE chat_key NOT IN (SELECT chat_key FROM whatsapp_messages)"
        )
        conn.executemany(
            "UPDATE whatsapp_chats SET subject = ? WHERE chat_key = ?",
            ((subject, key) for key, subject in subjects.items() if subject),
        )
        conn.execute(
            "UPDATE whatsapp_chats SET label = CASE WHEN subject != '' THEN subject "
            "ELSE COALESCE((SELECT label FROM threads "
            "WHERE threads.thread_key = whatsapp_chats.chat_key), '') END"
        )
