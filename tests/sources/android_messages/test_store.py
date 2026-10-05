"""Tests for writing messages into the database."""

import sqlite3
from pathlib import Path

import pytest
from helpers import SAM, make_message, outgoing
from lxml import etree

from vpop import db
from vpop.sources.android_messages.parse import (
    messages_from_xml,
    mms_from_element,
    sms_from_element,
)
from vpop.sources.android_messages.store import insert_messages

RCS_ID = "fnug63lfmjxxsjkg@rcs.google.com"
RCS_MESSAGE_ID = "891C0147-43E5-45E6-8456-0EF3E44BC227"
RCS_GROUP_MMS = (
    f'<mms address="{RCS_ID}" date="1728242138000" msg_box="1" m_id="{RCS_MESSAGE_ID}" '
    'contact_name="Pat, Lee">'
    '<parts><part ct="text/plain" text="in the parking lot" /></parts>'
    '<addrs><addr address="+13015251780" type="137" /></addrs></mms>'
)
RCS_SHADOW_SMS = (
    '<sms address="+13015251780" date="1728242138000" type="1" body="in the parking lot" '
    f'imdn_message_id="{RCS_MESSAGE_ID}" contact_name="Pat" />'
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "vpop.db")
    yield connection  # type: ignore[misc]
    connection.close()


def rows(conn: sqlite3.Connection, columns: str) -> list[tuple[object, ...]]:
    return conn.execute(f"SELECT {columns} FROM messages ORDER BY epoch_ms").fetchall()


def test_insert_is_idempotent_and_counts_new_rows(conn: sqlite3.Connection) -> None:
    messages = [make_message(), make_message(body="second")]
    assert insert_messages(conn, messages) == 2
    assert insert_messages(conn, messages) == 0
    assert len(rows(conn, "id")) == 2


def test_contact_rename_reaches_sms_rows(conn: sqlite3.Connection) -> None:
    insert_messages(conn, [make_message()])
    insert_messages(conn, [make_message(contact_name="Samuel")])
    assert rows(conn, "contact_name") == [("Samuel",)]
    assert conn.execute("SELECT label FROM threads").fetchall() == [("Samuel",)]


def test_empty_name_never_replaces_a_known_one(conn: sqlite3.Connection) -> None:
    insert_messages(conn, [outgoing(contact_name="Sam Smith")])
    insert_messages(conn, [outgoing(contact_name="")])
    assert rows(conn, "contact_name") == [("Sam Smith",)]


@pytest.mark.parametrize("mms_first", [True, False])
def test_rcs_duplicate_keeps_group_thread(
    conn: sqlite3.Connection, mms_first: bool
) -> None:
    mms = mms_from_element(etree.fromstring(RCS_GROUP_MMS))
    sms = sms_from_element(etree.fromstring(RCS_SHADOW_SMS))
    pair = [mms, sms] if mms_first else [sms, mms]
    insert_messages(conn, pair)  # type: ignore[arg-type]
    insert_messages(conn, pair)  # type: ignore[arg-type]
    assert rows(conn, "thread_key, sender, contact_name, rcs_message_id") == [
        (RCS_ID, "+13015251780", "Pat, Lee", RCS_MESSAGE_ID)
    ]


def test_overlapping_dumps(conn: sqlite3.Connection, tmp_path: Path) -> None:
    first = tmp_path / "a.xml"
    second = tmp_path / "b.xml"
    first.write_text(
        '<smses><sms address="+12405551234" date="1754056776000" type="1" body="a" '
        f'contact_name="Sam" />{RCS_GROUP_MMS}</smses>'
    )
    second.write_text(
        '<smses><sms address="2405551234" date="1754056776000" type="1" body="a" '
        'contact_name="Sam Smith" />'
        '<sms address="2405551234" date="1754056777000" type="2" body="b" /></smses>'
    )
    insert_messages(conn, messages_from_xml(first))
    insert_messages(conn, messages_from_xml(second))
    assert len(rows(conn, "id")) == 3
    assert ("Sam Smith",) in rows(conn, "contact_name")


def test_full_text_index_follows_inserts(conn: sqlite3.Connection) -> None:
    insert_messages(conn, [make_message(body="We adopted a puppy")])
    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'pupp*'"
    ).fetchall()
    assert len(hits) == 1
    assert conn.execute("SELECT thread_key FROM threads").fetchall() == [(SAM,)]
