"""
This module is used to convert MMS and RCS messages from the Android Messages app to a SQLite
database.
"""

import hashlib
import re
import sqlite3
from collections.abc import Generator, Iterable
from contextlib import closing
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from lxml.etree import _Element, iterparse

from data_gathering.file_management.data_paths import db_path


class Direction(Enum):
    """
    Direction of the message.
    """

    INCOMING = "incoming"
    OUTGOING = "outgoing"


def iso_timestamp(epoch_millis: str | None) -> str:
    """
    Convert an Android epoch-milliseconds timestamp to `YYYY-MM-DD HH:MM:SS` local time.

    The XML also carries a `readable_date` attribute, but it is slash-formatted, which SQLite's
    date functions reject: `date('2026/08/01 09:59:36')` is NULL, so any query filtering on a
    date range silently returns nothing. Storing ISO-8601 keeps the same local wall-clock reading
    while sorting correctly as text and working with `date()` / `strftime()`.
    """
    if not epoch_millis:
        return ""
    try:
        seconds = int(epoch_millis) / 1000
    except ValueError:
        return ""
    local = datetime.fromtimestamp(seconds, tz=UTC).astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S")


PHONE_CHARS = re.compile(r"^[\d\s+\-().]+$")

# PDU address type for the sender of an MMS (the `From` header).
MMS_FROM_ADDR_TYPE = "137"


def normalize_address(raw: str | None) -> str:
    """
    Normalize a single phone number so one person maps to one key across dumps.

    The same number appears as `+12405551234`, `12405551234` or `2405551234` depending on the
    dump. US numbers are coerced to E.164 (`+1XXXXXXXXXX`); other numbers keep their digits and
    any leading `+`. Non-phone addresses (RCS group ids, email, alphanumeric senders) are only
    lowercased and trimmed.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if not PHONE_CHARS.match(raw):
        return raw.lower()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if raw.startswith("+"):
        return f"+{digits}"
    return digits


def thread_key(raw_address: str | None) -> str:
    """
    Return a stable key for the conversation a message belongs to.

    Group addresses list participants joined by `~` or `;` in no fixed order, so the same group
    shows up under several spellings. Participants are normalized, deduplicated and sorted.
    RCS group ids (`...@rcs.google.com`) are already stable and pass through unchanged.
    """
    if not raw_address:
        return ""
    parts = {normalize_address(part) for part in re.split(r"[~;]", raw_address)}
    parts.discard("")
    return ",".join(sorted(parts))


class Message(NamedTuple):
    """
    Platform-agnostic message. Mirrors SQLite message table schema.

    `sender` is the normalized address of whoever sent an incoming message, and empty for
    outgoing ones. `contact_name` is display-only: it changes when a contact is renamed, so it is
    left out of the hash.
    """

    direction: Direction
    was_sent: bool
    thread_key: str
    sender: str
    contact_name: str
    body: str
    timestamp: str

    def hash(self) -> str:
        """
        Return a hash of the message's stable fields.
        """
        return hashlib.sha256(
            "\x1f".join(
                (
                    self.direction.value,
                    str(self.was_sent),
                    self.thread_key,
                    self.sender,
                    self.timestamp,
                    self.body,
                )
            ).encode()
        ).hexdigest()


def mms_from_message(elem: _Element) -> Message:
    """
    Parse an MMS message and return a Message object.
    """
    contact_name = elem.get("contact_name") or ""
    timestamp = iso_timestamp(elem.get("date"))
    address = elem.get("address") or ""

    message_type = elem.get("msg_box")
    if message_type == "1":
        direction = Direction.INCOMING
        was_sent = True
    elif message_type == "2":
        direction = Direction.OUTGOING
        was_sent = True
    else:  # draft, outbox, etc.
        direction = Direction.INCOMING
        was_sent = False

    body = ""
    for parts_list in elem.findall("parts"):
        for part in parts_list.findall("part"):
            if part.get("ct") == "text/plain":
                body += part.get("text") or ""

    sender = ""
    if direction is Direction.INCOMING:
        # In a group the thread address is the whole group; the `From` addr names who sent it.
        from_addr = elem.find(f"addrs/addr[@type='{MMS_FROM_ADDR_TYPE}']")
        from_value = from_addr.get("address") if from_addr is not None else None
        sender = normalize_address(from_value or address)

    return Message(
        direction, was_sent, thread_key(address), sender, contact_name, body, timestamp
    )


def sms_from_message(elem: _Element) -> Message:
    """
    Parse an SMS message and return a Message object.
    """
    contact_name = elem.get("contact_name") or ""
    timestamp = iso_timestamp(elem.get("date"))
    address = elem.get("address") or ""
    body = elem.get("body") or ""

    message_type = elem.get("type")
    if message_type == "1":
        direction = Direction.INCOMING
        was_sent = True
    elif message_type == "2":
        direction = Direction.OUTGOING
        was_sent = True
    else:  # draft, outbox, etc.
        direction = Direction.INCOMING
        was_sent = False

    sender = normalize_address(address) if direction is Direction.INCOMING else ""

    return Message(
        direction, was_sent, thread_key(address), sender, contact_name, body, timestamp
    )


def messages_from_xml(file_path: Path) -> Generator[Message, None, None]:
    """
    Parse the SMS file and return a list of Message objects.
    """
    for _, elem in iterparse(
        file_path.as_posix(), events=("end",), recover=True, encoding="utf-8"
    ):
        if elem.tag == "mms":
            yield mms_from_message(elem)
        elif elem.tag == "sms":
            yield sms_from_message(elem)
        else:
            continue

        # Clean up only if we've parsed the message
        elem.clear()


def add_messages_to_sqlite(messages: Iterable[Message]):
    """
    Add a list of Message objects to a SQLite database. Creates the table if it doesn't exist.

    `closing` is what actually releases the handle: `with sqlite3.connect(...)` manages the
    transaction, not the connection, so on its own it leaves the connection open once the block
    exits. `sync` calls this once per XML file, so that leaks a handle per file.
    """
    with closing(sqlite3.connect(str(db_path()))) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS messages "
            "(id TEXT PRIMARY KEY, direction TEXT, was_sent INTEGER, "
            "thread_key TEXT, sender TEXT, contact_name TEXT, body TEXT, timestamp TEXT)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_thread_ts "
            "ON messages (thread_key, timestamp)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (timestamp)"
        )
        for message in messages:
            message_id = message.hash()
            cursor.execute(
                "INSERT OR IGNORE INTO messages "
                "(id, direction, was_sent, thread_key, sender, contact_name, body, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    message.direction.value,
                    message.was_sent,
                    message.thread_key,
                    message.sender,
                    message.contact_name,
                    message.body,
                    message.timestamp,
                ),
            )

        conn.commit()


def xml_to_sqlite(xml_path: Path):
    """
    Convert an XML file to a SQLite database.
    """
    messages = messages_from_xml(xml_path)
    add_messages_to_sqlite(messages)
