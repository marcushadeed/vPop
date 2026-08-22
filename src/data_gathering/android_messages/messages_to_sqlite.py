"""
This module is used to convert MMS and RCS messages from the Android Messages app to a SQLite
database.
"""

from typing import Generator, NamedTuple
from lxml.etree import Element, iterparse


class Message(NamedTuple):
    """
    Platform-agnostic message. Mirrors SQLite message table schema.
    """

    contact_name: str
    body: str
    timestamp: str


def mms_from_message(elem: Element) -> Message:
    """
    Parse an MMS message and return a Message object.
    """
    return Message(
        contact_name=elem.get("contact_name"),
        body=elem.findtext("body"),
        timestamp=elem.get("readable_date"),
    )


def sms_from_message(elem: Element) -> Message:
    """
    Parse an SMS message and return a Message object.
    """
    return Message(
        contact_name=elem.get("contact_name"),
        body=elem.findtext("body"),
        timestamp=elem.get("readable_date"),
    )


def messages_from_xml(file_path: str) -> Generator[Message, None, None]:
    """
    Parse the SMS file and return a list of Message objects.
    """

    for _, elem in iterparse(file_path, events=("end",), recover=True):
        if elem.tag == "mms":
            yield mms_from_message(elem)
        elif elem.tag == "sms":
            yield sms_from_message(elem)
        else:
            # mph: for debugging purposes
            if elem.tag not in ["parts", "part", "addr", "addrs"]:
                print(f"Unknown message type: {elem.tag}")

        # Clean up the XML tree to keep memory lightweight
        elem.clear()
        while elem.getparent() is not None and elem.getprevious() is not None:
            del elem.getparent()[0]


# mph: for debugging, print all messages to a file
def print_messages_to_file(
    all_messages: Generator[Message, None, None], file_path: str
) -> None:
    """
    Print all messages to a file.
    """
    with open(file_path, "w", encoding="utf-8") as f:
        for message in all_messages:
            f.write(f"{message.contact_name}\n{message.body}\n{message.timestamp}\n\n")
        print(f"Messages printed to {file_path}")


if __name__ == "__main__":
    messages = messages_from_xml(
        "/home/marcus/.local/share/vpop/raw/android-messages/sms-2026-07-30_03-30-10.xml"
    )
    print_messages_to_file(messages, "/tmp/android_messages.txt")
