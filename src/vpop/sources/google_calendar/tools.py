"""
The calendar tools: search events and read one, live from Google Calendar. Results have one
line per occurrence (`when | calendar | title | where | id`), in local time.
"""

import html
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from vpop.assistant.toolbox import (
    LABEL_CHARS,
    Toolset,
    clamp_limit,
    one_line,
    parse_time_range,
)
from vpop.sources.google_calendar.client import Calendar, CalendarClient

DESCRIPTION_CHARS = 2000
# How far a search reaches when it gives no dates: ahead for a schedule, both ways for text.
SCHEDULE_DAYS = 30
TEXT_DAYS = 365

PROMPT = """\
Google Calendar is read live through its API with search_events and read_event; it isn't in \
the SQL database.

How to work with the calendar:
- For anything about the user's schedule (what's on a day, when something is, whether they're \
free), use search_events with `start` and `end` for the days in question. `text` searches \
titles, descriptions, locations and attendees, so "dentist" finds a dentist appointment.
- A recurring event appears once per occurrence. Events marked `(declined)` are ones the user \
said they won't attend.
- Use read_event for an event's description, attendees and video link.
- When you rely on an event, give its date and time and its calendar.
"""


def local(value: str) -> datetime:
    """An RFC 3339 time from the API, in local time."""
    return datetime.fromisoformat(value).astimezone()


def event_dates(event: dict[str, Any]) -> tuple[datetime | date, datetime | date]:
    """An event's start and end: local datetimes, or dates for an all-day event."""
    start, end = event.get("start", {}), event.get("end", {})
    if "dateTime" in start:
        return local(start["dateTime"]), local(end.get("dateTime", start["dateTime"]))
    first = date.fromisoformat(start["date"])
    # An all-day event's end date is exclusive.
    last = (
        date.fromisoformat(end["date"]) - timedelta(days=1) if "date" in end else first
    )
    return first, max(first, last)


def when(event: dict[str, Any]) -> str:
    """`2026-10-07 09:00–09:30`, `2026-10-07 (all day)`, or a range across days."""
    start, end = event_dates(event)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        if start == end:
            return f"{start:%Y-%m-%d} (all day)"
        return f"{start:%Y-%m-%d} → {end:%Y-%m-%d} (all day)"
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M}–{end:%H:%M}"
    return f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}"


def sort_key(event: dict[str, Any]) -> datetime:
    """When an event starts, comparable across timed and all-day events."""
    start = event_dates(event)[0]
    if isinstance(start, datetime):
        return start
    return datetime(start.year, start.month, start.day).astimezone()


def my_response(event: dict[str, Any]) -> str:
    """` (declined)`, ` (tentative)` or ` (not answered)` for the user, else nothing."""
    me = next((a for a in event.get("attendees", []) if a.get("self")), None)
    status = me.get("responseStatus") if me else None
    labels = {
        "declined": "declined",
        "tentative": "tentative",
        "needsAction": "not answered",
    }
    if status in labels:
        return f" ({labels[status]})"
    if event.get("status") == "tentative":
        return " (tentative)"
    return ""


def title(event: dict[str, Any]) -> str:
    """An event's title, which busy-only calendars leave out."""
    return one_line(event.get("summary") or "(no title)", LABEL_CHARS)


def event_line(event: dict[str, Any], cal: Calendar) -> str:
    """One search result: `when | calendar | title | where | event_id`."""
    where = one_line(event.get("location", ""), LABEL_CHARS)
    return (
        f"{when(event)} | {one_line(cal.name, LABEL_CHARS)} | "
        f"{title(event)}{my_response(event)} | {where} | {event['id']}"
    )


def person(attendee: dict[str, Any]) -> str:
    """`Sam Rivera <sam@example.com>`, or `me` for the user."""
    if attendee.get("self"):
        return "me"
    name, email = attendee.get("displayName", ""), attendee.get("email", "")
    return f"{name} <{email}>" if name and email else name or email or "unknown"


def plain_text(text: str) -> str:
    """An event description as plain text: invites often carry HTML."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def video_link(event: dict[str, Any]) -> str:
    """The event's video call link, if it has one."""
    entry_points = event.get("conferenceData", {}).get("entryPoints", [])
    video = next((e for e in entry_points if e.get("entryPointType") == "video"), None)
    return str(video.get("uri", "")) if video else str(event.get("hangoutLink", ""))


class CalendarTools(Toolset):
    """
    The calendar tools over one `CalendarClient`. `now` gives the current local time, from
    which searches without dates are measured.
    """

    tool_names = ("search_events", "read_event")
    data = "Google Calendar events"
    prompt = PROMPT

    def __init__(
        self,
        client: CalendarClient,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.client = client
        self.now = now

    def pick_calendars(self, calendar: str) -> list[Calendar]:
        """The calendars a `calendar` filter means: all of them, or those whose name has it."""
        calendars = self.client.calendars()
        if not calendar:
            return calendars
        wanted = calendar.strip().casefold()
        exact = [c for c in calendars if wanted in (c.id.casefold(), c.name.casefold())]
        matches = exact or [c for c in calendars if wanted in c.name.casefold()]
        if not matches:
            names = ", ".join(c.name for c in calendars) or "none"
            raise ValueError(
                f"no calendar matches {calendar!r}; the calendars are: {names}"
            )
        return matches

    def window(self, start: str, end: str, text: str) -> tuple[datetime, datetime]:
        """
        The [first, last) local times a search covers; see `search_events` for defaults.
        The arithmetic is on wall-clock times, so "30 days from today" ends at midnight
        even across a daylight saving change.
        """
        reach = timedelta(days=TEXT_DAYS if text else SCHEDULE_DAYS)
        if start:
            first = parse_time_range(start, "start")[0]
            if end:
                last = parse_time_range(end, "end")[1] + timedelta(seconds=1)
            else:
                last = first + reach
        elif end:
            last = parse_time_range(end, "end")[1] + timedelta(seconds=1)
            first = last - reach
        else:
            now = self.now().astimezone().replace(tzinfo=None)
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            first, last = (today - reach if text else today), today + reach
        if first >= last:
            raise ValueError("start must be before end")
        # Naive times are local; astimezone() attaches the local offset at each end.
        return first.astimezone(), last.astimezone()

    def search_events(
        self,
        *,
        text: str = "",
        start: str = "",
        end: str = "",
        calendar: str = "",
        limit: int = 50,
    ) -> str:
        """
        Find calendar events, soonest first. A recurring event appears once per occurrence.

        With no dates, it searches the next 30 days from today, or a year either side of
        today when `text` is given. With only `start` or only `end`, it covers 30 days (a
        year with `text`) from that side.

        Args:
            text: Words to find in an event's title, description, location or attendees.
            start: Earliest time: "2026-08" (a month), "2026-08-01" or "2026-08-01 14:30".
            end: Latest time, same forms; a month or day includes all of it.
            calendar: Only calendars whose name contains this (all calendars if empty).
            limit: Maximum number of events to return.
        """
        limit = clamp_limit(limit)
        first, last = self.window(start, end, text)
        found: list[tuple[datetime, str]] = []
        more = False
        for cal in self.pick_calendars(calendar):
            events, cut = self.client.events(
                cal.id,
                time_min=first.isoformat(),
                time_max=last.isoformat(),
                text=text,
                limit=limit,
            )
            more = more or cut
            found += [
                (sort_key(event), event_line(event, cal))
                for event in events
                if event.get("status") != "cancelled"
            ]
        span = f"{first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M}"
        if not found:
            return f"No events from {span}" + (f" matching {text!r}." if text else ".")
        found.sort(key=lambda item: item[0])
        lines = ["when | calendar | title | where | event_id"]
        lines += [line for _, line in found[:limit]]
        if more or len(found) > limit:
            lines.append("… more events (narrow the dates, add text, or raise limit)")
        return "\n".join(lines)

    def read_event(self, event_id: str, calendar: str = "") -> str:
        """
        Read one calendar event in full: description, attendees and their answers, organizer
        and video link.

        Args:
            event_id: The event_id from search_events.
            calendar: The event's calendar name, as search_events shows it.
        """
        event_id = event_id.strip()
        if not event_id:
            raise ValueError("event_id is empty")
        for cal in self.pick_calendars(calendar):
            event = self.client.event(cal.id, event_id)
            if event is not None:
                return self.describe(event, cal)
        return f"No event {event_id!r}. Check the id and calendar with search_events."

    @staticmethod
    def describe(event: dict[str, Any], cal: Calendar) -> str:
        """An event's details, one per line."""
        lines = [
            f"{title(event)}{my_response(event)}",
            f"when: {when(event)}",
            f"calendar: {cal.name}",
        ]
        if event.get("status") not in (None, "confirmed"):
            lines.append(f"status: {event['status']}")
        if event.get("location"):
            lines.append(f"where: {one_line(event['location'], LABEL_CHARS)}")
        if event.get("recurringEventId") or event.get("recurrence"):
            lines.append("repeats: yes, this is one occurrence of a recurring event")
        organizer = event.get("organizer")
        if organizer and not organizer.get("self"):
            lines.append(f"organizer: {person(organizer)}")
        attendees = event.get("attendees", [])
        if attendees:
            people = ", ".join(
                f"{person(a)} ({a.get('responseStatus', 'unknown')})" for a in attendees
            )
            lines.append(f"attendees ({len(attendees)}): {one_line(people, 1500)}")
        if link := video_link(event):
            lines.append(f"video call: {link}")
        description = plain_text(event.get("description", ""))
        if description:
            if len(description) > DESCRIPTION_CHARS:
                description = description[: DESCRIPTION_CHARS - 1] + "…"
            lines.append(f"description:\n{description}")
        return "\n".join(lines)
