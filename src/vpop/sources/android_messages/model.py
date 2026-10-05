"""
The message model shared by the importer, the query tools and the benchmark: one row of the
`messages` table, and how phone numbers and conversations are keyed.
"""

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import NamedTuple


class Direction(Enum):
    """Whether the user received a message or wrote it."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"


PHONE_CHARS = re.compile(r"^[\d\s+\-().]+$")


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
    shows up under several spellings. Participants are normalized, deduplicated, sorted and
    joined with `,`; a key that is already comma-joined (even with spaces) comes out the same.
    RCS group ids (`...@rcs.google.com`) are already stable and only get lowercased.
    """
    if not raw_address:
        return ""
    parts = {normalize_address(part) for part in re.split(r"[~;,]", raw_address)}
    parts.discard("")
    return ",".join(sorted(parts))


def local_timestamp(epoch_ms: int) -> str:
    """
    An epoch-milliseconds time as `YYYY-MM-DD HH:MM:SS` in the machine's local time zone.

    This is the human-facing `timestamp` column: it sorts as text and works with SQLite's
    date() and strftime(), which reject the backup's slash-formatted `readable_date`. It is
    derived, never part of a message's identity, because it changes with the time zone.
    """
    return (
        datetime.fromtimestamp(epoch_ms / 1000)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


class Message(NamedTuple):
    """
    One stored message. Mirrors the `messages` table.

    `sender` is the normalized address of whoever sent an incoming message, and empty for
    outgoing ones. `was_sent` is false for an outgoing message that never went out (outbox,
    failed, queued). `contact_name` is display-only: it changes when a contact is renamed, so
    it is left out of the hash, as is the time-zone-dependent `timestamp`. `attachments` lists
    the content types of non-text MMS parts, comma-separated.

    `rcs_message_id` is the RCS message id, when the message has one. The backup stores some
    RCS group messages twice, as an MMS in the group thread and as an SMS that looks like a 1:1
    text from the sender, and this id is how the two are matched. `from_mms` marks the copy
    whose thread and sender win when both are imported.
    """

    direction: Direction
    was_sent: bool
    thread_key: str
    sender: str
    contact_name: str
    body: str
    epoch_ms: int
    timestamp: str
    attachments: str = ""
    rcs_message_id: str = ""
    from_mms: bool = False

    def hash(self) -> str:
        """
        Return a hash of the message's stable fields, its primary key.

        With an RCS id, only the id, direction and time are hashed, because the thread and
        sender differ between the MMS and SMS copies of the same message.
        """
        if self.rcs_message_id:
            key = f"rcs\x1f{self.rcs_message_id}\x1f{self.direction.value}\x1f{self.epoch_ms}"
            return hashlib.sha256(key.encode()).hexdigest()
        return hashlib.sha256(
            "\x1f".join(
                (
                    self.direction.value,
                    str(self.was_sent),
                    self.thread_key,
                    self.sender,
                    str(self.epoch_ms),
                    self.body,
                    self.attachments,
                )
            ).encode()
        ).hexdigest()
