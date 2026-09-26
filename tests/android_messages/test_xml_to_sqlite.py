"""Tests for converting Android Messages XML dumps to SQLite."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from lxml import etree

from data_gathering.android_messages import xml_to_sqlite
from data_gathering.android_messages.xml_to_sqlite import (
    Direction,
    Message,
    add_messages_to_sqlite,
    messages_from_xml,
    mms_from_message,
    normalize_address,
    sms_from_message,
    thread_key,
)

RCS_ID = "fnug63lfmjxxsjkg@rcs.google.com"


@pytest.mark.parametrize(
    "raw",
    ["+12405551234", "12405551234", "2405551234", "+1 (240) 555-1234", "240.555.1234"],
)
def test_normalize_address_us_numbers(raw: str) -> None:
    assert normalize_address(raw) == "+12405551234"


def test_normalize_address_non_phone_passes_through() -> None:
    assert normalize_address(RCS_ID) == RCS_ID
    assert normalize_address(" Someone@Example.com ") == "someone@example.com"


def test_normalize_address_other_numbers() -> None:
    assert normalize_address("+447911123456") == "+447911123456"
    assert normalize_address("72975") == "72975"


def test_normalize_address_empty() -> None:
    assert normalize_address("") == ""
    assert normalize_address(None) == ""


def test_thread_key_group_variants_collapse() -> None:
    variants = [
        "+13013252243;+12404624993;2022356333",
        "12022356333~+13013252243~+12404624993",
        "+12404624993;+12022356333;+13013252243;+12022356333",
    ]
    keys = {thread_key(v) for v in variants}
    assert keys == {"+12022356333,+12404624993,+13013252243"}


def test_thread_key_single_and_rcs() -> None:
    assert thread_key("2405551234") == "+12405551234"
    assert thread_key(RCS_ID) == RCS_ID
    assert thread_key("") == ""


def parse(xml: str) -> etree._Element:
    return etree.fromstring(xml)


@pytest.mark.parametrize(
    ("type_", "direction", "was_sent", "sender"),
    [
        ("1", Direction.INCOMING, True, "+12405551234"),
        ("2", Direction.OUTGOING, True, ""),
        ("3", Direction.INCOMING, False, "+12405551234"),
    ],
)
def test_sms_from_message(
    type_: str, direction: Direction, was_sent: bool, sender: str
) -> None:
    elem = parse(
        f'<sms address="2405551234" date="1754056776000" type="{type_}" '
        'body="hello" contact_name="Sam" />'
    )
    message = sms_from_message(elem)
    assert message.direction is direction
    assert message.was_sent is was_sent
    assert message.thread_key == "+12405551234"
    assert message.sender == sender
    assert message.contact_name == "Sam"
    assert message.body == "hello"
    assert len(message.timestamp) == len("2025-08-01 09:59:36")


GROUP_MMS = f"""
<mms address="{RCS_ID}" date="1754056776000" msg_box="{{box}}" contact_name="(Unknown)">
  <parts>
    <part ct="application/smil" text="&lt;smil/&gt;" />
    <part ct="text/plain" text="see you " />
    <part ct="image/jpeg" />
    <part ct="text/plain" text="at 7" />
  </parts>
  <addrs>
    <addr address="+12404418450" type="130" />
    <addr address="+14435423702" type="130" />
    <addr address="14435423702" type="137" />
    <addr address="+12405796060" type="151" />
  </addrs>
</mms>
"""


def test_mms_incoming_group_uses_from_addr_as_sender() -> None:
    message = mms_from_message(parse(GROUP_MMS.format(box="1")))
    assert message.direction is Direction.INCOMING
    assert message.thread_key == RCS_ID
    assert message.sender == "+14435423702"
    assert message.body == "see you at 7"


def test_mms_outgoing_has_no_sender() -> None:
    message = mms_from_message(parse(GROUP_MMS.format(box="2")))
    assert message.direction is Direction.OUTGOING
    assert message.sender == ""


def test_mms_without_addrs_falls_back_to_address() -> None:
    elem = parse(
        '<mms address="12405551234" date="1754056776000" msg_box="1">'
        '<parts><part ct="text/plain" text="hi" /></parts></mms>'
    )
    assert mms_from_message(elem).sender == "+12405551234"


def make_message(**overrides: object) -> Message:
    fields: dict[str, object] = {
        "direction": Direction.INCOMING,
        "was_sent": True,
        "thread_key": "+12405551234",
        "sender": "+12405551234",
        "contact_name": "Sam",
        "body": "hello",
        "timestamp": "2026-08-01 09:59:36",
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


def test_hash_ignores_contact_name() -> None:
    assert make_message().hash() == make_message(contact_name="Sam Smith").hash()


def test_hash_same_across_address_formats() -> None:
    plus = sms_from_message(
        parse('<sms address="+12405551234" date="1754056776000" type="1" body="x" />')
    )
    bare = sms_from_message(
        parse('<sms address="2405551234" date="1754056776000" type="1" body="x" />')
    )
    assert plus.hash() == bare.hash()


def test_hash_differs_on_body() -> None:
    assert make_message().hash() != make_message(body="bye").hash()


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vpop.db"
    monkeypatch.setattr(xml_to_sqlite, "db_path", lambda: path)
    return path


def row_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT count(*) FROM messages").fetchone()[0]


def test_add_messages_is_idempotent(db_file: Path) -> None:
    messages = [make_message(), make_message(body="second")]
    add_messages_to_sqlite(iter(messages))
    add_messages_to_sqlite(iter(messages))
    add_messages_to_sqlite(iter([make_message(contact_name="Renamed")]))
    assert row_count(db_file) == 2


def test_add_messages_creates_indexes(db_file: Path) -> None:
    add_messages_to_sqlite(iter([make_message()]))
    with closing(sqlite3.connect(db_file)) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert {"idx_messages_thread_ts", "idx_messages_ts"} <= names


def test_messages_from_xml_overlapping_dumps(tmp_path: Path, db_file: Path) -> None:
    first = tmp_path / "a.xml"
    second = tmp_path / "b.xml"
    first.write_text(
        "<smses>"
        '<sms address="+12405551234" date="1754056776000" type="1" body="a" '
        'contact_name="Sam" />'
        f"{GROUP_MMS.format(box='1')}"
        "</smses>"
    )
    second.write_text(
        "<smses>"
        '<sms address="2405551234" date="1754056776000" type="1" body="a" '
        'contact_name="Sam Smith" />'
        '<sms address="2405551234" date="1754056777000" type="2" body="b" />'
        "</smses>"
    )
    add_messages_to_sqlite(messages_from_xml(first))
    add_messages_to_sqlite(messages_from_xml(second))
    assert row_count(db_file) == 3


RCS_MESSAGE_ID = "891C0147-43E5-45E6-8456-0EF3E44BC227"

RCS_GROUP_MMS = f"""
<mms address="{RCS_ID}" date="1728242138000" msg_box="1" m_id="{RCS_MESSAGE_ID}"
     contact_name="(Unknown)">
  <parts><part ct="text/plain" text="in the parking lot" /></parts>
  <addrs>
    <addr address="+13015251780" type="137" />
    <addr address="+19196074173" type="151" />
  </addrs>
</mms>
"""

# The Samsung cloud backup also stores the group message as a 1:1 SMS from the sender.
RCS_SHADOW_SMS = (
    '<sms address="+13015251780" date="1728242138000" type="1" body="in the parking lot" '
    f'imdn_message_id="{RCS_MESSAGE_ID}" creator="com.samsung.android.scloud" />'
)


def test_rcs_copies_share_hash() -> None:
    mms = mms_from_message(parse(RCS_GROUP_MMS))
    sms = sms_from_message(parse(RCS_SHADOW_SMS))
    assert mms.rcs_message_id == sms.rcs_message_id == RCS_MESSAGE_ID
    assert mms.thread_key != sms.thread_key
    assert mms.hash() == sms.hash()


def test_null_rcs_id_is_ignored() -> None:
    elem = parse(
        '<mms address="2405551234" date="1754056776000" msg_box="1" m_id="null">'
        '<parts><part ct="text/plain" text="hi" /></parts></mms>'
    )
    assert mms_from_message(elem).rcs_message_id == ""


@pytest.mark.parametrize("mms_first", [True, False])
def test_rcs_duplicate_keeps_group_thread(
    tmp_path: Path, db_file: Path, mms_first: bool
) -> None:
    records = [RCS_GROUP_MMS, RCS_SHADOW_SMS]
    if not mms_first:
        records.reverse()
    dump = tmp_path / "dump.xml"
    dump.write_text(f"<smses>{''.join(records)}</smses>")
    add_messages_to_sqlite(messages_from_xml(dump))
    add_messages_to_sqlite(messages_from_xml(dump))

    with closing(sqlite3.connect(db_file)) as conn:
        rows = conn.execute(
            "SELECT thread_key, sender, rcs_message_id FROM messages"
        ).fetchall()
    assert rows == [(RCS_ID, "+13015251780", RCS_MESSAGE_ID)]
