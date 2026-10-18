"""`list_sources`: what each enabled source holds, and how fresh it is."""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from vpop.db import DatabaseError
from vpop.sources.base import Source, SyncedSource
from vpop.sources.sync import SyncStatus, read_status
from vpop.tools.toolbox import Toolset


class StatusTools(Toolset):
    """`list_sources` over the enabled sources and the database they're synced into."""

    tool_names = ("list_sources",)

    def __init__(self, db: Path, sources: Sequence[Source]) -> None:
        self.db = db
        self.sources = list(sources)

    def list_sources(self) -> str:
        """
        List vPop's data sources and how fresh each one is.

        For each source: what it holds, how many messages and the span from the first to the
        newest, and when it last synced (or why it can't be used, and what to run). Call it
        when a question depends on recent messages, or to explain an empty result.
        """
        lines = [
            f"Now: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}. vPop's sources:"
        ]
        statuses = read_status()
        for source in self.sources:
            lines.append(f"- {source.name}: {source.description}")
            for store in source.stores():
                try:
                    summary = store.summary(self.db)
                except DatabaseError as exc:
                    summary = str(exc).replace("\n", " ")
                lines.append(f"  {store.kind}: {summary}")
            if isinstance(source, SyncedSource):
                lines.append(f"  {statuses.get(source.name, SyncStatus()).describe()}")
        return "\n".join(lines)
