"""WhatsApp: Android's encrypted backups on Google Drive, synced into SQLite."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from vpop.assistant.toolbox import Toolset
from vpop.db import check_readable
from vpop.paths import whatsapp_raw_dir
from vpop.sources.base import SyncedSource
from vpop.sources.google import DRIVE_READONLY


class WhatsApp(SyncedSource):
    """WhatsApp messages from an Android phone. Off unless the config enables it."""

    name = "whatsapp"
    google_scopes = (DRIVE_READONLY,)
    rebuild_warning = (
        "WhatsApp keeps only its latest backup on disk, so the rebuilt database won't have "
        "WhatsApp messages deleted on the phone since earlier syncs"
    )

    def enabled(self) -> bool:
        return self.config.whatsapp.enabled

    def download(self, google_scopes: Sequence[str]) -> None:
        # Imported here so the Google and crypto libraries load only for a sync.
        # pylint: disable=import-outside-toplevel
        from vpop.sources.whatsapp import sync

        sync.download(self.config, whatsapp_raw_dir(), google_scopes)

    def import_raw(self, conn: sqlite3.Connection) -> str:
        # pylint: disable=import-outside-toplevel
        from vpop.sources.whatsapp import sync

        files, added = sync.import_latest(conn, whatsapp_raw_dir())
        return f"{files} WhatsApp file(s) imported, {added} new WhatsApp messages"

    def toolsets(self, db: Path) -> list[Toolset]:
        # pylint: disable=import-outside-toplevel
        from vpop.sources.whatsapp.tools import WhatsAppTools

        check_readable(db)
        return [WhatsAppTools(db)]
