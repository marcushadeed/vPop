"""Tests for the Drive client and the backup downloader, with a fake Drive service."""

import stat
from pathlib import Path
from typing import Any

import pytest

from vpop.paths import oauth_token_path
from vpop.sources import google_drive
from vpop.sources.android_messages.fetch import download_backups
from vpop.sources.google_drive import (
    DriveSetupError,
    download_file,
    get_credentials,
    list_folder_files,
)


class Request:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        return self.result


class FakeFiles:
    """`service.files()`: pages of listings, and file bytes by id."""

    def __init__(self, pages: list[dict[str, Any]], contents: dict[str, bytes]) -> None:
        self.pages = pages
        self.contents = contents
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Request:
        self.list_calls.append(kwargs)
        return Request(self.pages[len(self.list_calls) - 1])

    def get_media(self, fileId: str) -> str:
        return fileId


class FakeService:
    def __init__(self, pages: list[dict[str, Any]], contents: dict[str, bytes]) -> None:
        self.fake_files = FakeFiles(pages, contents)

    def files(self) -> FakeFiles:
        return self.fake_files


class FakeDownloader:
    """Replaces `MediaIoBaseDownload`: writes the file's bytes, or fails for `fail`."""

    def __init__(self, fh: Any, request: str) -> None:
        self.fh = fh
        self.request = request

    def next_chunk(self) -> tuple[None, bool]:
        if self.request == "fail":
            self.fh.write(b"half")
            raise ConnectionError("network dropped")
        self.fh.write(SERVICE_CONTENTS[self.request])
        return None, True


SERVICE_CONTENTS = {"id-a": b"<smses/>", "id-b": b"<smses></smses>"}


@pytest.fixture(autouse=True)
def fake_downloader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_drive, "MediaIoBaseDownload", FakeDownloader)


def file(
    name: str, file_id: str, size: int | None = None, **extra: str
) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": file_id, "name": name, **extra}
    if size is not None:
        entry["size"] = str(size)
    return entry


def test_list_follows_pages() -> None:
    service = FakeService(
        [
            {"files": [file("a.xml", "id-a")], "nextPageToken": "t"},
            {"files": [file("b.xml", "id-b")]},
        ],
        SERVICE_CONTENTS,
    )
    names = [f["name"] for f in list_folder_files(service, "folder'1")]
    assert names == ["a.xml", "b.xml"]
    calls = service.fake_files.list_calls
    assert calls[1]["pageToken"] == "t"
    assert "'folder\\'1' in parents" in calls[0]["q"]


def test_interrupted_download_leaves_no_file(tmp_path: Path) -> None:
    dest = tmp_path / "a.xml"
    with pytest.raises(ConnectionError):
        download_file(FakeService([], {}), "fail", dest)
    assert list(tmp_path.iterdir()) == []


def test_download_backups_skips_what_it_has(tmp_path: Path) -> None:
    (tmp_path / "a.xml").write_bytes(SERVICE_CONTENTS["id-a"])  # complete copy
    (tmp_path / "b.xml").write_bytes(b"<sm")  # truncated by an older version
    service = FakeService(
        [
            {
                "files": [
                    file("a.xml", "id-a", len(SERVICE_CONTENTS["id-a"])),
                    file("b.xml", "id-b", len(SERVICE_CONTENTS["id-b"])),
                    file("notes.txt", "id-n", 3),
                    file(
                        "doc.xml",
                        "id-d",
                        mimeType="application/vnd.google-apps.document",
                    ),
                ]
            }
        ],
        SERVICE_CONTENTS,
    )
    assert download_backups(service, "folder", tmp_path) == ["b.xml"]
    assert (tmp_path / "b.xml").read_bytes() == SERVICE_CONTENTS["id-b"]


def test_missing_oauth_client_explains_setup() -> None:
    with pytest.raises(DriveSetupError, match=r"oauth-client-credentials\.json"):
        get_credentials()


def test_new_token_is_private(monkeypatch: pytest.MonkeyPatch) -> None:
    client = oauth_token_path().parent / "oauth-client-credentials.json"
    client.parent.mkdir(parents=True)
    client.write_text("{}")

    class Creds:
        valid = True

        def to_json(self) -> str:
            return '{"token": "t"}'

    class Flow:
        def run_local_server(self, port: int) -> Creds:
            return Creds()

    monkeypatch.setattr(
        google_drive.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *_: Flow(),
    )
    get_credentials()
    assert stat.S_IMODE(oauth_token_path().stat().st_mode) == 0o600
