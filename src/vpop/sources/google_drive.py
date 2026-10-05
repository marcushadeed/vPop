"""Read files from Google Drive with the user's OAuth consent (read-only scope)."""

import os
from pathlib import Path
from typing import Any, cast

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from vpop.fsutil import write_private
from vpop.paths import oauth_credentials_path, oauth_token_path
from vpop.sources import SourceError

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# The Drive API client is built at runtime from a discovery document, so it has no types.
DriveService = Any


class DriveSetupError(SourceError):
    """Drive access isn't set up. The message says what to do."""


def missing_client_message(path: Path) -> str:
    """What to do when the OAuth client file is missing."""
    return (
        f"no Google OAuth client at {path}.\n"
        "Create one in Google Cloud Console (APIs & Services → Credentials → Create "
        "credentials → OAuth client ID → Desktop app), enable the Google Drive API for the "
        f"project, download the client's JSON, and save it as {path}."
    )


def get_credentials() -> Credentials:
    """Load cached OAuth credentials, refreshing or requesting consent as needed."""
    token_path = oauth_token_path()
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Refresh token was revoked or expired; fall back to consent.
            creds = None

    if not creds or not creds.valid:
        client_path = oauth_credentials_path()
        if not client_path.exists():
            raise DriveSetupError(missing_client_message(client_path))
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
        creds = cast(Credentials, flow.run_local_server(port=0))

    # The refresh token reads the whole Drive, so only the owner may read it.
    write_private(token_path, creds.to_json())
    return creds


def get_drive_service() -> DriveService:
    """The Drive v3 API client, authorized with the user's credentials."""
    return build("drive", "v3", credentials=get_credentials())


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
