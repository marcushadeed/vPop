"""`vpop sync` for Android Messages: download new backups from Drive and import them."""

import logging
import os
import sqlite3
from pathlib import Path

from vpop import db
from vpop.config import Config, set_setting
from vpop.fsutil import read_env_file
from vpop.paths import (
    android_messages_raw_dir,
    config_path,
    db_path,
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


def download(config: Config, raw_dir: Path) -> None:
    """Fetch new backups from Drive into `raw_dir`."""
    # Imported here so the Google client libraries load only when they're needed.
    # pylint: disable=import-outside-toplevel
    from vpop.sources.android_messages.fetch import download_backups
    from vpop.sources.google_drive import get_drive_service

    folder_id = drive_folder_id(config)
    names = download_backups(get_drive_service(), folder_id, raw_dir)
    log.info("downloaded %d new backup file(s)", len(names))


def sync(config: Config, *, rebuild: bool = False, offline: bool = False) -> None:
    """
    Download new backups (unless `offline`), then import the ones not imported yet. With
    `rebuild`, the database is recreated from every downloaded backup; it's built beside the
    old one and swapped in only once complete.
    """
    raw_dir = android_messages_raw_dir()
    if not offline:
        download(config, raw_dir)
    target = db_path()
    path = target.with_name(target.name + ".rebuild") if rebuild else target
    if rebuild:
        path.unlink(missing_ok=True)
    try:
        conn = db.connect(path)
        try:
            files, added = import_backups(conn, raw_dir)
        finally:
            conn.close()
        if rebuild:
            os.replace(path, target)
    except BaseException:
        if rebuild:
            path.unlink(missing_ok=True)
        raise
    log.info(
        "%s %s: %d file(s) imported, %d new messages",
        "rebuilt" if rebuild else "synced",
        target,
        files,
        added,
    )
