"""Android text messages: SMS Backup & Restore backups on Google Drive, synced into SQLite."""

import sqlite3
from collections.abc import Sequence

from vpop.paths import android_messages_raw_dir
from vpop.sources.android_messages.model import normalize_address, normalize_thread
from vpop.sources.base import Store, SyncedSource
from vpop.sources.google import DRIVE_READONLY
from vpop.sources.message_queries import MessageStore

MESSAGES = MessageStore(
    prefix="text",
    description="text messages",
    table="messages",
    key="thread_key",
    normalize_key=normalize_thread,
    normalize_sender=normalize_address,
    conversations="threads",
    name_match="thread_key IN (SELECT thread_key FROM messages "
    "WHERE contact_name LIKE ? ESCAPE '\\')",
    # Group messages carry only the sender's number; their 1:1 thread names them.
    names="SELECT thread_key, label FROM threads "
    "WHERE thread_key NOT LIKE '%,%' AND label != ''",
    # A group is a list of numbers, or an RCS group's id.
    is_group="thread_key LIKE '%,%' OR thread_key LIKE '%@%'",
    was_sent="was_sent",
)


class AndroidMessages(SyncedSource):
    """
    Text messages from an Android phone. Always enabled: it's the core source, and
    `vpop sync` explains how to set it up.
    """

    name = "android_messages"
    description = "text messages (SMS, MMS and RCS) from an Android phone, as `text:` conversations"
    google_scopes = (DRIVE_READONLY,)

    def enabled(self) -> bool:
        return True

    def download(self, google_scopes: Sequence[str]) -> None:
        # Imported here so the parser and Google client libraries load only for a sync.
        # pylint: disable=import-outside-toplevel
        from vpop.sources.android_messages import sync

        sync.download(self.config, android_messages_raw_dir(), google_scopes)

    def import_raw(self, conn: sqlite3.Connection) -> str:
        # pylint: disable=import-outside-toplevel
        from vpop.sources.android_messages import sync

        files, added = sync.import_backups(conn, android_messages_raw_dir())
        return f"{files} file(s) imported, {added} new messages"

    def stores(self) -> list[Store]:
        return [MESSAGES]
