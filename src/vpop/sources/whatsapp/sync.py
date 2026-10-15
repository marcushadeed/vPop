"""WhatsApp backups: download the latest from Drive, and import it into the database."""

import logging
import sqlite3
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

from vpop import db
from vpop.config import Config
from vpop.paths import config_path, whatsapp_key_path
from vpop.sources import SourceError
from vpop.sources.whatsapp.crypt import decrypt, parse_key
from vpop.sources.whatsapp.fetch import FULL_BACKUP, LATEST_SET, increment_number
from vpop.sources.whatsapp.increment import read_increment
from vpop.sources.whatsapp.msgstore import Backup, read_msgstore
from vpop.sources.whatsapp.store import insert_messages, refresh_chats

log = logging.getLogger(__name__)

SOURCE = "whatsapp"


def drive_folder_id(config: Config) -> str:
    """The Drive folder the backups are uploaded to."""
    if config.whatsapp.drive_folder_id:
        return config.whatsapp.drive_folder_id
    raise SourceError(
        f"no Drive folder for the WhatsApp backups. Set drive_folder_id under [whatsapp] "
        f"in {config_path()}."
    )


def backup_key() -> bytes:
    """The backup key from its file, warning if others can read it."""
    path = whatsapp_key_path()
    if not path.exists():
        raise SourceError(
            f"no WhatsApp backup key. Put its 64 hex digits in {path} and make it readable "
            f"only by you: chmod 600 {path}"
        )
    if path.stat().st_mode & 0o077:
        log.warning("%s can be read by other users; run: chmod 600 %s", path, path)
    try:
        return parse_key(path.read_text())
    except ValueError as exc:
        raise SourceError(f"{path}: {exc}") from exc


def decrypt_full(path: Path, key: bytes, tmp: Path) -> Backup:
    """Read a full backup, decrypted into `tmp` for as long as it takes."""
    decrypted = tmp / "msgstore.db"
    decrypt(path, key, decrypted)
    try:
        return read_msgstore(decrypted)
    finally:
        decrypted.unlink(missing_ok=True)


def decrypt_increment(
    path: Path, key: bytes, tmp: Path, lid_to_phone: dict[str, str]
) -> Backup | None:
    """
    Read an increment, decrypted into `tmp` for as long as it takes. None, with a warning,
    for one in a format WhatsApp has changed since this was written.
    """
    archive = tmp / "increment.zip"
    decrypt(path, key, archive)
    try:
        return read_increment(archive, lid_to_phone)
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        log.warning(
            "skipped %s: not in a format vPop can read (%s); its messages will come in "
            "with WhatsApp's next full backup",
            path.name,
            exc,
        )
        return None
    finally:
        archive.unlink(missing_ok=True)


def import_latest(conn: sqlite3.Connection, raw_dir: Path) -> tuple[int, int]:
    """
    Import the latest backup and its increments, whichever weren't imported yet. The full
    backup is always read, since the increments' senders are resolved with its privacy-id
    map. An increment WhatsApp has changed the format of is warned about and skipped; its
    messages arrive with the next full backup. Returns (files imported, new messages).
    """
    full = raw_dir / FULL_BACKUP
    pending = sorted(
        (
            path
            for path in raw_dir.glob("*.crypt15")
            if LATEST_SET.fullmatch(path.name)
            and not db.already_imported(conn, SOURCE, path)
        ),
        key=lambda path: increment_number(path.name),
    )
    if not pending:
        return 0, 0
    if not full.exists():
        raise SourceError(
            f"no {FULL_BACKUP} in {raw_dir}; run `vpop sync` online first"
        )
    key = backup_key()
    imported = added = 0
    with tempfile.TemporaryDirectory(dir=raw_dir, prefix=".decrypted-") as tmp:
        backup = decrypt_full(full, key, Path(tmp))
        lid_to_phone, subjects = backup.lid_to_phone, dict(backup.subjects)
        for path in pending:
            if path == full:
                messages = backup.messages
            else:
                increment = decrypt_increment(path, key, Path(tmp), lid_to_phone)
                if increment is None:
                    continue
                lid_to_phone = increment.lid_to_phone
                subjects.update(increment.subjects)
                messages = increment.messages
            new = insert_messages(conn, messages)
            db.record_import(conn, SOURCE, path, new)
            log.info("imported %s: %d new WhatsApp messages", path.name, new)
            imported += 1
            added += new
    refresh_chats(conn, subjects)
    return imported, added


def download(config: Config, raw_dir: Path, google_scopes: Sequence[str]) -> None:
    """
    Fetch the latest backup set from Drive into `raw_dir`. `google_scopes` are the scopes
    every enabled source needs, so one consent prompt covers them all.
    """
    # Imported here so the Google client libraries load only when they're needed.
    # pylint: disable=import-outside-toplevel
    from vpop.sources.google.drive import get_drive_service
    from vpop.sources.whatsapp.fetch import download_latest

    folder_id = drive_folder_id(config)
    service = get_drive_service(google_scopes)
    names = download_latest(service, folder_id, raw_dir)
    log.info("downloaded %d new WhatsApp backup file(s)", len(names))
