"""Tests for `list_sources`: what each source holds, and how fresh it is."""

from pathlib import Path

from helpers import build_db, make_message

from vpop.config import Config, WhatsAppConfig
from vpop.paths import db_path
from vpop.sources.registry import enabled_sources
from vpop.sources.sync import SyncStatus, write_status
from vpop.tools.status import StatusTools

BOTH = Config(whatsapp=WhatsAppConfig(enabled=True))


def status(config: Config = BOTH) -> list[str]:
    return StatusTools(db_path(), enabled_sources(config)).list_sources().splitlines()


def test_each_source_says_what_it_holds() -> None:
    build_db(
        db_path(),
        [
            make_message(timestamp="2026-08-01 09:00:00"),
            make_message(body="later", timestamp="2026-10-17 21:30:00"),
        ],
    )
    lines = status()
    assert lines[0].startswith("Now: ")
    assert lines[1:] == [
        (
            "- android_messages: text messages (SMS, MMS and RCS) from an Android "
            "phone, as `text:` conversations"
        ),
        "  messages: 2 messages, 2026-08-01 09:00:00 → 2026-10-17 21:30:00",
        "  no sync recorded; run `vpop sync`",
        "- whatsapp: WhatsApp messages from an Android phone, as `whatsapp:` conversations",
        "  messages: no messages",
        "  no sync recorded; run `vpop sync`",
    ]


def test_each_source_says_how_its_last_sync_went() -> None:
    build_db(db_path(), [make_message()])
    write_status(
        {
            "android_messages": SyncStatus(
                "2026-10-18 10:00:00", "2026-10-18 10:00:00", None
            ),
            "whatsapp": SyncStatus(
                "2026-10-18 10:00:00",
                "2026-10-17 09:00:00",
                "no WhatsApp backup key.",
            ),
        }
    )
    lines = status()
    assert lines[3] == "  last synced 2026-10-18 10:00:00"
    assert lines[6] == (
        "  last sync failed at 2026-10-18 10:00:00: no WhatsApp backup key. "
        "(last success: 2026-10-17 09:00:00)"
    )


def test_only_enabled_sources_are_listed() -> None:
    build_db(db_path(), [])
    assert not any("whatsapp" in line for line in status(Config()))


def test_a_missing_database_says_to_sync(tmp_path: Path) -> None:
    lines = StatusTools(tmp_path / "none.db", enabled_sources(BOTH)).list_sources()
    assert lines.count("Run `vpop sync` first.") == 2
