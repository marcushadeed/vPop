"""
Builders for WhatsApp backups: crypt15 encryption, a minimal `msgstore.db`, and increments
(a ZIP of `HistorySync` protobuf chunks), shaped like the real ones.
"""

import hashlib
import io
import sqlite3
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vpop.sources.whatsapp.crypt import encryption_key

KEY_HEX = "00112233445566778899aabbccddeeff" * 2
KEY = bytes.fromhex(KEY_HEX)
IV = bytes(range(16))

ProtoField = tuple[int, "int | bytes | str"]


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pb(*fields: ProtoField) -> bytes:
    """A protobuf message: ints as varints, str and bytes length-delimited."""
    out = bytearray()
    for number, value in fields:
        if isinstance(value, int):
            out += varint(number << 3) + varint(value)
        else:
            data = value.encode() if isinstance(value, str) else value
            out += varint(number << 3 | 2) + varint(len(data)) + data
    return bytes(out)


def encrypt(
    plain: bytes, key: bytes = KEY, *, feature_flag: bool = True, md5: bool = True
) -> bytes:
    """`plain` as a crypt15 file."""
    header = pb((1, 1), (3, pb((1, IV))), (4, pb((1, "2.26.39.79"))))
    sealed = AESGCM(encryption_key(key)).encrypt(IV, zlib.compress(plain), None)
    data = bytes([len(header)]) + (b"\x01" if feature_flag else b"") + header + sealed
    return data + hashlib.md5(data).digest() if md5 else data


MSGSTORE_SCHEMA = """
CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER, sort_id INTEGER);
CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
CREATE TABLE message (
    _id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, key_id TEXT,
    sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT
);
CREATE TABLE message_media (message_row_id INTEGER, mime_type TEXT);
CREATE TABLE message_system (message_row_id INTEGER, action_type INTEGER);
"""


@dataclass
class Msgstore:
    """A `msgstore.db` being built: JIDs get row ids as they're first used."""

    jids: dict[str, int] = field(default_factory=dict)
    chats: dict[str, tuple[int, str]] = field(default_factory=dict)
    lid_map: list[tuple[str, str]] = field(default_factory=list)
    messages: list[tuple[object, ...]] = field(default_factory=list)
    media: list[tuple[int, str]] = field(default_factory=list)
    system: list[int] = field(default_factory=list)

    def jid(self, raw: str) -> int:
        return self.jids.setdefault(raw, len(self.jids) + 1)

    def chat(self, jid: str, subject: str = "") -> int:
        if jid not in self.chats:
            self.chats[jid] = (len(self.chats) + 1, subject)
        return self.chats[jid][0]

    def add(  # pylint: disable=too-many-arguments
        self,
        chat: str,
        key_id: str,
        epoch_ms: int,
        text: str | None,
        *,
        from_me: bool = False,
        sender: str = "",
        mime: str = "",
        system: bool = False,
    ) -> None:
        row_id = len(self.messages) + 1
        self.messages.append(
            (
                row_id,
                self.chat(chat),
                int(from_me),
                key_id,
                self.jid(sender) if sender else 0,
                epoch_ms,
                0,
                text,
            )
        )
        if mime:
            self.media.append((row_id, mime))
        if system:
            self.system.append(row_id)

    def write(self, path: Path) -> Path:
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(MSGSTORE_SCHEMA)
            for jid, (chat_id, subject) in self.chats.items():
                conn.execute(
                    "INSERT INTO chat VALUES (?, ?, ?)",
                    (chat_id, self.jid(jid), subject),
                )
            for lid, phone in self.lid_map:
                conn.execute(
                    "INSERT INTO jid_map VALUES (?, ?, 0)",
                    (self.jid(lid), self.jid(phone)),
                )
            conn.executemany(
                "INSERT INTO jid VALUES (?, ?, ?, ?)",
                (
                    (row, raw.partition("@")[0], raw.partition("@")[2], raw)
                    for raw, row in self.jids.items()
                ),
            )
            conn.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?)", self.messages
            )
            conn.executemany("INSERT INTO message_media VALUES (?, ?)", self.media)
            conn.executemany(
                "INSERT INTO message_system VALUES (?, 1)", ((r,) for r in self.system)
            )
            conn.commit()
        return path


def text_message(text: str) -> bytes:
    """A `Message` holding plain text."""
    return pb((1, text))


def web_message(  # pylint: disable=too-many-arguments
    chat: str,
    key_id: str,
    seconds: int,
    message: bytes,
    *,
    from_me: bool = False,
    participant: str = "",
) -> bytes:
    """A `HistorySyncMsg` holding a `WebMessageInfo`."""
    key: list[ProtoField] = [(1, chat), (2, int(from_me)), (3, key_id)]
    if participant:
        key.append((4, participant))
    info = pb((1, pb(*key)), (2, message), (3, seconds))
    return pb((1, info), (2, 1))


def conversation(jid: str, messages: Iterable[bytes], name: str = "") -> bytes:
    """A `Conversation` with its messages (from `web_message`)."""
    fields: list[ProtoField] = [(1, jid)]
    fields += [(2, message) for message in messages]
    if name:
        fields.append((13, name))
    return pb(*fields)


def increment(
    conversations: Sequence[bytes], mappings: Sequence[tuple[str, str]] = ()
) -> bytes:
    """
    An increment's ZIP: one `HistorySync` chunk per conversation, and the (phone, lid)
    mappings in the last one.
    """
    data = bytearray()
    for i, conv in enumerate(conversations):
        fields: list[ProtoField] = [(1, 2), (2, conv), (5, i)]
        if i == len(conversations) - 1:
            fields += [(15, pb((1, phone), (2, lid))) for phone, lid in mappings]
        chunk = pb(*fields)
        data += varint(len(chunk)) + chunk
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("messages.bin", bytes(data))
        archive.writestr("header.json", "{}")
    return buffer.getvalue()
