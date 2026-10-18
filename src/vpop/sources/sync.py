"""
`vpop sync`: download new raw files for every synced source, then import them.

Each run records, per source, when it last tried, when it last succeeded and why it last
failed, in `sync-status.json` in the data directory. That's run state, not data, so it stays
out of the database, which can be rebuilt. A lock file keeps two syncs (say the timer's and
one typed at the terminal) from running at once.
"""

import fcntl
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime

from vpop import db
from vpop.config import Config
from vpop.fsutil import atomic_write
from vpop.paths import db_path, sync_lock_path, sync_status_path
from vpop.sources import SourceError
from vpop.sources.base import SyncedSource
from vpop.sources.registry import enabled_sources, google_scopes

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncStatus:
    """How a source's syncs went: local `YYYY-MM-DD HH:MM:SS` times, None if never."""

    last_attempt: str | None = None
    last_success: str | None = None
    # Why the latest attempt failed; None once one succeeds.
    last_error: str | None = None

    def describe(self) -> str:
        """A line for `list_sources` and `vpop schedule status`."""
        if self.last_error is not None:
            return (
                f"last sync failed at {self.last_attempt}: {self.last_error} "
                f"(last success: {self.last_success or 'never'})"
            )
        if self.last_success:
            return f"last synced {self.last_success}"
        return "no sync recorded; run `vpop sync`"


def read_status() -> dict[str, SyncStatus]:
    """Every source's sync status, by source name; empty if no sync has recorded one."""
    try:
        data = json.loads(sync_status_path().read_text())
    except (OSError, ValueError):
        return {}
    names = {f.name for f in fields(SyncStatus)}
    statuses = {}
    for source, values in data.items() if isinstance(data, dict) else ():
        if isinstance(values, dict):
            statuses[source] = SyncStatus(
                **{k: v for k, v in values.items() if k in names and isinstance(v, str)}
            )
    return statuses


def write_status(statuses: dict[str, SyncStatus]) -> None:
    """Replace the status file with `statuses`, in one step."""
    data = {name: asdict(status) for name, status in sorted(statuses.items())}
    atomic_write(sync_status_path(), json.dumps(data, indent=2) + "\n")


@contextmanager
def sync_lock() -> Iterator[None]:
    """Hold the sync lock, or raise `SourceError` if another sync holds it."""
    path = sync_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SourceError(
                "another vpop sync is running; try again once it's done"
            ) from None
        yield


def one_line(exc: Exception) -> str:
    """An error's message on one line, for the status file."""
    return " ".join(str(exc).split()) or type(exc).__name__


def sync(
    config: Config,
    *,
    rebuild: bool = False,
    offline: bool = False,
    interactive: bool = True,
) -> None:
    """
    Download new raw files (unless `offline`), then import the ones not imported yet. With
    `rebuild`, the database is recreated from every downloaded file; it's built beside the
    old one and swapped in only once every source is in. A source that fails doesn't stop
    the others; its error is recorded and raised at the end. Without `interactive`, a
    source never opens a browser for consent. Live sources have nothing to sync.
    """
    sources = [s for s in enabled_sources(config) if isinstance(s, SyncedSource)]
    with sync_lock():
        now = f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}"
        errors: dict[str, Exception] = {}
        try:
            run(
                config,
                sources,
                errors,
                rebuild=rebuild,
                offline=offline,
                interactive=interactive,
            )
        except Exception as exc:
            # The database itself failed, so every source did.
            for source in sources:
                errors.setdefault(source.name, exc)
            raise
        finally:
            statuses = read_status()
            for source in sources:
                failure = errors.get(source.name)
                statuses[source.name] = replace(
                    statuses.get(source.name, SyncStatus()),
                    last_attempt=now,
                    last_error=None if failure is None else one_line(failure),
                    **({} if failure else {"last_success": now}),
                )
            write_status(statuses)
    if errors:
        first, *rest = errors.values()
        for error in rest:
            log.error("%s", error)
        raise first


def run(  # pylint: disable=too-many-arguments
    config: Config,
    sources: list[SyncedSource],
    errors: dict[str, Exception],
    *,
    rebuild: bool,
    offline: bool,
    interactive: bool,
) -> None:
    """`sync`'s work: each source's failure goes in `errors`, keyed by its name."""
    if not offline:
        download(config, sources, errors, interactive=interactive)
    target = db_path()
    path = target.with_name(target.name + ".rebuild") if rebuild else target
    if rebuild:
        path.unlink(missing_ok=True)
        for source in sources:
            if source.rebuild_warning:
                log.warning("%s", source.rebuild_warning)
    try:
        conn = db.connect(path)
        try:
            summaries = import_raw(conn, sources, errors)
        finally:
            conn.close()
        if rebuild and errors:
            # The old database stays, so no source was rebuilt.
            for source in sources:
                errors.setdefault(
                    source.name, SourceError("not rebuilt, since another source failed")
                )
            path.unlink()
            return
        if rebuild:
            os.replace(path, target)
    except BaseException:
        if rebuild:
            path.unlink(missing_ok=True)
        raise
    log.info(
        "%s %s: %s",
        "rebuilt" if rebuild else "synced",
        target,
        "; ".join(summaries) or "no synced sources",
    )


# Whatever goes wrong with one source (Drive, the network, a bad backup) is recorded in
# `errors`, and raised once the others have synced.


def download(
    config: Config,
    sources: list[SyncedSource],
    errors: dict[str, Exception],
    *,
    interactive: bool,
) -> None:
    """Download each source's new raw files."""
    scopes = google_scopes(config)
    for source in sources:
        try:
            source.download(scopes, interactive=interactive)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors[source.name] = exc


def import_raw(
    conn: sqlite3.Connection,
    sources: list[SyncedSource],
    errors: dict[str, Exception],
) -> list[str]:
    """Import each source whose download worked, and say what each added."""
    summaries = []
    for source in sources:
        if source.name in errors:
            continue
        try:
            summaries.append(source.import_raw(conn))
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors[source.name] = exc
    return summaries
