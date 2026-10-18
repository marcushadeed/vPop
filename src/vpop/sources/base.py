"""
What a data source is.

A source is either *synced*, copied to disk by `vpop sync` (its raw files are the source of
truth and the database is derived from them, so it can be rebuilt), or *live*, read where it
lives while a question is answered, with nothing stored. Either way it describes its data as
`Store`s, each of one kind (messages, for now): the tools are by kind, so one search covers
every source of that kind. To add a source, subclass `Source` (or `SyncedSource`) and add it
to `vpop.sources.registry.SOURCES`.
"""

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from vpop.config import Config


class Store(ABC):  # pylint: disable=too-few-public-methods
    """
    Where one source keeps one kind of data, as the tools of that kind read it. Subclasses
    set `kind`, and the tools for that kind take a list of them.
    """

    kind: ClassVar[str]

    @abstractmethod
    def summary(self, db: Path) -> str:
        """
        What the source holds, for `list_sources`, e.g. `1,234 messages, 2019-03-01 08:00:00
        → 2026-10-18 09:12:00`. Raises `DatabaseError` if the database isn't usable.
        """


class Source(ABC):
    """A place the user's data lives, and how the tools read it."""

    # The source's config section, e.g. "android_messages".
    name: ClassVar[str]
    # The Google OAuth scopes it needs, if it reads a Google API.
    google_scopes: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def enabled(self) -> bool:
        """Whether the config turns this source on."""

    @abstractmethod
    def stores(self) -> list[Store]:
        """The source's data, one store per kind."""


class SyncedSource(Source):
    """A source copied to disk: its raw files are the source of truth, the database derived."""

    # Said on `vpop sync --rebuild` by a source whose raw files don't hold everything the
    # database does, e.g. one that keeps only its latest backup.
    rebuild_warning: ClassVar[str] = ""

    @abstractmethod
    def download(self, google_scopes: Sequence[str]) -> None:
        """
        Fetch new raw files. `google_scopes` are the scopes every enabled source needs, for
        a source that reads Google: asking for all of them makes one consent prompt.
        """

    @abstractmethod
    def import_raw(self, conn: sqlite3.Connection) -> str:
        """Import the raw files not imported yet, and say what was added, for the log."""
