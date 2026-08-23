"""Fetch Android Messages SMS Backup & Restore XML from Google Drive."""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from paths import OAUTH_CREDENTIALS_PATH, OAUTH_TOKEN_PATH

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _get_credentials() -> Credentials:
    """Load cached OAuth credentials, refreshing or requesting consent as needed."""
    creds = None
    if OAUTH_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(OAUTH_TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(OAUTH_CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        OAUTH_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        OAUTH_TOKEN_PATH.write_text(creds.to_json())

    return creds


def get_drive_service():
    """Access the Drive API service with OAuth credentials."""

    return build("drive", "v3", credentials=_get_credentials())


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


def download_file_by_name(service, folder_id: str, name: str, dest_dir: Path) -> bool:
    """
    Download a file by exact name from a Drive folder into local storage.
    Returns True if the file was downloaded successfully, False otherwise.
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
        return False
    if len(matches) > 1:
        return False

    file_meta = matches[0]
    if file_meta.get("mimeType", "").startswith("application/vnd.google-apps."):
        return False

    dest = (dest_dir) / name
    dest.parent.mkdir(parents=True, exist_ok=True)

    request = service.files().get_media(fileId=file_meta["id"])
    with dest.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return True
