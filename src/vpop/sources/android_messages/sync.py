"""`vpop sync` for Android Messages: download new backups from Drive and import them."""

import logging
from pathlib import Path

from vpop.config import Config, set_setting
from vpop.fsutil import read_env_file
from vpop.paths import (
    android_messages_raw_dir,
    config_path,
    legacy_remote_locations_path,
)
from vpop.sources import SourceError
from vpop.sources.android_messages.xml_to_sqlite import xml_to_sqlite

log = logging.getLogger(__name__)

LEGACY_FOLDER_VAR = "MESSAGES_BACKUP_FOLDER_ID"


class SyncError(SourceError):
    """Sync can't run as configured. The message says what to do."""


def drive_folder_id(config: Config) -> str:
    """
    The Drive folder the backups are in. Older versions kept it in
    `remote-data-locations.env`; if that's where it still is, it's copied into the config.
    """
    if config.android_messages.drive_folder_id:
        return config.android_messages.drive_folder_id
    legacy = read_env_file(legacy_remote_locations_path()).get(LEGACY_FOLDER_VAR, "")
    if legacy:
        set_setting(config_path(), "android_messages", "drive_folder_id", legacy)
        log.info(
            "moved the Drive folder id from %s into %s (the old file can be deleted)",
            legacy_remote_locations_path(),
            config_path(),
        )
        return legacy
    raise SyncError(
        f"no Drive folder for the message backups. Set drive_folder_id under "
        f"[android_messages] in {config_path()}."
    )


def download(config: Config, raw_dir: Path) -> None:
    """Fetch new backups from Drive into `raw_dir`."""
    # Imported here so the Google client libraries load only when they're needed.
    # pylint: disable=import-outside-toplevel
    from vpop.sources.android_messages.fetch import download_backups
    from vpop.sources.google_drive import get_drive_service

    folder_id = drive_folder_id(config)
    names = download_backups(get_drive_service(), folder_id, raw_dir)
    log.info("downloaded %d new backup file(s)", len(names))


def sync(config: Config) -> None:
    """Download new backups, then import every downloaded backup."""
    raw_dir = android_messages_raw_dir()
    download(config, raw_dir)
    for file in raw_dir.glob("*.xml"):
        xml_to_sqlite(file)
