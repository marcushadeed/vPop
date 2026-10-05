"""
The SQLite database: its schema, migrations and connections.

The database is derived data. The raw backups under the data directory are the source of
truth, so `vpop sync --rebuild` can always recreate it, which is how a database from a
version too old to migrate is replaced.

Tables:
- `messages`: one row per text message (see `Message` for the columns).
- `messages_fts`: an FTS5 index over `messages.body`, kept current by triggers.
- `threads`: one row per conversation with its display label, message count and first/last
  timestamps, recomputed after every import.
- `imports`: which backup files have been imported, so a sync skips them next time.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 1

# Contact names the backup uses when it doesn't know who a number belongs to.
UNKNOWN_NAMES = ("", "(Unknown)")


class DatabaseError(RuntimeError):
    """The database is missing or unusable. The message says what to do."""


def create_v1(conn: sqlite3.Connection) -> None:
    """The first versioned schema."""
    conn.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            epoch_ms INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            direction TEXT NOT NULL,
            was_sent INTEGER NOT NULL,
            thread_key TEXT NOT NULL,
            sender TEXT NOT NULL,
            contact_name TEXT NOT NULL,
            body TEXT NOT NULL,
            attachments TEXT NOT NULL DEFAULT '',
            rcs_message_id TEXT NOT NULL DEFAULT '',
            from_mms INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_thread_epoch ON messages (thread_key, epoch_ms);
        CREATE INDEX idx_messages_epoch ON messages (epoch_ms);
        CREATE INDEX idx_messages_timestamp ON messages (timestamp);

        CREATE VIRTUAL TABLE messages_fts USING fts5(
            body, content='messages', content_rowid='rowid',
            tokenize='porter unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts (rowid, body) VALUES (new.rowid, new.body);
        END;
        CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts (messages_fts, rowid, body)
            VALUES ('delete', old.rowid, old.body);
        END;
        CREATE TRIGGER messages_fts_update AFTER UPDATE OF body ON messages BEGIN
            INSERT INTO messages_fts (messages_fts, rowid, body)
            VALUES ('delete', old.rowid, old.body);
            INSERT INTO messages_fts (rowid, body) VALUES (new.rowid, new.body);
        END;

        CREATE TABLE threads (
            thread_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            first TEXT NOT NULL,
            last TEXT NOT NULL
        );

        CREATE TABLE imports (
            source TEXT NOT NULL,
            name TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            messages INTEGER NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (source, name)
        );
        """
    )


# MIGRATIONS[n] takes a database at version n to version n + 1. Append, never edit.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [create_v1]
assert len(MIGRATIONS) == SCHEMA_VERSION


def user_version(conn: sqlite3.Connection) -> int:
    """The schema version stored in the database file."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def outdated_message(path: Path) -> str:
    """What to do about a database from an older vpop."""
    return (
        f"{path} was built by an older version of vpop and can't be upgraded in place.\n"
        "Rebuild it from the downloaded backups with: vpop sync --rebuild"
    )


def migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Bring the schema up to `SCHEMA_VERSION`, or raise if the file is too old or too new."""
    version = user_version(conn)
    if (
        version == 0
        and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'messages'"
        ).fetchone()
    ):
        raise DatabaseError(outdated_message(path))
    if version > SCHEMA_VERSION:
        raise DatabaseError(
            f"{path} was built by a newer version of vpop (schema {version}); upgrade vpop"
        )
    for step in range(version, SCHEMA_VERSION):
        with conn:
            MIGRATIONS[step](conn)
            conn.execute(f"PRAGMA user_version = {step + 1}")


def connect(path: Path) -> sqlite3.Connection:
    """Open the database for writing, creating it or migrating its schema as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        migrate(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """
    Open the database read-only.

    `mode=ro` stops writes at the file level and `query_only` stops them at the statement
    level, so a query that slips past other checks still can't change anything.
    """
    if not path.exists():
        raise DatabaseError(f"No database at {path}. Run `vpop sync` first.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    if user_version(conn) != SCHEMA_VERSION:
        conn.close()
        raise DatabaseError(outdated_message(path))
    return conn


def check_readable(path: Path) -> None:
    """Raise `DatabaseError` unless the database exists and has the current schema."""
    connect_readonly(path).close()


def message_summary(path: Path) -> tuple[int, str | None]:
    """How many messages the database holds, and the newest one's timestamp (None if empty)."""
    with closing(connect_readonly(path)) as conn:
        count, newest = conn.execute(
            "SELECT COALESCE(SUM(message_count), 0), MAX(last) FROM threads"
        ).fetchone()
    return int(count), newest


def refresh_threads(conn: sqlite3.Connection) -> None:
    """
    Recompute the `threads` table. A thread's label is the most recent contact name the
    backup recorded on it, skipping the placeholders it uses for unknown numbers.
    """
    with conn:
        conn.execute("DELETE FROM threads")
        conn.execute(
            "INSERT INTO threads (thread_key, label, message_count, first, last) "
            "SELECT thread_key, COALESCE(("
            "    SELECT contact_name FROM messages AS named "
            "    WHERE named.thread_key = m.thread_key AND contact_name NOT IN (?, ?) "
            "    ORDER BY epoch_ms DESC LIMIT 1"
            "), ''), COUNT(*), MIN(timestamp), MAX(timestamp) "
            "FROM messages AS m GROUP BY thread_key",
            UNKNOWN_NAMES,
        )


def already_imported(conn: sqlite3.Connection, source: str, file: Path) -> bool:
    """Whether this exact file (same name, size and modification time) was imported."""
    stat = file.stat()
    row = conn.execute(
        "SELECT size, mtime_ns FROM imports WHERE source = ? AND name = ?",
        (source, file.name),
    ).fetchone()
    return row is not None and tuple(row) == (stat.st_size, stat.st_mtime_ns)


def record_import(
    conn: sqlite3.Connection, source: str, file: Path, messages: int
) -> None:
    """Remember that `file` was imported, so later syncs skip it."""
    stat = file.stat()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO imports "
            "(source, name, size, mtime_ns, messages, imported_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))",
            (source, file.name, stat.st_size, stat.st_mtime_ns, messages),
        )
