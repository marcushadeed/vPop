"""Tests for the database schema, migrations and connections."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from helpers import SAM, build_db, make_message

from vpop import db


def test_new_database_gets_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "vpop.db"
    with closing(db.connect(path)) as conn:
        assert db.user_version(conn) == db.SCHEMA_VERSION
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"messages", "messages_fts", "threads", "imports"} <= tables


def test_old_unversioned_database_must_be_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "vpop.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT)")
    with pytest.raises(db.DatabaseError, match="vpop sync --rebuild"):
        db.connect(path)
    with pytest.raises(db.DatabaseError, match="vpop sync --rebuild"):
        db.connect_readonly(path)


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "vpop.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(db.DatabaseError, match="newer version"):
        db.connect(path)


def test_missing_database_says_to_sync(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseError, match="vpop sync"):
        db.check_readable(tmp_path / "none.db")


def test_readonly_connection_cannot_write(tmp_path: Path) -> None:
    path = build_db(tmp_path / "vpop.db", [make_message()])
    with closing(db.connect_readonly(path)) as conn, pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM messages")


def test_threads_table_labels_and_counts(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "vpop.db",
        [
            make_message(contact_name="Sam", timestamp="2026-01-01 10:00:00"),
            make_message(contact_name="Sam Smith", timestamp="2026-02-01 10:00:00"),
            make_message(contact_name="(Unknown)", timestamp="2026-03-01 10:00:00"),
        ],
    )
    with closing(db.connect_readonly(path)) as conn:
        rows = conn.execute("SELECT * FROM threads").fetchall()
    assert rows == [(SAM, "Sam Smith", 3, "2026-01-01 10:00:00", "2026-03-01 10:00:00")]


def test_import_tracking_notices_changed_files(tmp_path: Path) -> None:
    backup = tmp_path / "sms-1.xml"
    backup.write_text("<smses/>")
    with closing(db.connect(tmp_path / "vpop.db")) as conn:
        assert not db.already_imported(conn, "src", backup)
        db.record_import(conn, "src", backup, 0)
        assert db.already_imported(conn, "src", backup)
        backup.write_text("<smses>changed</smses>")
        assert not db.already_imported(conn, "src", backup)
