"""
Parse the XML backups SMS Backup & Restore writes (SMS, MMS and RCS from the Android Messages
app) into `Message`s.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

from lxml.etree import _Element, iterparse

from vpop.sources.android_messages.model import (
    Direction,
    Message,
    local_timestamp,
    normalize_address,
    thread_key,
)

log = logging.getLogger(__name__)

# PDU address type for the sender of an MMS (the `From` header).
MMS_FROM_ADDR_TYPE = "137"

# `type` (SMS) / `msg_box` (MMS) values: 1 inbox, 2 sent, 3 draft, 4 outbox, 5 failed,
# 6 queued. Everything but the inbox was written by the user.
INBOX = "1"
SENT = "2"
DRAFT = "3"

# MMS parts that carry no content of their own.
LAYOUT_PART_TYPES = {"text/plain", "application/smil"}


def attribute(elem: _Element, name: str) -> str:
    """
    Return an attribute's value, treating the literal `null` the backup writes as missing.
    """
    value = elem.get(name) or ""
    return "" if value == "null" else value


def epoch_ms(elem: _Element) -> int | None:
    """The message's `date` (epoch milliseconds), or None if it's missing or garbled."""
    try:
        return int(attribute(elem, "date"))
    except ValueError:
        return None


def classify(box: str | None) -> tuple[Direction, bool] | None:
    """Direction and whether it was sent, from an SMS `type` / MMS `msg_box`; None for drafts."""
    if box == INBOX:
        return Direction.INCOMING, True
    if box == DRAFT:
        return None
    return Direction.OUTGOING, box == SENT


def mms_from_element(elem: _Element) -> Message | None:
    """Parse an `<mms>` element; None for drafts and undated messages."""
    kind = classify(elem.get("msg_box"))
    when = epoch_ms(elem)
    if kind is None or when is None:
        return None
    direction, was_sent = kind
    address = elem.get("address") or ""

    body = ""
    attachments: list[str] = []
    for part in elem.iterfind("parts/part"):
        content_type = part.get("ct") or ""
        if content_type == "text/plain":
            body += part.get("text") or ""
        elif content_type not in LAYOUT_PART_TYPES:
            attachments.append(content_type or "application/octet-stream")

    sender = ""
    if direction is Direction.INCOMING:
        # In a group the thread address is the whole group; the `From` addr names who sent it.
        from_addr = elem.find(f"addrs/addr[@type='{MMS_FROM_ADDR_TYPE}']")
        from_value = from_addr.get("address") if from_addr is not None else None
        sender = normalize_address(from_value or address)

    return Message(
        direction=direction,
        was_sent=was_sent,
        thread_key=thread_key(address),
        sender=sender,
        contact_name=elem.get("contact_name") or "",
        body=body,
        epoch_ms=when,
        timestamp=local_timestamp(when),
        attachments=",".join(attachments),
        rcs_message_id=attribute(elem, "m_id"),
        from_mms=True,
    )


def sms_from_element(elem: _Element) -> Message | None:
    """Parse an `<sms>` element; None for drafts and undated messages."""
    kind = classify(elem.get("type"))
    when = epoch_ms(elem)
    if kind is None or when is None:
        return None
    direction, was_sent = kind
    address = elem.get("address") or ""
    return Message(
        direction=direction,
        was_sent=was_sent,
        thread_key=thread_key(address),
        sender=normalize_address(address) if direction is Direction.INCOMING else "",
        contact_name=elem.get("contact_name") or "",
        body=elem.get("body") or "",
        epoch_ms=when,
        timestamp=local_timestamp(when),
        rcs_message_id=attribute(elem, "imdn_message_id"),
    )


def messages_from_xml(path: Path) -> Iterator[Message]:
    """
    Stream the messages in a backup file. Drafts are skipped, and so are messages without a
    usable date (counted in a warning).

    Each parsed element is cleared and detached from the tree, so memory stays flat however
    big the backup is. `huge_tree` lets lxml read the multi-megabyte base64 attachments MMS
    parts carry, which it otherwise rejects (and `recover` would then silently drop).
    """
    undated = 0
    for _, elem in iterparse(
        path.as_posix(),
        events=("end",),
        tag=("sms", "mms"),
        recover=True,
        huge_tree=True,
        encoding="utf-8",
    ):
        parse = mms_from_element if elem.tag == "mms" else sms_from_element
        message = parse(elem)
        if message is not None:
            yield message
        elif epoch_ms(elem) is None:
            undated += 1
        elem.clear(keep_tail=True)
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]
    if undated:
        log.warning("%s: skipped %d message(s) without a date", path.name, undated)
