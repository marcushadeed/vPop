"""Android Messages backups: download new ones from Drive, and import them into the database."""

import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from vpop import db
from vpop.config import Config, set_setting
from vpop.fsutil import read_env_file
from vpop.paths import (
    config_path,
    legacy_remote_locations_path,
)
from vpop.sources import SourceError
from vpop.sources.android_messages.parse import messages_from_xml
from vpop.sources.android_messages.store import insert_messages

log = logging.getLogger(__name__)

SOURCE = "android_messages"
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


def import_backups(conn: sqlite3.Connection, raw_dir: Path) -> tuple[int, int]:
    """
    Import every backup in `raw_dir` not imported before, oldest name first (backup names
    embed their date). Returns (files imported, new messages).
    """
    files = sorted(raw_dir.glob("*.xml")) if raw_dir.exists() else []
    imported = added = 0
    for file in files:
        if db.already_imported(conn, SOURCE, file):
            continue
        new = insert_messages(conn, messages_from_xml(file))
        db.record_import(conn, SOURCE, file, new)
        log.info("imported %s: %d new messages", file.name, new)
        imported += 1
        added += new
    return imported, added


def download(config: Config, raw_dir: Path, google_scopes: Sequence[str]) -> None:
    """
    Fetch new backups from Drive into `raw_dir`. `google_scopes` are the scopes every
    enabled source needs, so one consent prompt covers them all.
    """
    # Imported here so the Google client libraries load only when they're needed.
    # pylint: disable=import-outside-toplevel
    from vpop.sources.android_messages.fetch import download_backups
    from vpop.sources.google.drive import get_drive_service

    folder_id = drive_folder_id(config)
    service = get_drive_service(google_scopes)
    names = download_backups(service, folder_id, raw_dir)
    log.info("downloaded %d new backup file(s)", len(names))
