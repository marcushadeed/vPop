"""Tests for downloading and importing WhatsApp backups (no network)."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from helpers import build_db, make_message
from whatsapp_backups import (
    KEY_HEX,
    Msgstore,
    conversation,
    encrypt,
    increment,
    text_message,
    web_message,
)

from vpop import db
from vpop.config import Config, WhatsAppConfig, parse_config
from vpop.paths import db_path, whatsapp_key_path, whatsapp_raw_dir
from vpop.sources import SourceError
from vpop.sources.registry import enabled_sources
from vpop.sources.sync import sync
from vpop.sources.whatsapp import fetch
from vpop.sources.whatsapp.fetch import download_latest
from vpop.sources.whatsapp.source import WhatsApp
from vpop.sources.whatsapp.sync import drive_folder_id, import_latest

SAM = "12405551234@s.whatsapp.net"
GROUP = "120363000@g.us"
T = 1_790_000_000_000
ENABLED = Config(whatsapp=WhatsAppConfig(enabled=True, drive_folder_id="folder"))


@pytest.fixture(autouse=True)
def key() -> Path:
    path = whatsapp_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(KEY_HEX + "\n")
    path.chmod(0o600)
    return path


def write_full(store: Msgstore, tmp_path: Path) -> Path:
    raw = whatsapp_raw_dir()
    raw.mkdir(parents=True, exist_ok=True)
    plain = store.write(tmp_path / f"plain-{len(list(tmp_path.iterdir()))}.db")
    path = raw / "msgstore.db.crypt15"
    path.write_bytes(encrypt(plain.read_bytes()))
    return path


def write_increment(number: int, *conversations: bytes) -> Path:
    path = whatsapp_raw_dir() / f"msgstore-increment-{number}.db.crypt15"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt(increment(list(conversations))))
    return path


def rows(sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(db_path())) as conn:
        return conn.execute(sql).fetchall()


def import_now() -> tuple[int, int]:
    with closing(db.connect(db_path())) as conn:
        return import_latest(conn, whatsapp_raw_dir())


def test_full_backup_then_increments(tmp_path: Path) -> None:
    store = Msgstore()
    store.chat(GROUP, "Book club")
    store.add(SAM, "a", T, "hi")
    store.add(GROUP, "b", T + 1000, "meeting at 7", sender=SAM)
    write_full(store, tmp_path)
    assert import_now() == (1, 2)
    assert import_now() == (0, 0)

    write_increment(
        1,
        conversation(
            SAM, [web_message(SAM, "c", T // 1000 + 60, text_message("later"))]
        ),
    )
    assert import_now() == (1, 1)
    assert rows(
        "SELECT chat_key, label, is_group, message_count FROM whatsapp_chats ORDER BY 1"
    ) == [
        ("+12405551234", "", 0, 2),
        (GROUP, "Book club", 1, 1),
    ]


def test_messages_deleted_on_the_phone_are_kept(tmp_path: Path) -> None:
    store = Msgstore()
    store.add(SAM, "a", T + 123, "deleted on the phone later")
    store.add(SAM, "b", T + 1000, "a caption")
    store.add(SAM, "c", T + 2000, "edited later")
    write_full(store, tmp_path)
    import_now()

    newer = Msgstore()
    newer.add(SAM, "b", T + 1000, None, mime="image/jpeg")  # the caption is gone
    newer.add(SAM, "c", T + 2000, "edited text")
    newer.add(SAM, "d", T + 3000, "new")
    write_full(newer, tmp_path)
    assert import_now() == (1, 1)
    assert rows(
        "SELECT key_id, body, attachments FROM whatsapp_messages ORDER BY key_id"
    ) == [
        ("a", "deleted on the phone later", ""),
        ("b", "a caption", "image/jpeg"),
        ("c", "edited text", ""),
        ("d", "new", ""),
    ]


def test_increment_keeps_the_first_time_and_full_precision(tmp_path: Path) -> None:
    write_increment(
        1, conversation(SAM, [web_message(SAM, "a", T // 1000, text_message("hi"))])
    )
    store = Msgstore()
    store.add(SAM, "a", T + 456, "hi")
    write_full(store, tmp_path)
    import_now()
    assert rows("SELECT epoch_ms FROM whatsapp_messages") == [(T + 456,)]


def test_chats_are_named_from_text_threads(tmp_path: Path) -> None:
    build_db(db_path(), [make_message(contact_name="Sam Smith")])
    store = Msgstore()
    store.add(SAM, "a", T, "hi")
    write_full(store, tmp_path)
    import_now()
    assert rows("SELECT label FROM whatsapp_chats") == [("Sam Smith",)]


def test_an_unreadable_increment_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = Msgstore()
    store.add(SAM, "a", T, "hi")
    write_full(store, tmp_path)
    bad = whatsapp_raw_dir() / "msgstore-increment-1.db.crypt15"
    bad.write_bytes(encrypt(b"not a zip"))
    write_increment(
        2, conversation(SAM, [web_message(SAM, "b", T // 1000 + 1, text_message("yo"))])
    )
    assert import_now() == (2, 2)
    assert "skipped msgstore-increment-1.db.crypt15" in caplog.text
    assert not [
        p for p in whatsapp_raw_dir().iterdir() if p.name.startswith(".decrypted")
    ]


def test_missing_key_says_where_to_put_it(tmp_path: Path, key: Path) -> None:
    store = Msgstore()
    store.add(SAM, "a", T, "hi")
    write_full(store, tmp_path)
    key.unlink()
    with pytest.raises(SourceError, match="chmod 600"):
        import_now()


def test_readable_key_is_warned_about(
    tmp_path: Path, key: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = Msgstore()
    write_full(store, tmp_path)
    key.chmod(0o644)
    import_now()
    assert "can be read by other users" in caplog.text


def test_bad_key_file(tmp_path: Path, key: Path) -> None:
    write_full(Msgstore(), tmp_path)
    key.write_text("not a key")
    with pytest.raises(SourceError, match="64 hex digits"):
        import_now()


def test_nothing_downloaded_imports_nothing() -> None:
    assert import_now() == (0, 0)


def test_whatsapp_is_off_unless_enabled() -> None:
    assert not any(isinstance(s, WhatsApp) for s in enabled_sources(Config()))
    assert any(isinstance(s, WhatsApp) for s in enabled_sources(ENABLED))


def test_rebuild_warns_about_deleted_messages(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = Msgstore()
    store.add(SAM, "a", T, "hi")
    write_full(store, tmp_path)
    sync(ENABLED, rebuild=True, offline=True)
    assert "won't have WhatsApp messages deleted on the phone" in caplog.text
    assert rows("SELECT COUNT(*) FROM whatsapp_messages") == [(1,)]


def test_folder_id() -> None:
    assert drive_folder_id(ENABLED) == "folder"
    with pytest.raises(SourceError, match=r"\[whatsapp\]"):
        drive_folder_id(parse_config("[whatsapp]\nenabled = true\n"))


def drive_file(
    name: str, size: int, modified: str = "2026-10-12T15:39:37.178Z"
) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": name,
        "size": str(size),
        "modifiedTime": modified,
    }


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stands in for the Drive folder: `listing` is what it holds, `fetched` what was downloaded."""
    state: dict[str, Any] = {"listing": [], "fetched": []}
    monkeypatch.setattr(
        fetch, "list_folder_files", lambda service, folder: state["listing"]
    )

    def download(service: object, file_id: str, dest: Path) -> None:
        state["fetched"].append(dest.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 10)

    monkeypatch.setattr(fetch, "download_file", download)
    return state


def test_download_latest_set(drive: dict[str, Any], tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "msgstore-increment-3.db.crypt15").write_bytes(b"old set")
    (raw / "msgstore-increment-1.db.crypt15").write_bytes(b"x" * 10)  # already current
    drive["listing"] = [
        drive_file("msgstore-increment-2.db.crypt15", 10),
        drive_file("msgstore.db.crypt15", 10),
        drive_file("msgstore-increment-1.db.crypt15", 10),
        drive_file("msgstore-2026-10-12.1.db.crypt15", 10),
        drive_file("msgstore-increment-1-2026-10-08.1.db.crypt14", 10),
    ]
    names = download_latest(object(), "folder", raw)
    assert names == ["msgstore.db.crypt15", "msgstore-increment-2.db.crypt15"]
    assert sorted(p.name for p in raw.iterdir()) == [
        "msgstore-increment-1.db.crypt15",
        "msgstore-increment-2.db.crypt15",
        "msgstore.db.crypt15",
    ]


def test_a_newer_file_with_the_same_name_and_size_is_fetched(
    drive: dict[str, Any], tmp_path: Path
) -> None:
    local = tmp_path / "msgstore.db.crypt15"
    local.write_bytes(b"x" * 10)
    os.utime(local, (1_700_000_000, 1_700_000_000))
    drive["listing"] = [drive_file("msgstore.db.crypt15", 10)]
    assert download_latest(object(), "folder", tmp_path) == ["msgstore.db.crypt15"]
    assert download_latest(object(), "folder", tmp_path) == []


def test_folder_without_crypt15_backups(drive: dict[str, Any], tmp_path: Path) -> None:
    drive["listing"] = [drive_file("msgstore.db.crypt14", 10)]
    with pytest.raises(SourceError, match="end-to-end encrypted backup"):
        download_latest(object(), "folder", tmp_path)
