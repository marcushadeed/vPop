"""Tests for the live calendar tools, with a fake Calendar API and a fixed clock."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from vpop.assistant.session import build_toolbox
from vpop.assistant.toolbox import Toolbox
from vpop.config import parse_config
from vpop.paths import db_path, oauth_token_path
from vpop.sources.base import SourceUnavailable
from vpop.sources.google import CALENDAR_READONLY, DRIVE_READONLY
from vpop.sources.google.auth import GoogleAuthRequired
from vpop.sources.google_calendar.client import CalendarClient, http_error_message
from vpop.sources.google_calendar.source import GoogleCalendar
from vpop.sources.google_calendar.tools import CalendarTools, plain_text
from vpop.sources.registry import google_scopes

# The tests run in America/New_York (see conftest); this is a Monday afternoon.
NOW = datetime(2026, 10, 5, 15, 30).astimezone()
ME = {"email": "me@example.com", "self": True, "responseStatus": "accepted"}


def http_error(status: int, message: str = "") -> HttpError:
    content = json.dumps({"error": {"message": message}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


class Request:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeService:
    """
    Stands in for the Calendar API client: `calendars` is the calendar list and `events`
    each calendar's events in the order the API returns them. `page` sets the page size.
    """

    def __init__(
        self,
        calendars: list[dict[str, Any]],
        events: dict[str, list[dict[str, Any]]],
        page: int = 100,
    ) -> None:
        self.calendar_items = calendars
        self.event_items = events
        self.page = page
        self.list_calls: list[dict[str, Any]] = []
        self.calendar_list_calls = 0
        self.failure: BaseException | None = None

    def calendarList(self) -> "FakeService":
        return self

    def events(self) -> "FakeService":
        return self

    def list(self, **kwargs: Any) -> Request:
        if self.failure:
            return Request(self.failure)
        if "calendarId" not in kwargs:
            self.calendar_list_calls += 1
            return Request({"items": self.calendar_items})
        self.list_calls.append(kwargs)
        items = [
            event
            for event in self.event_items.get(kwargs["calendarId"], [])
            if kwargs.get("q", "").casefold() in event.get("summary", "").casefold()
        ]
        offset = int(kwargs.get("pageToken") or 0)
        result: dict[str, Any] = {"items": items[offset : offset + self.page]}
        if offset + self.page < len(items):
            result["nextPageToken"] = str(offset + self.page)
        return Request(result)

    def get(self, calendarId: str, eventId: str) -> Request:
        for event in self.event_items.get(calendarId, []):
            if event["id"] == eventId:
                return Request(event)
        return Request(http_error(404, "Not Found"))


def timed(event_id: str, start: str, end: str, summary: str, **extra: Any) -> dict:
    return {
        "id": event_id,
        "status": "confirmed",
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    } | extra


def all_day(event_id: str, start: str, end: str, summary: str) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"date": start},
        "end": {"date": end},
    }


CALENDARS = [
    {"id": "work@group.calendar.google.com", "summary": "Work"},
    {"id": "me@example.com", "summary": "me@example.com", "primary": True},
    {"id": "en.usa#holiday@group.v.calendar.google.com", "summary": "Holidays in US"},
]
EVENTS = {
    "me@example.com": [
        all_day("trip", "2026-10-09", "2026-10-12", "Trip to Denver"),
        timed(
            "dentist",
            "2026-10-07T15:00:00+02:00",  # 09:00 in New York
            "2026-10-07T16:00:00+02:00",
            "Dentist",
            location="12 Main St\nSuite 4",
        ),
        timed("gone", "2026-10-08T10:00:00-04:00", "2026-10-08T11:00:00-04:00", "x")
        | {"status": "cancelled"},
    ],
    "work@group.calendar.google.com": [
        timed(
            "standup_20261006T130000Z",
            "2026-10-06T09:00:00-04:00",
            "2026-10-06T09:15:00-04:00",
            "Standup",
            recurringEventId="standup",
            attendees=[ME | {"responseStatus": "declined"}],
        ),
        timed(
            "review",
            "2026-10-07T09:30:00-04:00",
            "2026-10-07T10:30:00-04:00",
            "Design review",
            organizer={"email": "sam@example.com", "displayName": "Sam Rivera"},
            attendees=[
                ME,
                {
                    "email": "sam@example.com",
                    "displayName": "Sam Rivera",
                    "responseStatus": "accepted",
                },
                {"email": "alex@example.com", "responseStatus": "needsAction"},
            ],
            hangoutLink="https://meet.google.com/abc-defg-hij",
            description="<p>Agenda:</p><ul><li>Q4 &amp; launch</li></ul>",
        ),
    ],
    "en.usa#holiday@group.v.calendar.google.com": [
        all_day("columbus", "2026-10-12", "2026-10-13", "Columbus Day"),
    ],
}


@pytest.fixture
def service() -> FakeService:
    return FakeService(CALENDARS, EVENTS)


def calendar_tools(service: FakeService, exclude: tuple[str, ...] = ()) -> Toolbox:
    client = CalendarClient(exclude, service_factory=lambda: service)
    return Toolbox([CalendarTools(client, now=lambda: NOW)])


def test_schedule_search_covers_the_next_30_days(service: FakeService) -> None:
    result = calendar_tools(service).call("search_events", {})
    call = service.list_calls[0]
    assert call["singleEvents"] is True
    assert call["orderBy"] == "startTime"
    assert call["timeMin"] == "2026-10-05T00:00:00-04:00"
    assert call["timeMax"] == "2026-11-04T00:00:00-05:00"
    assert "q" not in call
    assert result.splitlines() == [
        "when | calendar | title | where | event_id",
        "2026-10-06 09:00–09:15 | Work | Standup (declined) |  | standup_20261006T130000Z",
        "2026-10-07 09:00–10:00 | me@example.com | Dentist | 12 Main St ⏎ Suite 4 | dentist",
        "2026-10-07 09:30–10:30 | Work | Design review |  | review",
        "2026-10-09 → 2026-10-11 (all day) | me@example.com | Trip to Denver |  | trip",
        "2026-10-12 (all day) | Holidays in US | Columbus Day |  | columbus",
    ]


def test_text_search_reaches_a_year_either_way(service: FakeService) -> None:
    calendar_tools(service).call("search_events", {"text": "dentist"})
    call = service.list_calls[0]
    assert call["q"] == "dentist"
    assert call["timeMin"] == "2025-10-05T00:00:00-04:00"
    assert call["timeMax"] == "2027-10-05T00:00:00-04:00"


@pytest.mark.parametrize(
    ("arguments", "time_min", "time_max"),
    [
        (
            {"start": "2026-10"},
            "2026-10-01T00:00:00-04:00",
            "2026-10-31T00:00:00-04:00",
        ),
        (
            {"start": "2026-10", "end": "2026-10"},
            "2026-10-01T00:00:00-04:00",
            "2026-11-01T00:00:00-04:00",
        ),
        (
            {"start": "2026-10-07 09:00", "end": "2026-10-07"},
            "2026-10-07T09:00:00-04:00",
            "2026-10-08T00:00:00-04:00",
        ),
        (
            {"end": "2026-10-07"},
            "2026-09-08T00:00:00-04:00",
            "2026-10-08T00:00:00-04:00",
        ),
    ],
)
def test_dates_bound_the_search(
    service: FakeService, arguments: dict[str, str], time_min: str, time_max: str
) -> None:
    calendar_tools(service).call("search_events", arguments)
    assert service.list_calls[0]["timeMin"] == time_min
    assert service.list_calls[0]["timeMax"] == time_max


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"start": "next week"}, 'error: start must look like "2026-08"'),
        (
            {"start": "2026-10-08", "end": "2026-10-07"},
            "error: start must be before end",
        ),
        ({"calendar": "gym"}, "error: no calendar matches 'gym'; the calendars are: "),
    ],
)
def test_bad_searches_are_errors(
    service: FakeService, arguments: dict[str, str], error: str
) -> None:
    assert calendar_tools(service).call("search_events", arguments).startswith(error)


def test_calendar_filter_and_exclusions(service: FakeService) -> None:
    result = calendar_tools(service).call("search_events", {"calendar": "work"})
    assert {call["calendarId"] for call in service.list_calls} == {
        "work@group.calendar.google.com"
    }
    assert "Dentist" not in result
    service.list_calls.clear()
    tools = calendar_tools(
        service, exclude=("holidays in us", "WORK@group.calendar.google.com")
    )
    result = tools.call("search_events", {})
    assert [call["calendarId"] for call in service.list_calls] == ["me@example.com"]
    assert "Columbus" not in result


def test_limit_and_pages(service: FakeService) -> None:
    service.page = 1
    result = calendar_tools(service).call("search_events", {"limit": 2})
    lines = result.splitlines()
    assert lines[1].startswith("2026-10-06 09:00") and lines[2].startswith(
        "2026-10-07 09:00"
    )
    assert lines[-1] == "… more events (narrow the dates, add text, or raise limit)"
    assert len(lines) == 4
    # Each calendar was read a page at a time, stopping once it had more than the limit.
    assert len(service.list_calls) == 3 + 2 + 1


def test_no_events(service: FakeService) -> None:
    result = calendar_tools(service).call(
        "search_events", {"text": "yoga", "start": "2026-12-01", "end": "2026-12-01"}
    )
    assert result.startswith("No events from 2026-12-01 00:00 to 2026-12-02 00:00")


def test_api_is_built_on_first_use_and_the_calendar_list_cached(
    service: FakeService,
) -> None:
    built: list[FakeService] = []

    def factory() -> FakeService:
        built.append(service)
        return service

    tools = Toolbox(
        [CalendarTools(CalendarClient(service_factory=factory), lambda: NOW)]
    )
    assert built == []
    tools.call("search_events", {})
    tools.call("search_events", {"text": "x"})
    assert len(built) == 1
    assert service.calendar_list_calls == 1


def test_read_event_in_full(service: FakeService) -> None:
    result = calendar_tools(service).call("read_event", {"event_id": "review"})
    assert result.splitlines() == [
        "Design review",
        "when: 2026-10-07 09:30–10:30",
        "calendar: Work",
        "organizer: Sam Rivera <sam@example.com>",
        (
            "attendees (3): me (accepted), Sam Rivera <sam@example.com> (accepted), "
            "alex@example.com (needsAction)"
        ),
        "video call: https://meet.google.com/abc-defg-hij",
        "description:",
        "Agenda:",
        "Q4 & launch",
    ]


def test_read_event_details(service: FakeService) -> None:
    tools = calendar_tools(service)
    standup = tools.call(
        "read_event", {"event_id": "standup_20261006T130000Z", "calendar": "Work"}
    )
    assert standup.startswith("Standup (declined)\n")
    assert "repeats: yes" in standup
    assert tools.call("read_event", {"event_id": "nope"}) == (
        "No event 'nope'. Check the id and calendar with search_events."
    )


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (
            http_error(
                403, "Google Calendar API has not been used in project 1 before"
            ),
            "error: the Google Calendar API isn't enabled in the user's Google Cloud project",
        ),
        (
            http_error(429, "Too many"),
            "error: Google Calendar's rate limit was reached",
        ),
        (http_error(503, "Backend"), "error: Google Calendar is having trouble (503)"),
        (OSError("Network is unreachable"), "error: couldn't reach Google Calendar"),
    ],
)
def test_api_failures_reach_the_model(
    service: FakeService, failure: BaseException, error: str
) -> None:
    service.failure = failure
    assert calendar_tools(service).call("search_events", {}).startswith(error)


def test_missing_consent_reaches_the_model() -> None:
    def factory() -> FakeService:
        raise GoogleAuthRequired(
            "Google access is missing calendar.readonly; run `vpop auth google`"
        )

    tools = Toolbox(
        [CalendarTools(CalendarClient(service_factory=factory), lambda: NOW)]
    )
    assert tools.call("search_events", {}) == (
        "error: no access to Google Calendar: Google access is missing calendar.readonly; "
        "run `vpop auth google`"
    )


@pytest.mark.parametrize(
    ("status", "start"),
    [
        (401, "Google rejected"),
        (404, "no such calendar"),
        (400, "Google Calendar refused"),
    ],
)
def test_http_error_messages(status: int, start: str) -> None:
    assert http_error_message(status, "Bad").startswith(start)


def test_plain_text_from_html() -> None:
    assert plain_text("a<br>b<br/><b>c</b> &lt;3") == "a\nb\nc <3"


ENABLED = parse_config("[google_calendar]\nenabled = true\n")


def grant(*scopes: str) -> None:
    oauth_token_path().parent.mkdir(parents=True, exist_ok=True)
    oauth_token_path().write_text(json.dumps({"scopes": list(scopes)}))


def test_calendar_is_off_by_default() -> None:
    assert not GoogleCalendar(parse_config("")).enabled()
    assert google_scopes(ENABLED) == sorted([CALENDAR_READONLY, DRIVE_READONLY])


def test_calendar_without_consent_is_unavailable() -> None:
    grant(DRIVE_READONLY)
    with pytest.raises(SourceUnavailable, match="run `vpop auth google`"):
        GoogleCalendar(ENABLED).toolsets(db_path())


def test_calendar_only_conversation(caplog: pytest.LogCaptureFixture) -> None:
    grant(CALENDAR_READONLY)
    box = build_toolbox(ENABLED, db_path())  # no messages synced yet
    assert box.names == ["search_events", "read_event"]
    assert box.db is None
    assert box.system_prompt.startswith(
        "You answer questions about the user's own Google Calendar events."
    )
    assert "vpop sync" in caplog.text


def test_messages_and_calendar_together(tmp_path: Path) -> None:
    from helpers import (  # pylint: disable=import-outside-toplevel
        build_db,
        make_message,
    )

    grant(CALENDAR_READONLY, DRIVE_READONLY)
    database = build_db(db_path(), [make_message()])
    box = build_toolbox(ENABLED, database)
    assert box.names == [
        "find_threads",
        "search_messages",
        "read_thread",
        "search_events",
        "read_event",
        "run_sql",
    ]
    assert (
        "text messages (SMS, MMS and RCS from their Android phone) and Google Calendar"
        in (box.system_prompt)
    )
