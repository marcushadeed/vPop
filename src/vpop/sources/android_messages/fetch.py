"""Download the XML backups SMS Backup & Restore uploads to a Google Drive folder."""

import logging
from pathlib import Path

from vpop.sources.google.drive import (
    DriveService,
    download_file,
    is_google_doc,
    list_folder_files,
)

log = logging.getLogger(__name__)


def download_backups(service: DriveService, folder_id: str, raw_dir: Path) -> list[str]:
    """
    Download every `.xml` file in the folder that isn't already in `raw_dir` at the same
    size. Returns the names downloaded.
    """
    files = sorted(list_folder_files(service, folder_id), key=lambda f: f["name"])
    downloaded: list[str] = []
    for i, file in enumerate(files, start=1):
        name = file["name"]
        progress = f"({i}/{len(files)})"
        if not name.endswith(".xml") or is_google_doc(file):
            log.debug("%s skipped %s (not an XML backup)", progress, name)
            continue
        local = raw_dir / name
        if local.exists() and str(local.stat().st_size) == str(file.get("size")):
            log.debug("%s skipped %s (already downloaded)", progress, name)
            continue
        log.info("%s downloading %s", progress, name)
        download_file(service, file["id"], local)
        downloaded.append(name)
    return downloaded
