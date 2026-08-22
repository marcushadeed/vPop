"""
This module is used to convert MMS and RCS messages from the Android Messages app to a SQLite
database.
"""

from enum import Enum
from typing import Any, Generator, NamedTuple
from lxml.etree import Element, iterparse


class Message(NamedTuple):
    """
    Platform-agnostic message. Mirrors SQLite message table schema.
    """

    class Direction(Enum):
        """
        Direction of the message.
        """

        INCOMING = "incoming"
        OUTGOING = "outgoing"

    direction: Direction
    was_sent: bool
    contact_address: str
    contact_name: str
    body: str
    timestamp: str


def mms_from_message(elem: Element) -> Message:
    """
    Parse an MMS message and return a Message object.
    """
    contact_name = elem.get("contact_name")
    timestamp = elem.get("readable_date")
    contact_address = elem.get("address")

    message_type = elem.get("msg_box")
    if message_type == "1":
        direction = Message.Direction.INCOMING
        was_sent = True
    elif message_type == "2":
        direction = Message.Direction.OUTGOING
        was_sent = True
    else:  # draft, outbox, etc.
        direction = Message.Direction.INCOMING
        was_sent = False

    body = ""
    for parts_list in elem.findall("parts"):
        for part in parts_list.findall("part"):
            if part.get("ct") == "text/plain":
                body += part.get("text")

    return Message(direction, was_sent, contact_address, contact_name, body, timestamp)


def sms_from_message(elem: Element) -> Message:
    """
    Parse an SMS message and return a Message object.
    """
    contact_name = elem.get("contact_name")
    timestamp = elem.get("readable_date")
    contact_address = elem.get("address")
    body = elem.get("body")

    message_type = elem.get("type")
    if message_type == "1":
        direction = Message.Direction.INCOMING
        was_sent = True
    elif message_type == "2":
        direction = Message.Direction.OUTGOING
        was_sent = True
    else:  # draft, outbox, etc.
        direction = Message.Direction.INCOMING
        was_sent = False

    return Message(direction, was_sent, contact_address, contact_name, body, timestamp)


def messages_from_xml(file_path: str) -> Generator[Message, None, None]:
    """
    Parse the SMS file and return a list of Message objects.
    """

    for _, elem in iterparse(
        file_path, events=("end",), recover=True, encoding="utf-8"
    ):
        if elem.tag == "mms":
            yield mms_from_message(elem)
        elif elem.tag == "sms":
            yield sms_from_message(elem)
        else:
            continue

        # Clean up only if we've parsed the message
        elem.clear()
