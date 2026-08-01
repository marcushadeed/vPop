"""Fetch Android Messages SMS Backup & Restore XML from Google Drive."""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from data_gathering.file_management.data_paths import android_messages_raw_dir
from paths import OAUTH_CREDENTIALS_PATH as CREDENTIALS_PATH
from paths import TOKEN_PATH

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service():
    """Build an authenticated Drive API client using secrets/credentials.json."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Missing OAuth client secrets at {CREDENTIALS_PATH}. "
                    "Download credentials.json from Google Cloud Console and place it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_folder_files(service, folder_id: str) -> list[dict]:
    """List non-trashed files directly inside a Drive folder.

    Returns dicts with at least ``id`` and ``name``.
    """
    query = f"'{_escape_drive_query_value(folder_id)}' in parents and trashed = false"
    files: list[dict] = []
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
            break

    return files


def download_file_by_name(
    service,
    folder_id: str,
    name: str,
    *,
    dest_dir: Path | None = None,
) -> Path:
    """Download a file by exact name from a Drive folder into local storage.

    Defaults to ``~/.local/share/vpop/raw/android-messages`` (or XDG equivalent).
    Raises ``FileNotFoundError`` if no match, ``ValueError`` if multiple match.
    """
    query = (
        f"'{_escape_drive_query_value(folder_id)}' in parents "
        f"and name = '{_escape_drive_query_value(name)}' "
        f"and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name, mimeType)",
            pageSize=10,
        )
        .execute()
    )
    matches = response.get("files", [])
    if not matches:
        raise FileNotFoundError(f"No file named {name!r} in Drive folder {folder_id!r}")
    if len(matches) > 1:
        raise ValueError(
            f"Multiple files named {name!r} in Drive folder {folder_id!r}; "
            f"ids={[m['id'] for m in matches]}"
        )

    file_meta = matches[0]
    if file_meta.get("mimeType", "").startswith("application/vnd.google-apps."):
        raise ValueError(
            f"{name!r} is a Google Docs editor file ({file_meta['mimeType']}), "
            "not a binary download. Export it first or pick a regular file."
        )

    dest = (dest_dir or android_messages_raw_dir()) / name
    dest.parent.mkdir(parents=True, exist_ok=True)

    request = service.files().get_media(fileId=file_meta["id"])
    with dest.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return dest
