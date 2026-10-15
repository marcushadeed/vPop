"""Tests for reading full backups (msgstore.db) and increments."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from whatsapp_backups import (
    Msgstore,
    conversation,
    increment,
    pb,
    text_message,
    web_message,
)

from vpop.sources import SourceError
from vpop.sources.android_messages.model import Direction
from vpop.sources.whatsapp.increment import read_increment
from vpop.sources.whatsapp.model import address
from vpop.sources.whatsapp.msgstore import read_msgstore

SAM = "12405551234@s.whatsapp.net"
SAM_LID = "98765@lid"
GROUP = "120363000@g.us"
T = 1_790_000_000_000


@pytest.mark.parametrize(
    ("jid", "key"),
    [
        (SAM, "+12405551234"),
        ("12405551234:7@s.whatsapp.net", "+12405551234"),
        ("447700900123@s.whatsapp.net", "+447700900123"),
        (SAM_LID, "+12405551234"),
        ("555@LID", "555@lid"),
        (GROUP, GROUP),
    ],
)
def test_address(jid: str, key: str) -> None:
    assert address(jid, {SAM_LID: SAM}) == key


def test_msgstore(tmp_path: Path) -> None:
    store = Msgstore(lid_map=[(SAM_LID, SAM)])
    store.chat(GROUP, subject="Book club")
    store.add(SAM, "a", T, "hi")
    store.add(SAM, "b", T + 1, "hey", from_me=True)
    store.add(SAM_LID, "c", T + 2, "from my lid chat")
    store.add(GROUP, "d", T + 3, "group hello", sender=SAM_LID)
    store.add(GROUP, "e", T + 4, None, sender="1@lid", mime="image/jpeg")
    store.add(GROUP, "f", T + 5, "Sam joined", system=True)
    store.add(SAM, "g", T + 6, None)  # a call: no text, no media
    store.add("status@broadcast", "h", T + 7, "a status")
    backup = read_msgstore(store.write(tmp_path / "msgstore.db"))

    by_id = {m.key_id: m for m in backup.messages}
    assert sorted(by_id) == ["a", "b", "c", "d", "e"]
    assert by_id["a"].chat_key == by_id["a"].sender == "+12405551234"
    assert by_id["b"].direction is Direction.OUTGOING
    assert by_id["b"].sender == ""
    assert by_id["c"].chat_key == "+12405551234"
    assert by_id["d"].chat_key == GROUP
    assert by_id["d"].sender == "+12405551234"
    assert (by_id["e"].sender, by_id["e"].body, by_id["e"].attachments) == (
        "1@lid",
        "",
        "image/jpeg",
    )
    assert by_id["a"].timestamp == "2026-09-21 10:13:20"
    assert backup.subjects == {GROUP: "Book club"}
    assert backup.lid_to_phone == {SAM_LID: SAM}


def test_old_msgstore_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "msgstore.db"
    store = Msgstore()
    store.write(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DROP TABLE jid_map")
    with pytest.raises(SourceError, match="no jid_map table"):
        read_msgstore(path)


def test_increment(tmp_path: Path) -> None:
    other_lid, other = "4444@lid", "13015550000@s.whatsapp.net"
    ephemeral = pb((40, pb((1, text_message("disappearing")))))
    image = pb((3, pb((2, "image/jpeg"), (3, "look"))))
    extended = pb((6, pb((1, "with a link https://example.com"))))
    edit = pb((12, pb((2, 14))))
    path = tmp_path / "inc.zip"
    path.write_bytes(
        increment(
            [
                conversation(
                    SAM,
                    [
                        web_message(SAM, "a", 1_790_000_000, text_message("hi")),
                        web_message(SAM, "b", 1_790_000_001, extended, from_me=True),
                        web_message(SAM, "c", 1_790_000_002, image),
                        web_message(SAM, "d", 1_790_000_003, ephemeral),
                        web_message(SAM, "e", 1_790_000_004, edit),
                    ],
                ),
                conversation(
                    GROUP,
                    [
                        web_message(
                            GROUP,
                            "f",
                            1_790_000_005,
                            text_message("yo"),
                            participant=other_lid,
                        ),
                        web_message(
                            GROUP,
                            "g",
                            1_790_000_006,
                            text_message("me too"),
                            participant=SAM_LID,
                        ),
                    ],
                    name="Book club!",
                ),
            ],
            mappings=[(other, other_lid)],
        )
    )
    backup = read_increment(path, {SAM_LID: SAM})
    by_id = {m.key_id: m for m in backup.messages}
    assert sorted(by_id) == ["a", "b", "c", "d", "f", "g"]
    assert by_id["a"].epoch_ms == 1_790_000_000_000
    assert by_id["b"].body.startswith("with a link")
    assert by_id["b"].direction is Direction.OUTGOING
    assert (by_id["c"].body, by_id["c"].attachments) == ("look", "image/jpeg")
    assert by_id["d"].body == "disappearing"
    assert by_id["f"].sender == "+13015550000"
    assert by_id["g"].sender == "+12405551234"
    assert backup.subjects == {GROUP: "Book club!"}
    assert backup.lid_to_phone[other_lid] == other


def test_increment_in_an_unknown_format(tmp_path: Path) -> None:
    path = tmp_path / "inc.zip"
    path.write_bytes(increment([b"\x0a\x50short"]))
    with pytest.raises(ValueError):
        read_increment(path, {})
