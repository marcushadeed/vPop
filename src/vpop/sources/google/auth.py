"""
Google OAuth with the user's consent, for every source that reads a Google API.

There is one OAuth client (a Desktop app the user creates in Cloud Console) and one cached
token. The token records the scopes Google actually granted; when a source needs one it
lacks, consent is asked for every scope at once (those already granted plus the new ones), so
adding a source never drops another's access.

`vpop sync` may open a browser for consent (`interactive=True`). Tools that run while a
question is answered must not, so they ask with `interactive=False` and get a
`GoogleAuthRequired` telling the user to run `vpop auth google` instead.
"""

import json
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, cast

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from vpop.fsutil import write_private
from vpop.paths import oauth_credentials_path, oauth_token_path
from vpop.sources import SourceError
from vpop.sources.google import scope_name

log = logging.getLogger(__name__)


class GoogleSetupError(SourceError):
    """Google access isn't set up. The message says what to do."""


class GoogleAuthRequired(GoogleSetupError):
    """Google needs the user's consent again: a scope is missing or the token stopped working."""


def missing_client_message(path: Path) -> str:
    """What to do when the OAuth client file is missing."""
    return (
        f"no Google OAuth client at {path}.\n"
        "Create one in Google Cloud Console (APIs & Services → Credentials → Create "
        "credentials → OAuth client ID → Desktop app), enable the APIs vPop reads for the "
        "project (Google Drive API for message backups, Google Calendar API for the "
        f"calendar), download the client's JSON, and save it as {path}."
    )


def names(scopes: Iterable[str]) -> str:
    """Scopes by their short names, e.g. `calendar.readonly, drive.readonly`."""
    return ", ".join(sorted(scope_name(scope) for scope in scopes)) or "none"


def scope_set(value: Any) -> set[str]:
    """Scopes as a token stores them: a list, or one space-separated string."""
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, list | tuple | set):
        return {str(scope) for scope in value}
    return set()


def read_token() -> dict[str, Any] | None:
    """The cached token's fields, or None if there's no usable token file."""
    try:
        data = json.loads(oauth_token_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def granted_scopes() -> set[str]:
    """The scopes the cached token holds; none without a token. Doesn't touch the network."""
    data = read_token()
    return scope_set(data.get("scopes")) if data else set()


def consent(scopes: Sequence[str]) -> Credentials:
    """
    Open a browser for the user to grant `scopes`, and return the new credentials. Raises
    `GoogleSetupError` if the OAuth client is missing or a scope wasn't granted (Google lets
    the user untick some).
    """
    client_path = oauth_credentials_path()
    if not client_path.exists():
        raise GoogleSetupError(missing_client_message(client_path))
    # Google may grant fewer scopes than asked for; that's checked below with a clear
    # message, instead of oauthlib's "Scope has changed" warning-as-error.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    log.info("asking Google for access to: %s (opens a browser)", names(scopes))
    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), list(scopes))
    creds = cast(Credentials, flow.run_local_server(port=0))
    granted = scope_set(getattr(creds, "granted_scopes", None)) or set(scopes)
    missing = set(scopes) - granted
    if missing:
        raise GoogleSetupError(
            f"Google didn't grant {names(missing)}. Run `vpop auth google` again and allow "
            "every permission it asks for."
        )
    save(creds, granted)
    return creds


def save(creds: Credentials, scopes: Iterable[str]) -> None:
    """Cache the credentials with the scopes they hold, readable only by the user."""
    data = json.loads(creds.to_json())
    data["scopes"] = sorted(scopes)
    # The refresh token reads the user's data, so only the owner may read it.
    write_private(oauth_token_path(), json.dumps(data))


def get_credentials(scopes: Sequence[str], *, interactive: bool = True) -> Credentials:
    """
    Credentials holding at least `scopes`: the cached token, refreshed if it expired. When
    consent is needed (no token, a scope missing, or a refresh Google refused), it's asked
    for every scope at once if `interactive`, else `GoogleAuthRequired` is raised.
    """
    granted = granted_scopes()
    data = read_token()
    if data and set(scopes) <= granted:
        creds = cast(Credentials, Credentials.from_authorized_user_info(data))
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Revoked, or expired (after 7 days while the consent screen is in testing).
                log.debug("Google refused to refresh the token", exc_info=True)
            else:
                save(creds, granted)
                return creds
        problem = "the saved Google token stopped working (expired or revoked)"
    elif data:
        problem = f"Google access is missing {names(set(scopes) - granted)}"
    else:
        problem = "vPop has no Google access yet"
    if not interactive:
        raise GoogleAuthRequired(f"{problem}; run `vpop auth google`")
    return consent(sorted(granted | set(scopes)))


def authorize(scopes: Sequence[str]) -> set[str]:
    """`vpop auth google`: ask for consent to `scopes` (and those already granted)."""
    consent(sorted(granted_scopes() | set(scopes)))
    return granted_scopes()


def status_line(scopes: Sequence[str]) -> str:
    """A line for `vpop auth status`: the scopes granted, and any the sources still need."""
    granted = granted_scopes()
    if not granted:
        line = "google: no access yet"
    else:
        line = f"google: {names(granted)} granted"
    missing = set(scopes) - granted
    if missing:
        line += f"; missing {names(missing)}, run `vpop auth google`"
    return line
