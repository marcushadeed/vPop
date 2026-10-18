"""WhatsApp: Android's encrypted backups on Google Drive, synced into SQLite."""

import sqlite3
from collections.abc import Sequence

from vpop.paths import whatsapp_raw_dir
from vpop.sources.base import Store, SyncedSource
from vpop.sources.google import DRIVE_READONLY
from vpop.sources.message_queries import MessageStore
from vpop.sources.whatsapp.model import normalize_chat

MESSAGES = MessageStore(
    prefix="whatsapp",
    description="WhatsApp messages",
    table="whatsapp_messages",
    key="chat_key",
    normalize_key=normalize_chat,
    normalize_sender=normalize_chat,
    conversations="whatsapp_chats",
    name_match="label LIKE ? ESCAPE '\\'",
    # A 1:1 chat's label is the person's name from their text thread, or their number.
    names="SELECT chat_key, label FROM whatsapp_chats WHERE NOT is_group AND label != ''",
    is_group="is_group",
)


class WhatsApp(SyncedSource):
    """WhatsApp messages from an Android phone. Off unless the config enables it."""

    name = "whatsapp"
    description = (
        "WhatsApp messages from an Android phone, as `whatsapp:` conversations"
    )
    google_scopes = (DRIVE_READONLY,)
    rebuild_warning = (
        "WhatsApp keeps only its latest backup on disk, so the rebuilt database won't have "
        "WhatsApp messages deleted on the phone since earlier syncs"
    )

    def enabled(self) -> bool:
        return self.config.whatsapp.enabled

    def download(self, google_scopes: Sequence[str], *, interactive: bool) -> None:
        # Imported here so the Google and crypto libraries load only for a sync.
        # pylint: disable=import-outside-toplevel
        from vpop.sources.whatsapp import sync

        sync.download(
            self.config, whatsapp_raw_dir(), google_scopes, interactive=interactive
        )

    def import_raw(self, conn: sqlite3.Connection) -> str:
        # pylint: disable=import-outside-toplevel
        from vpop.sources.whatsapp import sync

        files, added = sync.import_latest(conn, whatsapp_raw_dir())
        return f"{files} WhatsApp file(s) imported, {added} new WhatsApp messages"

    def stores(self) -> list[Store]:
        return [MESSAGES]
