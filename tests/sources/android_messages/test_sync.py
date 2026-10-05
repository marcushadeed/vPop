"""Tests for `vpop sync` on Android Messages (no network: backups are written locally)."""

import pytest

from vpop.config import Config, load_config, parse_config
from vpop.paths import config_path
from vpop.sources.android_messages.sync import SyncError, drive_folder_id


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
