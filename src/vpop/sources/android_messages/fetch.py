"""Download raw Android Messages XML backups from Drive."""

import logging

from vpop.data_paths import android_messages_raw_dir
from vpop.sources.data_locations import MESSAGES_BACKUP_FOLDER_ID
from vpop.sources.google_drive import (
    download_file_by_name,
    get_drive_service,
    list_folder_files,
)

log = logging.getLogger(__name__)


def download_raw_xml() -> None:
    """Download the raw XML files from the drive backup"""
    service = get_drive_service()
    folder_id = MESSAGES_BACKUP_FOLDER_ID
    files = list_folder_files(service, folder_id)

    for i, file in enumerate(files):
        progress = f"({i + 1}/{len(files)})"
        # Skip if the file is already in the raw directory
        if (android_messages_raw_dir() / file["name"]).exists():
            log.debug("%s skipped %s (already downloaded)", progress, file["name"])
            continue

        # Download the file
        if file["name"].endswith(".xml"):
            if download_file_by_name(
                service, folder_id, file["name"], android_messages_raw_dir()
            ):
                log.info("%s downloaded %s", progress, file["name"])
            else:
                log.warning("%s couldn't download %s", progress, file["name"])
        else:
            log.debug("%s skipped %s (not an XML file)", progress, file["name"])
