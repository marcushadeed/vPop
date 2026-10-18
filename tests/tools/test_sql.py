"""Tests for `run_sql`: read-only SQL over the whole database."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from helpers import SAM, build_db, make_message, message_toolbox, outgoing

from vpop.tools import sql
from vpop.tools.toolbox import Toolbox


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return build_db(
        tmp_path / "vpop.db",
        [
            make_message(body="one", timestamp="2026-03-01 10:00:00"),
            outgoing(body="two", timestamp="2026-03-01 10:05:00"),
            make_message(body="three", timestamp="2026-03-01 10:06:00"),
            make_message(
                thread_key="+13015550000",
                sender="+13015550000",
                body="four; with a semicolon",
                timestamp="2026-04-02 12:00:00",
            ),
            make_message(body="five", timestamp="2026-05-03 18:00:00"),
        ],
    )


@pytest.fixture
def box(db_file: Path) -> Toolbox:
    return message_toolbox(db_file)


def test_run_sql_aggregate(box: Toolbox) -> None:
    result = box.functions["run_sql"](
        "SELECT thread_key, COUNT(*) AS n FROM messages GROUP BY thread_key ORDER BY n DESC;"
    )
    assert result.splitlines()[:2] == ["thread_key | n", f"{SAM} | 4"]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT COUNT(*) FROM messages WHERE body LIKE '%;%'",
        "-- how many\nSELECT COUNT(*) FROM messages",
        "SELECT label FROM threads",
    ],
)
def test_run_sql_accepts_valid_reads(box: Toolbox, query: str) -> None:
    assert not box.call("run_sql", {"query": query}).startswith("error")


def test_run_sql_row_cap(box: Toolbox) -> None:
    lines = box.functions["run_sql"]("SELECT body FROM messages", limit=2).splitlines()
    assert len(lines) == 4
    assert lines[-1].startswith("… more rows")


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM messages",
        "ATTACH DATABASE 'x.db' AS x",
        "SELECT 1; DELETE FROM messages",
        "PRAGMA query_only = OFF",
        "WITH doomed AS (SELECT id FROM messages) DELETE FROM messages",
        "WITH x AS (SELECT 1) UPDATE messages SET body = 'gone'",
        "SELECT * FROM pragma_writable_schema",
        "",
    ],
)
def test_run_sql_cannot_write(box: Toolbox, db_file: Path, query: str) -> None:
    assert box.call(
        "run_sql", {"query": query} if query else {"query": " "}
    ).startswith("error:")
    with closing(sqlite3.connect(db_file)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE body = 'gone'"
            ).fetchone()[0]
            == 0
        )


def test_run_sql_names_the_read_only_rule(box: Toolbox) -> None:
    assert box.call("run_sql", {"query": "DELETE FROM messages"}) == (
        "error: only read-only queries are allowed (SELECT or WITH)"
    )


def test_runaway_query_is_stopped(
    box: Toolbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sql, "SQL_TIMEOUT_SECONDS", 0.2)
    result = box.call(
        "run_sql",
        {
            "query": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT COUNT(*) FROM c"
        },
    )
    assert result.startswith("error: query stopped after 0.2s")
