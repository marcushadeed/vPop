"""Tests for parsing Android Messages XML backups."""

import os
import time
from pathlib import Path

import pytest
from lxml import etree

from vpop.sources.android_messages.model import (
    Direction,
    local_timestamp,
    normalize_address,
    thread_key,
)
from vpop.sources.android_messages.parse import (
    messages_from_xml,
    mms_from_element,
    sms_from_element,
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
        "+12404624993, +12022356333, +13013252243",
    ]
    keys = {thread_key(v) for v in variants}
    assert keys == {"+12022356333,+12404624993,+13013252243"}


def test_thread_key_single_and_rcs() -> None:
    assert thread_key("2405551234") == "+12405551234"
    assert thread_key(RCS_ID) == RCS_ID
    assert thread_key("") == ""


def parse(xml: str) -> etree._Element:
    return etree.fromstring(xml)


def sms(type_: str, date: str = "1754056776000", **attrs: str) -> etree._Element:
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return parse(
        f'<sms address="2405551234" date="{date}" type="{type_}" body="hello" '
        f'contact_name="Sam"{extra} />'
    )


@pytest.mark.parametrize(
    ("type_", "direction", "was_sent", "sender"),
    [
        ("1", Direction.INCOMING, True, "+12405551234"),
        ("2", Direction.OUTGOING, True, ""),
        ("4", Direction.OUTGOING, False, ""),  # outbox
        ("5", Direction.OUTGOING, False, ""),  # failed
        ("6", Direction.OUTGOING, False, ""),  # queued
    ],
)
def test_sms_from_element(
    type_: str, direction: Direction, was_sent: bool, sender: str
) -> None:
    message = sms_from_element(sms(type_))
    assert message is not None
    assert message.direction is direction
    assert message.was_sent is was_sent
    assert message.thread_key == "+12405551234"
    assert message.sender == sender
    assert message.contact_name == "Sam"
    assert message.body == "hello"
    assert message.epoch_ms == 1754056776000
    assert message.timestamp == "2025-08-01 09:59:36"  # America/New_York, from conftest


def test_drafts_and_undated_messages_are_skipped() -> None:
    assert sms_from_element(sms("3")) is None
    assert sms_from_element(sms("1", date="null")) is None
    assert sms_from_element(sms("1", date="soon")) is None


def test_hash_does_not_depend_on_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    east = sms_from_element(sms("1"))
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    west = sms_from_element(sms("1"))
    assert east is not None and west is not None
    assert east.timestamp != west.timestamp
    assert east.hash() == west.hash()


def test_dst_fall_back_keeps_messages_apart() -> None:
    # 01:30 EDT and 01:30 EST on 2025-11-02 read the same on the clock.
    first = sms_from_element(sms("1", date="1762061400000"))
    second = sms_from_element(sms("1", date="1762065000000"))
    assert first is not None and second is not None
    assert first.timestamp == second.timestamp == "2025-11-02 01:30:00"
    assert first.hash() != second.hash()


def test_local_timestamp_follows_tz() -> None:
    assert local_timestamp(0) == "1969-12-31 19:00:00"
    assert os.environ["TZ"] == "America/New_York"


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
    message = mms_from_element(parse(GROUP_MMS.format(box="1")))
    assert message is not None
    assert message.direction is Direction.INCOMING
    assert message.thread_key == RCS_ID
    assert message.sender == "+14435423702"
    assert message.body == "see you at 7"
    assert message.attachments == "image/jpeg"
    assert message.from_mms


def test_mms_outgoing_has_no_sender() -> None:
    message = mms_from_element(parse(GROUP_MMS.format(box="2")))
    assert message is not None
    assert message.direction is Direction.OUTGOING
    assert message.sender == ""


def test_mms_draft_is_skipped() -> None:
    assert mms_from_element(parse(GROUP_MMS.format(box="3"))) is None


def test_mms_without_addrs_falls_back_to_address() -> None:
    elem = parse(
        '<mms address="12405551234" date="1754056776000" msg_box="1">'
        '<parts><part ct="text/plain" text="hi" /></parts></mms>'
    )
    message = mms_from_element(elem)
    assert message is not None
    assert message.sender == "+12405551234"


def test_hash_ignores_contact_name() -> None:
    message = sms_from_element(sms("1"))
    assert message is not None
    assert message.hash() == message._replace(contact_name="Sam Smith").hash()


def test_hash_same_across_address_formats() -> None:
    plus = sms_from_element(
        parse('<sms address="+12405551234" date="1754056776000" type="1" body="x" />')
    )
    bare = sms_from_element(
        parse('<sms address="2405551234" date="1754056776000" type="1" body="x" />')
    )
    assert plus is not None and bare is not None
    assert plus.hash() == bare.hash()


def test_hash_differs_on_body_and_attachments() -> None:
    message = sms_from_element(sms("1"))
    assert message is not None
    assert message.hash() != message._replace(body="bye").hash()
    assert message.hash() != message._replace(attachments="image/png").hash()


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
    f'imdn_message_id="{RCS_MESSAGE_ID}" creator="com.samsung.android.scloud" '
    'contact_name="Pat" />'
)


def test_rcs_copies_share_hash() -> None:
    mms = mms_from_element(parse(RCS_GROUP_MMS))
    sms_copy = sms_from_element(parse(RCS_SHADOW_SMS))
    assert mms is not None and sms_copy is not None
    assert mms.rcs_message_id == sms_copy.rcs_message_id == RCS_MESSAGE_ID
    assert mms.thread_key != sms_copy.thread_key
    assert mms.hash() == sms_copy.hash()


def test_null_rcs_id_is_ignored() -> None:
    elem = parse(
        '<mms address="2405551234" date="1754056776000" msg_box="1" m_id="null">'
        '<parts><part ct="text/plain" text="hi" /></parts></mms>'
    )
    message = mms_from_element(elem)
    assert message is not None
    assert message.rcs_message_id == ""


def test_messages_from_xml_streams_everything(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dump = tmp_path / "dump.xml"
    dump.write_text(
        "<smses>"
        '<sms address="2405551234" date="1754056776000" type="1" body="a" />'
        '<sms address="2405551234" date="1754056777000" type="3" body="draft" />'
        '<sms address="2405551234" date="null" type="1" body="undated" />'
        f"{GROUP_MMS.format(box='1')}"
        '<sms address="2405551234" date="1754056778000" type="2" body="b" />'
        "</smses>"
    )
    bodies = [m.body for m in messages_from_xml(dump)]
    assert bodies == ["a", "see you at 7", "b"]
    assert "skipped 1 message(s) without a date" in caplog.text
