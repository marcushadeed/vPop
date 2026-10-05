"""Read files from Google Drive with the user's OAuth consent (read-only scope)."""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from vpop.sources.google import DRIVE_READONLY
from vpop.sources.google.auth import get_credentials

# The Drive API client is built at runtime from a discovery document, so it has no types.
DriveService = Any


def get_drive_service(scopes: Sequence[str] = (DRIVE_READONLY,)) -> DriveService:
    """
    The Drive v3 API client, authorized with the user's credentials. `scopes` should be
    every scope the enabled sources need, so one consent prompt covers all of them.
    """
    return build("drive", "v3", credentials=get_credentials(scopes))


def escape_query_value(value: str) -> str:
    """Escape a value for a single-quoted string in a Drive search query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_folder_files(service: DriveService, folder_id: str) -> list[dict[str, Any]]:
    """
    List non-trashed files directly inside a Drive folder, as dicts with `id`, `name`,
    `mimeType`, `modifiedTime` and `size` (Google Docs have no size).
    """
    query = f"'{escape_query_value(folder_id)}' in parents and trashed = false"
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return files


def is_google_doc(file: dict[str, Any]) -> bool:
    """Whether a file is a native Google Docs/Sheets/... file, which has no bytes to download."""
    return str(file.get("mimeType", "")).startswith("application/vnd.google-apps.")


def download_file(service: DriveService, file_id: str, dest: Path) -> None:
    """
    Download a file's bytes to `dest`. It's written to `dest.part` and renamed when complete,
    so an interrupted download never leaves a truncated file at `dest`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    request = service.files().get_media(fileId=file_id)
    try:
        with partial.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        os.replace(partial, dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
