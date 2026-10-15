"""Read a decrypted full backup, WhatsApp's own `msgstore.db` SQLite database."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

from vpop.sources import SourceError
from vpop.sources.android_messages.model import Direction, local_timestamp
from vpop.sources.whatsapp.model import CHAT_SERVERS, WhatsAppMessage, address, server

REQUIRED_TABLES = {
    "message",
    "chat",
    "jid",
    "jid_map",
    "message_media",
    "message_system",
}

MESSAGES = (
    "SELECT m.key_id, m.from_me, m.timestamp, m.text_data, chat_jid.raw_string, "
    "sender_jid.raw_string, media.mime_type "
    "FROM message AS m "
    "JOIN chat ON chat._id = m.chat_row_id "
    "JOIN jid AS chat_jid ON chat_jid._id = chat.jid_row_id "
    "LEFT JOIN jid AS sender_jid ON sender_jid._id = m.sender_jid_row_id "
    "LEFT JOIN message_media AS media ON media.message_row_id = m._id "
    "WHERE m.key_id IS NOT NULL AND m.timestamp > 0 "
    "AND m._id NOT IN (SELECT message_row_id FROM message_system)"
)
LID_MAP = (
    "SELECT lid.raw_string, phone.raw_string FROM jid_map "
    "JOIN jid AS lid ON lid._id = jid_map.lid_row_id "
    "JOIN jid AS phone ON phone._id = jid_map.jid_row_id"
)
SUBJECTS = (
    "SELECT jid.raw_string, chat.subject FROM chat JOIN jid ON jid._id = chat.jid_row_id "
    "WHERE chat.subject IS NOT NULL AND chat.subject != ''"
)


class Backup(NamedTuple):
    """
    What a backup says: its messages, its map from privacy ids to phone JIDs (lowercased),
    and each group's subject by chat key.
    """

    messages: list[WhatsAppMessage]
    lid_to_phone: dict[str, str]
    subjects: dict[str, str]


def make_message(  # pylint: disable=too-many-arguments
    *,
    key_id: str,
    from_me: bool,
    epoch_ms: int,
    body: str,
    chat_jid: str,
    sender_jid: str,
    mime_type: str,
    lid_to_phone: dict[str, str],
) -> WhatsAppMessage | None:
    """
    A message from either kind of backup, or None for one vPop leaves out: outside a
    person's or group chat, or with neither text nor media (calls, deleted messages).
    """
    if server(chat_jid) not in CHAT_SERVERS or not (body or mime_type):
        return None
    chat_key = address(chat_jid, lid_to_phone)
    if from_me:
        sender = ""
    elif server(chat_jid) == "g.us":
        sender = address(sender_jid, lid_to_phone) if sender_jid else ""
    else:
        sender = chat_key
    return WhatsAppMessage(
        key_id=key_id,
        direction=Direction.OUTGOING if from_me else Direction.INCOMING,
        chat_key=chat_key,
        sender=sender,
        epoch_ms=epoch_ms,
        timestamp=local_timestamp(epoch_ms),
        body=body,
        attachments=mime_type,
    )


def check_tables(conn: sqlite3.Connection, path: Path) -> None:
    """Raise unless the database has the tables of a WhatsApp version vPop can read."""
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.DatabaseError as exc:
        raise SourceError(f"{path.name} isn't a WhatsApp database: {exc}") from exc
    missing = REQUIRED_TABLES - tables
    if missing:
        raise SourceError(
            f"{path.name} is from a WhatsApp version vPop can't read (no "
            f"{', '.join(sorted(missing))} table)"
        )


def read_msgstore(path: Path) -> Backup:
    """
    Read a decrypted `msgstore.db`. System messages (joins, number changes, ...) are left
    out, as are the chats and messages `make_message` skips.
    """
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        check_tables(conn, path)
        lid_to_phone = {
            lid.lower(): phone.lower()
            for lid, phone in conn.execute(LID_MAP)
            if lid and phone
        }
        subjects = {
            address(jid, lid_to_phone): subject
            for jid, subject in conn.execute(SUBJECTS)
            if server(jid) == "g.us"
        }
        messages = []
        for key_id, from_me, epoch_ms, text, chat_jid, sender_jid, mime in conn.execute(
            MESSAGES
        ):
            message = make_message(
                key_id=key_id,
                from_me=bool(from_me),
                epoch_ms=int(epoch_ms),
                body=text or "",
                chat_jid=chat_jid or "",
                sender_jid=sender_jid or "",
                mime_type=mime or "",
                lid_to_phone=lid_to_phone,
            )
            if message is not None:
                messages.append(message)
    return Backup(messages, lid_to_phone, subjects)
