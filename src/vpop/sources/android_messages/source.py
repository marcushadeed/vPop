"""Android text messages: SMS Backup & Restore backups on Google Drive, synced into SQLite."""

import sqlite3
from pathlib import Path

from vpop.assistant.toolbox import Toolset
from vpop.db import check_readable
from vpop.paths import android_messages_raw_dir
from vpop.sources.base import SyncedSource


class AndroidMessages(SyncedSource):
    """
    Text messages from an Android phone. Always enabled: it's the core source, and
    `vpop sync` explains how to set it up.
    """

    name = "android_messages"

    def enabled(self) -> bool:
        return True

    def download(self) -> None:
        # Imported here so the parser and Google client libraries load only for a sync.
        # pylint: disable=import-outside-toplevel
        from vpop.sources.android_messages import sync

        sync.download(self.config, android_messages_raw_dir())

    def import_raw(self, conn: sqlite3.Connection) -> str:
        # pylint: disable=import-outside-toplevel
        from vpop.sources.android_messages import sync

        files, added = sync.import_backups(conn, android_messages_raw_dir())
        return f"{files} file(s) imported, {added} new messages"

    def toolsets(self, db: Path) -> list[Toolset]:
        # pylint: disable=import-outside-toplevel
        from vpop.sources.android_messages.tools import MessageTools

        check_readable(db)
        return [MessageTools(db)]
