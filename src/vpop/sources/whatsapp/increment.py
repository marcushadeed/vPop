"""
Read a decrypted increment: a ZIP whose `messages.bin` holds the messages added or changed
since the last backup.

`messages.bin` is a run of varint-length-prefixed chunks, each a `HistorySync` protobuf as
in WhatsApp's open-source protocol definitions (WAProto): conversations (field 2) with their
messages as `WebMessageInfo`, and phone-to-privacy-id mappings (field 15). WhatsApp
publishes no schema for it, so a new WhatsApp version can change it; the field numbers below
are the ones those definitions give.
"""

import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path

from vpop.sources.whatsapp.msgstore import Backup, make_message
from vpop.sources.whatsapp.protobuf import (
    fields,
    first_bytes,
    first_int,
    first_text,
    varint,
)

MESSAGES_FILE = "messages.bin"

# HistorySync
CONVERSATIONS = 2
PHONE_LID_MAPPINGS = 15
# Conversation: its JID, its messages (HistorySyncMsg, the WebMessageInfo in field 1), and
# a group's subject.
CONVERSATION_ID = 1
CONVERSATION_MESSAGES = 2
CONVERSATION_NAME = 13
# WebMessageInfo
INFO_KEY = 1
INFO_MESSAGE = 2
INFO_TIMESTAMP = 3  # seconds
INFO_PARTICIPANT = 5
# MessageKey
KEY_CHAT = 1
KEY_FROM_ME = 2
KEY_ID = 3
KEY_PARTICIPANT = 4
# Message: plain text, text with a link preview or quote, and media with the fields holding
# their content type and caption.
CONVERSATION_TEXT = 1
EXTENDED_TEXT = 6
MEDIA: dict[int, tuple[int, int | None]] = {
    3: (2, 3),  # image: mimetype, caption
    7: (2, None),  # document
    8: (2, None),  # audio
    9: (2, 7),  # video: mimetype, caption
    26: (5, None),  # sticker
}
# Messages that wrap another Message in their field 1: view-once, ephemeral, document with
# caption, view-once v2 and its extension.
WRAPPERS = {37, 40, 53, 55, 59}


def content(message: bytes) -> tuple[str, str]:
    """
    A Message's text and media content type. Anything else (reactions, edits, deletions,
    polls) comes out empty and is skipped.
    """
    for number, value in fields(message):
        if not isinstance(value, bytes):
            continue
        if number == CONVERSATION_TEXT:
            return value.decode("utf-8", errors="replace"), ""
        if number == EXTENDED_TEXT:
            return first_text(value, 1), ""
        if number in MEDIA:
            mime_field, caption_field = MEDIA[number]
            caption = first_text(value, caption_field) if caption_field else ""
            return caption, first_text(value, mime_field)
        if number in WRAPPERS:
            inner = first_bytes(value, 1)
            if inner:
                return content(inner)
    return "", ""


def chunks(data: bytes) -> Iterator[bytes]:
    """The length-prefixed chunks of `messages.bin`."""
    pos = 0
    while pos < len(data):
        size, pos = varint(data, pos)
        if pos + size > len(data):
            raise ValueError(f"{MESSAGES_FILE} chunk runs past the end of the file")
        yield data[pos : pos + size]
        pos += size


def read_increment(path: Path, lid_to_phone: Mapping[str, str]) -> Backup:
    """
    Read a decrypted increment. `lid_to_phone` comes from the full backup it follows; the
    increment's own mappings are added to it. Raises `ValueError` or `zipfile.BadZipFile`
    for a file in a format this doesn't know.
    """
    with zipfile.ZipFile(path) as archive:
        data = archive.read(MESSAGES_FILE)
    sync_chunks = list(chunks(data))
    mapping = dict(lid_to_phone)
    for chunk in sync_chunks:
        for number, value in fields(chunk):
            if number == PHONE_LID_MAPPINGS and isinstance(value, bytes):
                phone, lid = first_text(value, 1).lower(), first_text(value, 2).lower()
                if phone and lid:
                    mapping[lid] = phone
    backup = Backup([], mapping, {})
    for chunk in sync_chunks:
        for number, value in fields(chunk):
            if number == CONVERSATIONS and isinstance(value, bytes):
                read_conversation(value, backup)
    return backup


def read_conversation(conversation: bytes, backup: Backup) -> None:
    """Add a Conversation's messages and subject to `backup`."""
    chat_jid = first_text(conversation, CONVERSATION_ID)
    name = first_text(conversation, CONVERSATION_NAME)
    for number, value in fields(conversation):
        if number != CONVERSATION_MESSAGES or not isinstance(value, bytes):
            continue
        info = first_bytes(value, 1)
        key = first_bytes(info, INFO_KEY)
        body, mime = content(first_bytes(info, INFO_MESSAGE))
        message = make_message(
            key_id=first_text(key, KEY_ID),
            from_me=bool(first_int(key, KEY_FROM_ME)),
            epoch_ms=first_int(info, INFO_TIMESTAMP) * 1000,
            body=body,
            chat_jid=first_text(key, KEY_CHAT) or chat_jid,
            sender_jid=first_text(key, KEY_PARTICIPANT)
            or first_text(info, INFO_PARTICIPANT),
            mime_type=mime,
            lid_to_phone=backup.lid_to_phone,
        )
        if message is not None and message.key_id and message.epoch_ms:
            backup.messages.append(message)
    if name and chat_jid.lower().endswith("@g.us"):
        backup.subjects[chat_jid.lower()] = name
