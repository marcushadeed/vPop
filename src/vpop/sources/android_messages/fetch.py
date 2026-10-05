"""Download raw Android Messages XML backups from Drive."""

from vpop.data_paths import android_messages_raw_dir
from vpop.sources.data_locations import MESSAGES_BACKUP_FOLDER_ID
from vpop.sources.google_drive import (
    download_file_by_name,
    get_drive_service,
    list_folder_files,
)


def download_raw_xml() -> None:
    """Download the raw XML files from the drive backup"""
    service = get_drive_service()
    folder_id = MESSAGES_BACKUP_FOLDER_ID
    files = list_folder_files(service, folder_id)

    for i, file in enumerate(files):
        # Skip if the file is already in the raw directory
        if (android_messages_raw_dir() / file["name"]).exists():
            print(
                f"({i + 1}/{len(files)}) Skipped {file['name']} (already in raw directory)"
            )
            continue

        # Download the file
        if file["name"].endswith(".xml"):
            file_downloaded = download_file_by_name(
                service, folder_id, file["name"], android_messages_raw_dir()
            )

            success_message = "Downloaded" if file_downloaded else "Failed to download"
            print(f"({i + 1}/{len(files)}) {success_message} {file['name']} from Drive")
        else:
            print(f"({i + 1}/{len(files)}) Skipped {file['name']} (not an XML file)")
