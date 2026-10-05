"""
The Google Calendar API, read-only, for the calendar tools.

The API client is built on first use, so a question that never touches the calendar never
contacts Google. Credentials are loaded without a browser (a tool can't stop for consent), and
every failure becomes a `ToolError` whose message the model can pass on.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from vpop.assistant.toolbox import ToolError
from vpop.sources.google import CALENDAR_READONLY

log = logging.getLogger(__name__)

# Seconds to wait for Google before giving up, so a dead network can't stall a question.
TIMEOUT_SECONDS = 10
# The most events one `events.list` page can hold.
MAX_PAGE = 2500

# The Calendar API client is built at runtime from a discovery document, so it has no types.
CalendarService = Any


@dataclass(frozen=True)
class Calendar:
    """One calendar in the user's calendar list."""

    id: str
    name: str
    primary: bool


def build_service() -> CalendarService:
    """The Calendar v3 API client, with the cached credentials and a timeout."""
    # Imported here so the Google client libraries load only when the calendar is used.
    # pylint: disable=import-outside-toplevel
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    from vpop.sources.google.auth import get_credentials

    creds = get_credentials([CALENDAR_READONLY], interactive=False)
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=TIMEOUT_SECONDS))
    return build("calendar", "v3", http=http, cache_discovery=False)


def http_error_message(status: int, reason: str) -> str:
    """What to tell the model about an error status from the Calendar API."""
    lowered = reason.lower()
    if status == 401:
        return "Google rejected vPop's saved access; the user should run `vpop auth google`"
    if status == 403 and ("not been used" in lowered or "disabled" in lowered):
        return (
            "the Google Calendar API isn't enabled in the user's Google Cloud project; "
            f"they should enable it in Cloud Console ({reason})"
        )
    if status == 429 or (status == 403 and "limit" in lowered):
        return "Google Calendar's rate limit was reached; try again in a moment"
    if status == 404:
        return "no such calendar or event"
    if status >= 500:
        return f"Google Calendar is having trouble ({status}); try again later"
    return f"Google Calendar refused the request ({status}): {reason}"


@contextmanager
def api_errors() -> Iterator[None]:
    """Turn auth, HTTP and network failures into a `ToolError` with a readable message."""
    # pylint: disable=import-outside-toplevel
    from google.auth.exceptions import TransportError
    from googleapiclient.errors import HttpError
    from httplib2 import HttpLib2Error

    from vpop.sources.google.auth import GoogleSetupError

    try:
        yield
    except GoogleSetupError as exc:
        raise ToolError(f"no access to Google Calendar: {exc}") from exc
    except HttpError as exc:
        log.debug("Calendar API error", exc_info=True)
        raise ToolError(http_error_message(exc.resp.status, exc.reason or "")) from exc
    except (TransportError, HttpLib2Error, OSError) as exc:
        raise ToolError(f"couldn't reach Google Calendar: {exc}") from exc


class CalendarClient:
    """
    Read-only calls to the Calendar API. `exclude` names calendars to leave out, by name or
    id; `service_factory` builds the API client (tests pass a fake).
    """

    def __init__(
        self,
        exclude: Sequence[str] = (),
        service_factory: Callable[[], CalendarService] = build_service,
    ) -> None:
        self.exclude = {name.casefold() for name in exclude}
        self.service_factory = service_factory
        self._service: CalendarService | None = None
        self._calendars: list[Calendar] | None = None

    @property
    def service(self) -> CalendarService:
        """The API client, built on first use."""
        if self._service is None:
            with api_errors():
                self._service = self.service_factory()
        return self._service

    def calendars(self) -> list[Calendar]:
        """Every calendar in the user's list, primary first, minus the excluded ones."""
        if self._calendars is None:
            items: list[dict[str, Any]] = []
            page_token: str | None = None
            with api_errors():
                while True:
                    response = (
                        self.service.calendarList()
                        .list(pageToken=page_token, maxResults=250)
                        .execute()
                    )
                    items += response.get("items", [])
                    page_token = response.get("nextPageToken")
                    if not page_token:
                        break
            calendars = [
                Calendar(
                    id=item["id"],
                    name=item.get("summaryOverride")
                    or item.get("summary")
                    or item["id"],
                    primary=bool(item.get("primary")),
                )
                for item in items
            ]
            self._calendars = sorted(
                (
                    cal
                    for cal in calendars
                    if cal.id.casefold() not in self.exclude
                    and cal.name.casefold() not in self.exclude
                ),
                key=lambda cal: not cal.primary,
            )
        return self._calendars

    def events(
        self,
        calendar_id: str,
        *,
        time_min: str,
        time_max: str,
        text: str = "",
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        Up to `limit` events overlapping [`time_min`, `time_max`) (RFC 3339), soonest first,
        with recurring events expanded into their occurrences, and whether there were more.
        """
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "orderBy": "startTime",
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": min(limit + 1, MAX_PAGE),
        }
        if text:
            params["q"] = text
        events: list[dict[str, Any]] = []
        with api_errors():
            while len(events) <= limit:
                response = self.service.events().list(**params).execute()
                events += response.get("items", [])
                params["pageToken"] = response.get("nextPageToken")
                if not params["pageToken"]:
                    break
        return events[:limit], len(events) > limit or bool(params.get("pageToken"))

    def event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        """One event by id, or None if the calendar has no such event."""
        # pylint: disable=import-outside-toplevel
        from googleapiclient.errors import HttpError

        with api_errors():
            try:
                result: dict[str, Any] = (
                    self.service.events()
                    .get(calendarId=calendar_id, eventId=event_id)
                    .execute()
                )
            except HttpError as exc:
                # 400 is what Google answers for an id that can't be an event id.
                if exc.resp.status in (400, 404):
                    return None
                raise
        return result
