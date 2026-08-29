"""
This module is used to convert MMS and RCS messages from the Android Messages app to a SQLite
database.
"""

import hashlib
import sqlite3
from collections.abc import Generator
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


class Message(NamedTuple):
    """
    Platform-agnostic message. Mirrors SQLite message table schema.
    """

    direction: Direction
    was_sent: bool
    contact_address: str
    contact_name: str
    body: str
    timestamp: str

    def hash(self) -> str:
        """
        Return a hash of the message.
        """
        return hashlib.sha256(
            (
                f"{self.direction.value}"
                f"{self.was_sent}"
                f"{self.contact_address}"
                f"{self.contact_name}"
                f"{self.body}"
                f"{self.timestamp}"
            ).encode()
        ).hexdigest()


def mms_from_message(elem: _Element) -> Message:
    """
    Parse an MMS message and return a Message object.
    """
    contact_name = elem.get("contact_name") or ""
    timestamp = iso_timestamp(elem.get("date"))
    contact_address = elem.get("address") or ""

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

    return Message(direction, was_sent, contact_address, contact_name, body, timestamp)


def sms_from_message(elem: _Element) -> Message:
    """
    Parse an SMS message and return a Message object.
    """
    contact_name = elem.get("contact_name") or ""
    timestamp = iso_timestamp(elem.get("date"))
    contact_address = elem.get("address") or ""
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

    return Message(direction, was_sent, contact_address, contact_name, body, timestamp)


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


def add_messages_to_sqlite(messages: Generator[Message, None, None]):
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
            "contact_address TEXT, contact_name TEXT, body TEXT, timestamp TEXT)"
        )
        for message in messages:
            message_id = message.hash()
            cursor.execute(
                "INSERT OR IGNORE INTO messages "
                "(id, direction, was_sent, contact_address, contact_name, body, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    message.direction.value,
                    message.was_sent,
                    message.contact_address,
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
