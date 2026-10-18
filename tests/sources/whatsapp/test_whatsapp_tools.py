"""Tests for the read-only WhatsApp query tools."""

from contextlib import closing
from pathlib import Path

import pytest
from helpers import SAM, build_db, epoch, make_message

from vpop import db
from vpop.sources.android_messages.model import Direction
from vpop.sources.whatsapp.model import WhatsAppMessage
from vpop.sources.whatsapp.store import insert_messages, refresh_chats
from vpop.sources.whatsapp.tools import WhatsAppTools

ALEX = "+13015550000"
GROUP = "120363000@g.us"


def wa(  # pylint: disable=too-many-arguments
    key_id: str,
    chat: str,
    body: str,
    timestamp: str,
    *,
    sender: str | None = None,
    outgoing: bool = False,
    attachments: str = "",
) -> WhatsAppMessage:
    return WhatsAppMessage(
        key_id=key_id,
        direction=Direction.OUTGOING if outgoing else Direction.INCOMING,
        chat_key=chat,
        sender="" if outgoing else (sender or chat),
        epoch_ms=epoch(timestamp),
        timestamp=timestamp,
        body=body,
        attachments=attachments,
    )


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = build_db(tmp_path / "vpop.db", [make_message(body="a text")])
    with closing(db.connect(path)) as conn:
        insert_messages(
            conn,
            [
                wa("1", SAM, "Are you moving to Denver?", "2026-09-01 10:00:00"),
                wa("2", SAM, "Yes, in June", "2026-09-01 10:01:00", outgoing=True),
                wa("3", SAM, "", "2026-09-01 10:02:00", attachments="image/jpeg"),
                wa(
                    "4",
                    GROUP,
                    "book is 50% off_today",
                    "2026-09-02 18:00:00",
                    sender=ALEX,
                ),
                wa("5", GROUP, "count me in", "2026-09-02 18:05:00", sender=SAM),
                wa("6", GROUP, "who's that", "2026-09-02 18:06:00", sender="777@lid"),
            ],
        )
        refresh_chats(conn, {GROUP: "Book club"})
    return path


@pytest.fixture
def tools(db_file: Path) -> WhatsAppTools:
    return WhatsAppTools(db_file)


def test_find_chats_by_name_number_and_group(tools: WhatsAppTools) -> None:
    by_name = tools.find_chats("sam")
    assert f"{SAM} | Sam Smith | 3 msgs" in by_name
    assert f"{SAM} | Sam Smith" in tools.find_chats("(240) 555-1234")
    assert f"{GROUP} | Book club (group) | 3 msgs" in tools.find_chats("book")
    assert tools.find_chats("nobody") == "No WhatsApp chats match 'nobody'."
    with pytest.raises(ValueError, match="empty"):
        tools.find_chats("  ")


def test_search_by_words_and_substring(tools: WhatsAppTools) -> None:
    result = tools.search_whatsapp(text="move denver")
    assert result == (
        f"2026-09-01 10:00:00 | Sam Smith [{SAM}] | Sam Smith | Are you moving to Denver?"
    )
    assert "50% off_today" in tools.search_whatsapp(contains="0% off_")
    assert tools.search_whatsapp(contains="1% off") == "No WhatsApp messages match."


def test_search_names_group_senders(tools: WhatsAppTools) -> None:
    result = tools.search_whatsapp(chat_key=GROUP)
    assert result.splitlines() == [
        "2026-09-02 18:06:00 | 777@lid | who's that",
        "2026-09-02 18:05:00 | Sam Smith | count me in",
        f"2026-09-02 18:00:00 | {ALEX} | book is 50% off_today",
    ]


def test_search_filters_and_paging(tools: WhatsAppTools) -> None:
    assert tools.search_whatsapp(sender="me").endswith("| me | Yes, in June")
    assert "count me in" in tools.search_whatsapp(sender="240-555-1234", chat_key=GROUP)
    assert "[image/jpeg]" in tools.search_whatsapp(chat_key="2405551234", limit=1)
    page = tools.search_whatsapp(limit=2)
    assert page.splitlines()[-1] == "… 4 more (use offset=2)"
    assert tools.search_whatsapp(offset=10) == "No WhatsApp messages past offset 10."
    with pytest.raises(ValueError, match="direction"):
        tools.search_whatsapp(direction="sideways")
    with pytest.raises(ValueError, match='sender="me"'):
        tools.search_whatsapp(sender="me", direction="incoming")


def test_read_chat(tools: WhatsAppTools) -> None:
    result = tools.read_chat(SAM, around="2026-09-01 10:01:00", limit=2)
    assert result.splitlines() == [
        f"WhatsApp chat {SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-09-01 10:01:00 | me | Yes, in June",
        "2026-09-01 10:02:00 | Sam Smith | [image/jpeg]",
    ]
    assert tools.read_chat(GROUP, since="2026-09-02 18:05").splitlines()[1:] == [
        "2026-09-02 18:05:00 | Sam Smith | count me in",
        "2026-09-02 18:06:00 | 777@lid | who's that",
    ]
    assert "No WhatsApp messages in chat" in tools.read_chat("+19999999999")
