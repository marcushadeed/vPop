"""
What a data source is.

A source is either *synced*, copied to disk by `vpop sync` (its raw files are the source of
truth and the database is derived from them, so it can be rebuilt), or *live*, read through
its API while a question is answered, with nothing stored. Either way it gives the assistant
a `Toolset` of read-only tools. To add a source, subclass `Source` (or `SyncedSource`) and add
it to `vpop.sources.registry.SOURCES`.
"""

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from vpop.assistant.toolbox import Toolset
from vpop.config import Config
from vpop.sources import SourceError


class SourceUnavailable(SourceError):
    """A source can't be queried right now. The message says why, and what to do."""


class Source(ABC):
    """A place the user's data lives, and how the assistant reads it."""

    # The source's config section, e.g. "google_calendar".
    name: ClassVar[str]
    # The Google OAuth scopes it needs, if it reads a Google API.
    google_scopes: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def enabled(self) -> bool:
        """Whether the config turns this source on."""

    @abstractmethod
    def toolsets(self, db: Path) -> list[Toolset]:
        """
        The tools for one conversation; `db` is the database synced sources import into.
        Raises `SourceUnavailable` (or `DatabaseError`) when the source can't be queried now.
        """


class SyncedSource(Source):
    """A source copied to disk: its raw files are the source of truth, the database derived."""

    @abstractmethod
    def download(self, google_scopes: Sequence[str]) -> None:
        """
        Fetch new raw files. `google_scopes` are the scopes every enabled source needs, for
        a source that reads Google: asking for all of them makes one consent prompt.
        """

    @abstractmethod
    def import_raw(self, conn: sqlite3.Connection) -> str:
        """Import the raw files not imported yet, and say what was added, for the log."""
