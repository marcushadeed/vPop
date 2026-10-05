"""Tests for `vpop sync` on Android Messages (no network: backups are written locally)."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from vpop import db
from vpop.config import Config, load_config, parse_config
from vpop.paths import android_messages_raw_dir, config_path, db_path
from vpop.sources.android_messages import sync as sync_module
from vpop.sources.android_messages.sync import (
    SyncError,
    drive_folder_id,
    import_backups,
)
from vpop.sources.sync import sync


def backup(name: str, *bodies: str, contact: str = "Sam") -> Path:
    """Write a backup file of incoming SMS from one number into the raw directory."""
    raw = android_messages_raw_dir()
    raw.mkdir(parents=True, exist_ok=True)
    smses = "".join(
        f'<sms address="2405551234" date="{1754056776000 + i}" type="1" body="{body}" '
        f'contact_name="{contact}" />'
        for i, body in enumerate(bodies)
    )
    path = raw / name
    path.write_text(f"<smses>{smses}</smses>")
    return path


def count(path: Path, sql: str = "SELECT COUNT(*) FROM messages") -> object:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchone()[0]


def test_import_is_incremental() -> None:
    backup("sms-20260801.xml", "a", "b")
    with closing(db.connect(db_path())) as conn:
        assert import_backups(conn, android_messages_raw_dir()) == (1, 2)
        assert import_backups(conn, android_messages_raw_dir()) == (0, 0)
        backup("sms-20260802.xml", "a", "b", "c")
        assert import_backups(conn, android_messages_raw_dir()) == (1, 1)


def test_newest_backup_wins_contact_name() -> None:
    # Written newest first, so a directory-order import would get this wrong.
    backup("sms-20260802.xml", "a", contact="Sam Smith")
    backup("sms-20260801.xml", "a", contact="Sam")
    sync(Config(), offline=True)
    assert count(db_path(), "SELECT label FROM threads") == "Sam Smith"


def test_rebuild_replaces_an_outdated_database() -> None:
    backup("sms-20260801.xml", "a")
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY)")
    with pytest.raises(db.DatabaseError):
        sync(Config(), offline=True)
    sync(Config(), offline=True, rebuild=True)
    assert count(path) == 1
    assert not path.with_name("vpop.db.rebuild").exists()


def test_failed_rebuild_keeps_the_old_database(monkeypatch: pytest.MonkeyPatch) -> None:
    backup("sms-20260801.xml", "a")
    sync(Config(), offline=True)

    def boom(*_: object) -> tuple[int, int]:
        raise RuntimeError("parse error")

    monkeypatch.setattr(sync_module, "import_backups", boom)
    with pytest.raises(RuntimeError):
        sync(Config(), offline=True, rebuild=True)
    assert count(db_path()) == 1
    assert not db_path().with_name("vpop.db.rebuild").exists()


def test_sync_downloads_unless_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(sync_module, "download", lambda _, raw: calls.append(raw))
    sync(Config())
    sync(Config(), offline=True)
    assert calls == [android_messages_raw_dir()]


def test_folder_id_from_config() -> None:
    config = parse_config('[android_messages]\ndrive_folder_id = "abc"\n')
    assert drive_folder_id(config) == "abc"


def test_folder_id_moves_from_legacy_file(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    load_config()  # writes the default config
    legacy = config_path().parent / "remote-data-locations.env"
    legacy.write_text("MESSAGES_BACKUP_FOLDER_ID='legacy-id'\n")
    assert drive_folder_id(load_config()) == "legacy-id"
    assert load_config().android_messages.drive_folder_id == "legacy-id"
    assert "# true: a local Ollama model" in config_path().read_text()
    assert "moved the Drive folder id" in caplog.text


def test_missing_folder_id_says_where_to_set_it() -> None:
    with pytest.raises(SyncError, match=r"drive_folder_id under \[android_messages\]"):
        drive_folder_id(Config())


def test_offline_sync_with_no_backups_makes_an_empty_database() -> None:
    sync(Config(), offline=True)
    assert count(db_path()) == 0
    assert os.path.exists(db_path())
