"""`run_sql`: read-only SQL over the whole database, for counts and aggregates."""

import sqlite3
import time
from contextlib import closing

from vpop.assistant.toolbox import MAX_LIMIT, DatabaseToolset, clamp_limit, one_line

SQL_CELL_CHARS = 300
SQL_TIMEOUT_SECONDS = 5.0

# Authorizer actions a `run_sql` query may perform: reading and calling functions only.
ALLOWED_SQL_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


class SqlTools(DatabaseToolset):
    """`run_sql` over the database the synced sources import into."""

    tool_names = ("run_sql",)

    def run_sql(self, query: str, limit: int = 200) -> str:
        """
        Run one read-only SQL query against the database, for counts and aggregates.

        Only reading is allowed (SELECT or WITH), one statement per call, and a query that
        runs longer than a few seconds is stopped. Prefer the other tools for reading
        messages; use this for questions like "how many texts per month with X" or "who did
        I text most in 2025". Results are cut to `limit` rows.

        Args:
            query: A single SQLite SELECT or WITH statement over the messages and threads
                tables.
            limit: Maximum number of rows to return.
        """
        statement = query.strip()
        if not statement:
            raise ValueError("query is empty")
        limit = clamp_limit(limit)
        denied: list[int] = []
        deadline = time.monotonic() + SQL_TIMEOUT_SECONDS
        timed_out = False

        def authorize(action: int, *_: object) -> int:
            if action in ALLOWED_SQL_ACTIONS:
                return sqlite3.SQLITE_OK
            denied.append(action)
            return sqlite3.SQLITE_DENY

        def past_deadline() -> int:
            nonlocal timed_out
            timed_out = time.monotonic() > deadline
            return int(timed_out)

        with closing(self.connect()) as conn:
            conn.set_authorizer(authorize)
            conn.set_progress_handler(past_deadline, 10_000)
            try:
                cursor = conn.execute(statement)
                rows = cursor.fetchmany(limit + 1)
            except sqlite3.DatabaseError as exc:
                if denied:
                    raise ValueError(
                        "only read-only queries are allowed (SELECT or WITH)"
                    ) from exc
                if timed_out:
                    raise ValueError(
                        f"query stopped after {SQL_TIMEOUT_SECONDS:g}s; add filters, "
                        "aggregate further, or bound any recursive CTE"
                    ) from exc
                raise
            columns = [col[0] for col in cursor.description or []]

        if not rows:
            return " | ".join(columns) + "\n(no rows)"
        lines = [" | ".join(columns)]
        lines += [" | ".join(sql_cell(value) for value in row) for row in rows[:limit]]
        if len(rows) > limit:
            lines.append(
                f"… more rows (raise limit or aggregate further, max {MAX_LIMIT})"
            )
        return "\n".join(lines)


def sql_cell(value: object) -> str:
    """Render one result cell, making NULL and empty strings visible."""
    if value is None:
        return "NULL"
    if value == "":
        return '""'
    return one_line(str(value), SQL_CELL_CHARS)
