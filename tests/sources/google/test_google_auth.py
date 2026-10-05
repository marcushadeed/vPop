"""Tests for the shared Google OAuth: scope checks, refresh, and consent, with a fake flow."""

import json
import stat
from typing import Any

import pytest

from vpop import cli
from vpop.config import Config
from vpop.paths import oauth_credentials_path, oauth_token_path
from vpop.sources.google import CALENDAR_READONLY, DRIVE_READONLY, auth
from vpop.sources.google.auth import (
    GoogleAuthRequired,
    GoogleSetupError,
    get_credentials,
    granted_scopes,
    status_line,
)
from vpop.sources.registry import google_scopes

BOTH = sorted([CALENDAR_READONLY, DRIVE_READONLY])


def write_token(scopes: list[str], **fields: Any) -> None:
    oauth_token_path().parent.mkdir(parents=True, exist_ok=True)
    oauth_token_path().write_text(
        json.dumps(
            {
                "token": "access",
                "refresh_token": "refresh",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": scopes,
                "expiry": "2099-01-01T00:00:00Z",
            }
            | fields
        )
    )


class FakeCreds:
    def __init__(self, granted: list[str]) -> None:
        self.granted_scopes = granted

    def to_json(self) -> str:
        return json.dumps({"token": "new", "scopes": "what was asked for"})


class FakeFlow:
    """Stands in for `InstalledAppFlow`; `granted` is what the user agrees to."""

    requested: list[list[str]]
    granted: list[str] | None = None

    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes

    def run_local_server(self, port: int) -> FakeCreds:
        FakeFlow.requested.append(self.scopes)
        return FakeCreds(self.granted if self.granted is not None else self.scopes)


@pytest.fixture(autouse=True)
def flow(monkeypatch: pytest.MonkeyPatch) -> type[FakeFlow]:
    FakeFlow.requested = []
    FakeFlow.granted = None
    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        lambda path, scopes: FakeFlow(scopes),
    )
    oauth_credentials_path().parent.mkdir(parents=True, exist_ok=True)
    oauth_credentials_path().write_text("{}")
    return FakeFlow


def test_missing_oauth_client_explains_setup() -> None:
    oauth_credentials_path().unlink()
    with pytest.raises(GoogleSetupError, match=r"oauth-client-credentials\.json"):
        get_credentials([DRIVE_READONLY])


def test_token_with_every_scope_is_used_as_is(flow: type[FakeFlow]) -> None:
    write_token(BOTH)
    creds = get_credentials([CALENDAR_READONLY], interactive=False)
    assert creds.token == "access"
    assert flow.requested == []


def test_missing_scope_asks_for_all_of_them_and_saves_what_was_granted(
    flow: type[FakeFlow],
) -> None:
    write_token([DRIVE_READONLY])
    get_credentials([CALENDAR_READONLY])
    # Asking for Calendar keeps Drive.
    assert flow.requested == [BOTH]
    assert granted_scopes() == set(BOTH)
    assert stat.S_IMODE(oauth_token_path().stat().st_mode) == 0o600


def test_without_a_terminal_consent_is_never_asked(flow: type[FakeFlow]) -> None:
    with pytest.raises(
        GoogleAuthRequired, match="no Google access yet; run `vpop auth"
    ):
        get_credentials([CALENDAR_READONLY], interactive=False)
    write_token([DRIVE_READONLY])
    with pytest.raises(GoogleAuthRequired, match=r"missing calendar\.readonly"):
        get_credentials([CALENDAR_READONLY], interactive=False)
    assert flow.requested == []


def test_refused_refresh_needs_consent_again(
    flow: type[FakeFlow], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_token([DRIVE_READONLY], expiry="2000-01-01T00:00:00Z")

    def refuse(self: object, request: object) -> None:
        raise auth.RefreshError("invalid_grant")

    monkeypatch.setattr(auth.Credentials, "refresh", refuse)
    with pytest.raises(GoogleAuthRequired, match="stopped working"):
        get_credentials([DRIVE_READONLY], interactive=False)
    get_credentials([DRIVE_READONLY])
    assert flow.requested == [[DRIVE_READONLY]]


def test_refreshed_token_is_saved(
    flow: type[FakeFlow], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_token([DRIVE_READONLY], expiry="2000-01-01T00:00:00Z")

    def refresh(self: Any, request: object) -> None:
        self.token = "refreshed"
        self.expiry = None

    monkeypatch.setattr(auth.Credentials, "refresh", refresh)
    assert get_credentials([DRIVE_READONLY], interactive=False).token == "refreshed"
    assert json.loads(oauth_token_path().read_text())["token"] == "refreshed"
    assert granted_scopes() == {DRIVE_READONLY}
    assert flow.requested == []


def test_unticked_scope_is_an_error(flow: type[FakeFlow]) -> None:
    flow.granted = [DRIVE_READONLY]
    with pytest.raises(GoogleSetupError, match=r"didn't grant calendar\.readonly"):
        get_credentials(BOTH)


def test_status_line() -> None:
    assert status_line([DRIVE_READONLY]) == (
        "google: no access yet; missing drive.readonly, run `vpop auth google`"
    )
    write_token(BOTH)
    assert status_line([DRIVE_READONLY]) == (
        "google: calendar.readonly, drive.readonly granted"
    )


def test_scopes_of_the_enabled_sources() -> None:
    assert google_scopes(Config()) == [DRIVE_READONLY]


def test_auth_google_asks_for_the_enabled_sources(
    flow: type[FakeFlow], capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["auth", "google"])
    assert flow.requested == [[DRIVE_READONLY]]
    assert "Google access granted: drive.readonly" in capsys.readouterr().out
