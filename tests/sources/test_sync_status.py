"""Tests for `vpop sync` across sources: the status file, the lock, and syncing unattended."""

import json
import sqlite3
import sys
from collections.abc import Sequence

import pytest

from vpop import cli
from vpop.config import Config
from vpop.paths import config_path, db_path, oauth_credentials_path, sync_status_path
from vpop.sources import SourceError
from vpop.sources.base import Store, SyncedSource
from vpop.sources.google import auth
from vpop.sources.sync import SyncStatus, read_status, sync, sync_lock


class Fake(SyncedSource):
    """A synced source that fails where it's told to, and records what it was asked."""

    description = "fake messages"

    def __init__(self, name: str, *, fails: str = "") -> None:
        super().__init__(Config())
        self.name = name  # type: ignore[misc]
        self.fails = fails
        self.interactive: list[bool] = []

    def enabled(self) -> bool:
        return True

    def stores(self) -> list[Store]:
        return []

    def download(self, google_scopes: Sequence[str], *, interactive: bool) -> None:
        self.interactive.append(interactive)
        if self.fails == "download":
            raise SourceError(f"{self.name}: no Drive folder.\nSet one.")

    def import_raw(self, conn: sqlite3.Connection) -> str:
        if self.fails == "import":
            raise ValueError(f"{self.name}: bad backup")
        return f"{self.name} imported"


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> list[Fake]:
    fakes = [Fake("first"), Fake("second")]
    monkeypatch.setattr("vpop.sources.sync.enabled_sources", lambda _: fakes)
    return fakes


def test_success_is_recorded_for_every_source(sources: list[Fake]) -> None:
    sync(Config())
    status = read_status()
    assert set(status) == {"first", "second"}
    first = status["first"]
    assert first.last_attempt == first.last_success and first.last_error is None
    assert first.describe() == f"last synced {first.last_success}"
    assert sources[0].interactive == [True]


def test_a_failing_source_is_recorded_and_the_others_still_sync(
    sources: list[Fake], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    sync(Config())
    succeeded = read_status()["first"].last_success
    sources[0].fails = "download"
    with pytest.raises(SourceError, match="first: no Drive folder"):
        sync(Config())
    status = read_status()
    assert status["first"] == SyncStatus(
        last_attempt=status["first"].last_attempt,
        last_success=succeeded,
        last_error="first: no Drive folder. Set one.",
    )
    assert status["first"].describe().startswith("last sync failed at ")
    assert status["second"].last_error is None
    assert "second imported" in caplog.text
    sources[0].fails = ""
    sync(Config())
    assert read_status()["first"].last_error is None


def test_every_failure_is_reported(
    sources: list[Fake], caplog: pytest.LogCaptureFixture
) -> None:
    sources[0].fails = "download"
    sources[1].fails = "import"
    with pytest.raises(SourceError, match="first"):
        sync(Config())
    assert "second: bad backup" in caplog.text
    assert read_status()["second"].last_error == "second: bad backup"


def test_a_failed_rebuild_rebuilds_no_source(sources: list[Fake]) -> None:
    sync(Config())
    sources[1].fails = "import"
    with pytest.raises(ValueError, match="bad backup"):
        sync(Config(), rebuild=True, offline=True)
    status = read_status()
    assert status["first"].last_error == "not rebuilt, since another source failed"
    assert status["second"].last_error == "second: bad backup"
    assert db_path().exists()
    assert not db_path().with_name("vpop.db.rebuild").exists()


def test_a_broken_database_fails_every_source(sources: list[Fake]) -> None:
    db_path().parent.mkdir(parents=True)
    db_path().write_text("not a database")
    with pytest.raises(sqlite3.DatabaseError):
        sync(Config(), offline=True)
    assert all(s.last_error for s in read_status().values())


def test_the_lock_refuses_a_second_sync(sources: list[Fake]) -> None:
    with sync_lock(), pytest.raises(SourceError, match="another vpop sync is running"):
        sync(Config())
    assert not sync_status_path().exists()
    sync(Config())  # the lock is released


def test_a_corrupt_status_file_reads_as_none() -> None:
    assert read_status() == {}
    sync_status_path().parent.mkdir(parents=True)
    sync_status_path().write_text("{not json")
    assert read_status() == {}
    sync_status_path().write_text(json.dumps({"a": {"last_error": 3}, "b": []}))
    assert read_status() == {"a": SyncStatus()}
    assert SyncStatus().describe() == "no sync recorded; run `vpop sync`"


def test_sync_without_a_terminal_never_opens_a_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path().parent.mkdir(parents=True)
    config_path().write_text('[android_messages]\ndrive_folder_id = "folder"\n')
    oauth_credentials_path().write_text("{}")

    def no_browser(*_: object, **__: object) -> None:
        raise AssertionError("opened a browser")

    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", no_browser)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["-q", "sync"])
    assert "no Google access yet; run `vpop auth google`" in str(exc.value.code)
    assert "run `vpop auth google`" in (
        read_status()["android_messages"].last_error or ""
    )
    assert capsys.readouterr().out == ""
