"""
Read-only query functions over the `messages` table, used as LLM tools.

Every function returns compact text rather than JSON, one line per message
(`timestamp | thread | from | body`), so tool results stay small. Bodies are cut to
`BODY_CHARS` and result sets are capped, with a trailing `… N more` line saying how to page.
"""

import re
import sqlite3
from contextlib import closing

from vpop.paths import db_path
from vpop.sources.android_messages.xml_to_sqlite import (
    Direction,
    normalize_address,
)
from vpop.sources.android_messages.xml_to_sqlite import (
    thread_key as normalize_thread_key,
)

BODY_CHARS = 300
SQL_CELL_CHARS = 300
MAX_LIMIT = 500

# Contact names the backup uses when it doesn't know who a number belongs to.
UNKNOWN_NAMES = ("", "(Unknown)")

# Authorizer actions a `run_sql` query may perform: reading and calling functions only.
ALLOWED_SQL_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def connect() -> sqlite3.Connection:
    """
    Open the database read-only.

    `mode=ro` stops writes at the file level and `query_only` stops them at the statement level,
    so a query that slips past `run_sql`'s checks still can't change anything.
    """
    path = db_path()
    if not path.exists():
        raise FileNotFoundError(f"No database at {path}. Run `vpop sync` first.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def one_line(text: str, limit: int = BODY_CHARS) -> str:
    """Collapse newlines and cut `text` to `limit` characters."""
    flat = re.sub(r"\s*\n\s*", " ⏎ ", text.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def clamp_limit(limit: int) -> int:
    """Keep a caller-supplied limit between 1 and `MAX_LIMIT`."""
    return max(1, min(int(limit), MAX_LIMIT))


def end_of_day(until: str) -> str:
    """Make a bare `YYYY-MM-DD` upper bound include that whole day."""
    return f"{until} 23:59:59" if len(until) == 10 else until


def normalize_thread(key: str) -> str:
    """
    Accept a thread key in any phone format.

    Group keys are already the sorted comma list stored in the table; a single number is
    normalized the same way the importer does it.
    """
    key = key.strip()
    return key if "," in key else normalize_thread_key(key)


def contact_names(conn: sqlite3.Connection) -> dict[str, str]:
    """
    Map each 1:1 thread's number to its most recent contact name.

    Group messages only carry the sender's number, so this is how a sender in a group gets a
    name.
    """
    rows = conn.execute(
        "SELECT thread_key, contact_name, MAX(timestamp) FROM messages "
        "WHERE thread_key NOT LIKE '%,%' AND contact_name NOT IN (?, ?) "
        "GROUP BY thread_key",
        UNKNOWN_NAMES,
    )
    return {key: name for key, name, _ in rows}


def thread_label(conn: sqlite3.Connection, key: str) -> str:
    """Return the most recent contact name recorded on a thread."""
    row = conn.execute(
        "SELECT contact_name FROM messages "
        "WHERE thread_key = ? AND contact_name NOT IN (?, ?) "
        "ORDER BY timestamp DESC LIMIT 1",
        (key, *UNKNOWN_NAMES),
    ).fetchone()
    return row[0] if row else "unknown"


def sender_label(direction: str, sender: str, names: dict[str, str]) -> str:
    """Return `me` for outgoing messages, else the sender's name or number."""
    if direction == Direction.OUTGOING.value:
        return "me"
    return names.get(sender, sender or "unknown")


def find_threads(name_or_number: str, limit: int = 20) -> str:
    """
    Find conversations by contact name or phone number.

    Use this first to turn a person's name into a thread_key. Matches part of a contact name
    ("sam") or part of a phone number in any format ("240-555-1234", "5551234"). Group threads
    containing the person are included.

    Args:
        name_or_number: Part of a contact name, or part of a phone number.
        limit: Maximum number of threads to return, most recently active first.
    """
    query = name_or_number.strip()
    if not query:
        raise ValueError("name_or_number is empty")
    limit = clamp_limit(limit)
    patterns = {f"%{query}%", f"%{normalize_address(query)}%"}
    digits = re.sub(r"\D", "", query)
    if len(digits) >= 4:
        patterns.add(f"%{digits}%")
    key_match = " OR ".join("thread_key LIKE ?" for _ in patterns)

    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT thread_key, COUNT(*), MIN(timestamp), MAX(timestamp) FROM messages "
            "WHERE thread_key IN (SELECT thread_key FROM messages "
            f"WHERE contact_name LIKE ? OR {key_match}) "
            "GROUP BY thread_key ORDER BY MAX(timestamp) DESC",
            (f"%{query}%", *patterns),
        ).fetchall()
        lines = [
            f"{key} | {one_line(thread_label(conn, key), 200)} | {count} msgs | "
            f"{first} → {last}"
            for key, count, first, last in rows[:limit]
        ]

    if not lines:
        return f"No threads match {query!r}."
    header = "thread_key | contact name(s) | messages | first → last"
    more = [f"… {len(rows) - limit} more (raise limit)"] if len(rows) > limit else []
    return "\n".join([header, *lines, *more])


def search_messages(
    text: str | None = None,
    thread_key: str | None = None,
    sender: str | None = None,
    since: str | None = None,
    until: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """
    Search messages with any combination of filters, newest first.

    Args:
        text: Case-insensitive substring to look for in the message body.
        thread_key: Only messages in this thread (from find_threads).
        sender: Only messages from this phone number, or "me" for messages I sent.
        since: Earliest timestamp, "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS" (inclusive).
        until: Latest timestamp, same formats (inclusive; a bare date covers the whole day).
        direction: "incoming" or "outgoing".
        limit: Maximum number of messages to return.
        offset: Number of newest matches to skip, for paging.
    """
    clauses: list[str] = []
    params: list[str] = []
    if text:
        clauses.append("body LIKE ?")
        params.append(f"%{text}%")
    if thread_key:
        clauses.append("thread_key = ?")
        params.append(normalize_thread(thread_key))
    if sender:
        if sender.strip().lower() == "me":
            if direction == Direction.INCOMING.value:
                raise ValueError(
                    'sender="me" means outgoing messages; drop direction="incoming"'
                )
            direction = Direction.OUTGOING.value
        else:
            clauses.append("sender = ?")
            params.append(normalize_address(sender))
    if direction:
        if direction not in {d.value for d in Direction}:
            raise ValueError('direction must be "incoming" or "outgoing"')
        clauses.append("direction = ?")
        params.append(direction)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(end_of_day(until))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = clamp_limit(limit)
    offset = max(0, int(offset))

    with closing(connect()) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM messages {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT timestamp, thread_key, direction, sender, body FROM messages "
            f"{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        names = contact_names(conn) if rows else {}

    if not rows:
        return (
            "No messages match." if total == 0 else f"No messages past offset {offset}."
        )
    lines = []
    for ts, key, direc, snd, body in rows:
        if thread_key:
            thread_col = ""
        else:
            thread_col = f"{names[key]} [{key}] | " if key in names else f"{key} | "
        lines.append(
            f"{ts} | {thread_col}{sender_label(direc, snd, names)} | {one_line(body)}"
        )
    remaining = total - offset - len(rows)
    if remaining > 0:
        lines.append(f"… {remaining} more (use offset={offset + len(rows)})")
    return "\n".join(lines)


def read_thread(
    thread_key: str,
    around: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 60,
) -> str:
    """
    Read a conversation in chronological order.

    With `around`, returns the messages on either side of that timestamp, which is how to see
    the context of a search hit. With `since`, reads forward from it. Otherwise returns the
    latest messages (before `until`, if given).

    Args:
        thread_key: The thread to read (from find_threads or search_messages).
        around: A timestamp to center the window on.
        since: Earliest timestamp to include (inclusive).
        until: Latest timestamp to include (inclusive; a bare date covers the whole day).
        limit: Maximum number of messages to return.
    """
    key = normalize_thread(thread_key)
    limit = clamp_limit(limit)
    clauses = ["thread_key = ?"]
    params = [key]
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(end_of_day(until))
    where = " AND ".join(clauses)
    select = f"SELECT timestamp, direction, sender, body FROM messages WHERE {where}"

    with closing(connect()) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where}", params
        ).fetchone()[0]
        if total == 0:
            return f"No messages in thread {key!r} for that range. Check the key with find_threads."
        if around:
            before = conn.execute(
                f"{select} AND timestamp <= ? ORDER BY timestamp DESC LIMIT ?",
                (*params, around, (limit + 1) // 2),
            ).fetchall()
            after = conn.execute(
                f"{select} AND timestamp > ? ORDER BY timestamp ASC LIMIT ?",
                (*params, around, limit - len(before)),
            ).fetchall()
            rows = [*reversed(before), *after]
        elif since:
            rows = conn.execute(
                f"{select} ORDER BY timestamp ASC LIMIT ?", (*params, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                f"{select} ORDER BY timestamp DESC LIMIT ?", (*params, limit)
            ).fetchall()
            rows.reverse()
        first, last = rows[0][0], rows[-1][0]
        earlier = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where} AND timestamp < ?",
            (*params, first),
        ).fetchone()[0]
        later = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where} AND timestamp > ?",
            (*params, last),
        ).fetchone()[0]
        names = contact_names(conn)
        label = thread_label(conn, key)

    lines = [f"thread {key} ({one_line(label, 200)}): {len(rows)} of {total} messages"]
    if earlier:
        lines.append(f"… {earlier} earlier (use until or around)")
    lines += [
        f"{ts} | {sender_label(direc, snd, names)} | {one_line(body)}"
        for ts, direc, snd, body in rows
    ]
    if later:
        lines.append(f"… {later} later (use since or around)")
    return "\n".join(lines)


def check_select(query: str) -> str:
    """Return `query` without a trailing `;`, or raise if it isn't one SELECT/WITH statement."""
    stripped = query.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("query is empty")
    if ";" in stripped:
        raise ValueError("only a single statement is allowed")
    first_word = stripped.split(None, 1)[0].lower()
    if first_word not in {"select", "with"}:
        raise ValueError("only SELECT or WITH queries are allowed")
    return stripped


def authorize(action: int, *_: object) -> int:
    """sqlite3 authorizer that only lets a statement read."""
    return sqlite3.SQLITE_OK if action in ALLOWED_SQL_ACTIONS else sqlite3.SQLITE_DENY


def sql_cell(value: object) -> str:
    """Render one result cell, making NULL and empty strings visible."""
    if value is None:
        return "NULL"
    if value == "":
        return '""'
    return one_line(str(value), SQL_CELL_CHARS)


def run_sql(query: str, limit: int = 200) -> str:
    """
    Run one read-only SQL query against the database, for counts and aggregates.

    Only a single SELECT or WITH statement is allowed. Prefer the other tools for reading
    messages; use this for questions like "how many texts per month with X" or "who did I text
    most in 2025". Results are cut to `limit` rows.

    Args:
        query: A single SQLite SELECT or WITH statement over the messages table.
        limit: Maximum number of rows to return.
    """
    statement = check_select(query)
    limit = clamp_limit(limit)
    with closing(connect()) as conn:
        conn.set_authorizer(authorize)
        cursor = conn.execute(statement)
        rows = cursor.fetchmany(limit + 1)
        columns = [col[0] for col in cursor.description or []]

    if not rows:
        return " | ".join(columns) + "\n(no rows)"
    lines = [" | ".join(columns)]
    lines += [" | ".join(sql_cell(value) for value in row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"… more rows (raise limit or aggregate further, max {MAX_LIMIT})")
    return "\n".join(lines)
