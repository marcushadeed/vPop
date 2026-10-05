"""Google Calendar as a live source: read through the API at question time, nothing stored."""

from pathlib import Path

from vpop.assistant.toolbox import Toolset
from vpop.sources.base import Source, SourceUnavailable
from vpop.sources.google import CALENDAR_READONLY


class GoogleCalendar(Source):
    """The user's Google calendars, read-only and live. Off unless the config enables it."""

    name = "google_calendar"
    google_scopes = (CALENDAR_READONLY,)

    def enabled(self) -> bool:
        return self.config.google_calendar.enabled

    def toolsets(self, db: Path) -> list[Toolset]:
        # Imported here so the Google libraries load only when the calendar is enabled.
        # pylint: disable=import-outside-toplevel
        from vpop.sources.google.auth import granted_scopes
        from vpop.sources.google_calendar.client import CalendarClient
        from vpop.sources.google_calendar.tools import CalendarTools

        # Checked here, from the token file, so a missing grant is one warning when the
        # conversation starts rather than a failed tool call in the middle of a question.
        if CALENDAR_READONLY not in granted_scopes():
            raise SourceUnavailable(
                "Google Calendar is enabled but vPop can't read it yet; run "
                "`vpop auth google`"
            )
        client = CalendarClient(self.config.google_calendar.exclude_calendars)
        return [CalendarTools(client)]
