"""
Google API access shared by the sources that read Google: one OAuth client, one cached token
holding every scope the enabled sources need (see `auth`).

The scope names live here, not in `auth`, so naming them doesn't load the Google libraries.
"""

DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"


def scope_name(scope: str) -> str:
    """The short name of a scope URL, e.g. `drive.readonly`."""
    return scope.rsplit("/", 1)[-1]
