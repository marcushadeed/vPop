"""
One row of the `whatsapp_messages` table, and how WhatsApp addresses (JIDs) become the
chat and sender keys stored in it.

A JID is `<user>@<server>`: `<phone>@s.whatsapp.net` for a person, `<id>@g.us` for a group,
and `<id>@lid` for a person under WhatsApp's privacy id, which the backup maps to a phone
JID where it can. Phone JIDs are stored as E.164 numbers like the text messages'
`thread_key`, so a person's WhatsApp chat and text thread share a key.
"""

import hashlib
from collections.abc import Mapping
from typing import NamedTuple

from vpop.sources.android_messages.model import Direction, normalize_address

PERSON = "s.whatsapp.net"
LID = "lid"
GROUP = "g.us"
# Chats vPop reads; broadcasts, channels, statuses and bots are left out.
CHAT_SERVERS = {PERSON, LID, GROUP}


def server(jid: str) -> str:
    """The part of a JID after `@`."""
    return jid.rpartition("@")[2].lower()


def address(jid: str, lid_to_phone: Mapping[str, str]) -> str:
    """
    The key for a JID: an E.164 number for a person (a privacy id mapped to their phone JID
    when the backup knows it), else the JID itself, lowercased.
    """
    jid = jid.strip().lower()
    if server(jid) == LID:
        jid = lid_to_phone.get(jid, jid)
    user, _, host = jid.partition("@")
    if host == PERSON:
        # A device suffix (`12405551234:3@s.whatsapp.net`) names the same person.
        return normalize_address("+" + user.partition(":")[0])
    return jid


class WhatsAppMessage(NamedTuple):
    """
    One stored WhatsApp message. Mirrors the `whatsapp_messages` table.

    `chat_key` is the chat (see `address`); `sender` is who sent an incoming group message,
    the chat's own key for an incoming 1:1 message, and empty for outgoing ones.
    `attachments` is the content type of the attached media, if any.
    """

    key_id: str
    direction: Direction
    chat_key: str
    sender: str
    epoch_ms: int
    timestamp: str
    body: str
    attachments: str = ""

    def hash(self) -> str:
        """
        The message's primary key. WhatsApp's message id is unique, so it's hashed with the
        direction alone: the chat key can change once a privacy id is mapped to a phone.
        """
        key = f"whatsapp\x1f{self.key_id}\x1f{self.direction.value}"
        return hashlib.sha256(key.encode()).hexdigest()
