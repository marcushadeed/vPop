"""`vpop sync`: download new raw files for every synced source, then import them."""

import logging
import os

from vpop import db
from vpop.config import Config
from vpop.paths import db_path
from vpop.sources.base import SyncedSource
from vpop.sources.registry import enabled_sources, google_scopes

log = logging.getLogger(__name__)


def sync(config: Config, *, rebuild: bool = False, offline: bool = False) -> None:
    """
    Download new raw files (unless `offline`), then import the ones not imported yet. With
    `rebuild`, the database is recreated from every downloaded file; it's built beside the
    old one and swapped in only once complete. Live sources have nothing to sync.
    """
    sources = [s for s in enabled_sources(config) if isinstance(s, SyncedSource)]
    if not offline:
        scopes = google_scopes(config)
        for source in sources:
            source.download(scopes)
    target = db_path()
    path = target.with_name(target.name + ".rebuild") if rebuild else target
    if rebuild:
        path.unlink(missing_ok=True)
    try:
        conn = db.connect(path)
        try:
            summaries = [source.import_raw(conn) for source in sources]
        finally:
            conn.close()
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
