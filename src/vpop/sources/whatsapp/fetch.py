"""Download the latest WhatsApp backup and its increments from a Google Drive folder."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from vpop.sources import SourceError
from vpop.sources.google.drive import (
    DriveService,
    download_file,
    is_google_doc,
    list_folder_files,
)

log = logging.getLogger(__name__)

FULL_BACKUP = "msgstore.db.crypt15"
# The latest set: the full backup and the increments since. WhatsApp renames an older set
# with its date (`msgstore-2026-10-12.1.db.crypt15`) when it makes a new full backup.
LATEST_SET = re.compile(r"msgstore(?:-increment-(\d+))?\.db\.crypt15")


def increment_number(name: str) -> int:
    """0 for the full backup, else the increment's number: the order to import them in."""
    match = LATEST_SET.fullmatch(name)
    return int(match.group(1) or 0) if match else -1


def is_current(local: Path, file: dict[str, Any]) -> bool:
    """
    Whether `local` already holds this Drive file. Names repeat from one set to the next,
    so a file is fetched again when Drive's copy is newer than the download, or its size
    differs.
    """
    if not local.exists() or str(local.stat().st_size) != str(file.get("size")):
        return False
    modified = datetime.fromisoformat(str(file.get("modifiedTime", "")))
    return local.stat().st_mtime >= modified.timestamp()


def download_latest(service: DriveService, folder_id: str, raw_dir: Path) -> list[str]:
    """
    Download the latest set's files that aren't already in `raw_dir`, then delete the files
    of older sets from `raw_dir`. Returns the names downloaded.
    """
    latest = sorted(
        (
            file
            for file in list_folder_files(service, folder_id)
            if LATEST_SET.fullmatch(file["name"]) and not is_google_doc(file)
        ),
        key=lambda f: increment_number(f["name"]),
    )
    if not any(file["name"] == FULL_BACKUP for file in latest):
        raise SourceError(
            f"no {FULL_BACKUP} in the Drive folder {folder_id}. vPop reads WhatsApp's "
            "end-to-end encrypted backups (Settings → Chats → Chat backup → End-to-end "
            "encrypted backup, with a 64-digit key)."
        )
    downloaded = [
        file for file in latest if not is_current(raw_dir / file["name"], file)
    ]
    for file in downloaded:
        log.info("downloading %s", file["name"])
        download_file(service, file["id"], raw_dir / file["name"])
    keep = {file["name"] for file in latest}
    for old in raw_dir.glob("*.crypt15"):
        if old.name not in keep:
            log.debug("deleted %s (from an older backup)", old.name)
            old.unlink()
    return [file["name"] for file in downloaded]
