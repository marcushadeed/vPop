"""Tests for the read-only message query tools."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from vpop.assistant import message_queries
from vpop.assistant.message_queries import (
    find_threads,
    read_thread,
    run_sql,
    search_messages,
)
from vpop.sources.android_messages import xml_to_sqlite
from vpop.sources.android_messages.xml_to_sqlite import (
    Direction,
    Message,
    add_messages_to_sqlite,
)

SAM = "+12405551234"
ALEX = "+13015550000"
GROUP = f"{SAM},{ALEX}"


def make_message(**overrides: object) -> Message:
    fields: dict[str, object] = {
        "direction": Direction.INCOMING,
        "was_sent": True,
        "thread_key": SAM,
        "sender": SAM,
        "contact_name": "Sam Smith",
        "body": "hello",
        "timestamp": "2026-08-01 09:00:00",
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


def outgoing(**overrides: object) -> Message:
    return make_message(direction=Direction.OUTGOING, sender="", **overrides)


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vpop.db"
    monkeypatch.setattr(xml_to_sqlite, "db_path", lambda: path)
    monkeypatch.setattr(message_queries, "db_path", lambda: path)
    add_messages_to_sqlite(
        [
            make_message(
                body="I'm moving to Denver in June", timestamp="2026-03-01 10:00:00"
            ),
            outgoing(body="No way, when?", timestamp="2026-03-01 10:05:00"),
            make_message(body="First week of June", timestamp="2026-03-01 10:06:00"),
            make_message(
                thread_key=ALEX,
                sender=ALEX,
                contact_name="Alex Jones",
                body="lunch?",
                timestamp="2026-04-02 12:00:00",
            ),
            make_message(
                thread_key=GROUP,
                sender=ALEX,
                contact_name="Sam Smith, Alex Jones",
                body="group hello",
                timestamp="2026-05-03 18:00:00",
            ),
        ]
    )
    return path


@pytest.mark.parametrize(
    "query", ["240-555-1234", "(240) 555 1234", "+12405551234", "5551234"]
)
def test_find_threads_phone_formats(db_file: Path, query: str) -> None:
    result = find_threads(query)
    assert f"{SAM} | Sam Smith | 3 msgs" in result
    assert GROUP in result
    assert not any(line.startswith(f"{ALEX} |") for line in result.splitlines())


def test_find_threads_by_name(db_file: Path) -> None:
    result = find_threads("sam")
    lines = result.splitlines()
    # Header, then most recently active first: the group, then Sam's 1:1 thread.
    assert lines[1].startswith(GROUP)
    assert lines[2].startswith(f"{SAM} | Sam Smith | 3 msgs | 2026-03-01 10:00:00")


def test_find_threads_no_match(db_file: Path) -> None:
    assert find_threads("nobody") == "No threads match 'nobody'."


def test_search_text_is_case_insensitive(db_file: Path) -> None:
    result = search_messages(text="DENVER")
    assert result == (
        f"2026-03-01 10:00:00 | Sam Smith [{SAM}] | Sam Smith | I'm moving to Denver in June"
    )


def test_search_group_sender_gets_name(db_file: Path) -> None:
    assert search_messages(text="group") == (
        f"2026-05-03 18:00:00 | {GROUP} | Alex Jones | group hello"
    )


def test_search_thread_filter_drops_thread_column(db_file: Path) -> None:
    result = search_messages(thread_key="2405551234", sender="me")
    assert result == "2026-03-01 10:05:00 | me | No way, when?"


def test_search_date_bounds(db_file: Path) -> None:
    result = search_messages(since="2026-04-01", until="2026-04-02")
    assert result.splitlines() == [
        f"2026-04-02 12:00:00 | Alex Jones [{ALEX}] | Alex Jones | lunch?"
    ]
    assert search_messages(until="2026-03-01").count("\n") == 2


def test_search_bad_direction(db_file: Path) -> None:
    with pytest.raises(ValueError):
        search_messages(direction="sideways")


def test_search_me_conflicts_with_incoming(db_file: Path) -> None:
    with pytest.raises(ValueError):
        search_messages(sender="me", direction="incoming")


def test_search_more_line_and_offset(db_file: Path) -> None:
    first = search_messages(limit=2).splitlines()
    assert len(first) == 3
    assert first[-1] == "… 3 more (use offset=2)"
    last = search_messages(limit=2, offset=4).splitlines()
    assert last == [
        "2026-03-01 10:00:00 | Sam Smith [+12405551234] | Sam Smith | I'm moving to Denver in June"
    ]


def test_body_is_truncated_to_one_line(db_file: Path) -> None:
    add_messages_to_sqlite([make_message(body="line one\nline two " + "x" * 500)])
    result = search_messages(text="line one")
    body = result.split(" | ")[-1]
    assert body.startswith("line one ⏎ line two")
    assert len(body) == message_queries.BODY_CHARS
    assert body.endswith("…")


def test_read_thread_latest(db_file: Path) -> None:
    assert read_thread(SAM, limit=2).splitlines() == [
        f"thread {SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-03-01 10:05:00 | me | No way, when?",
        "2026-03-01 10:06:00 | Sam Smith | First week of June",
    ]


def test_read_thread_around(db_file: Path) -> None:
    lines = read_thread(
        "2405551234", around="2026-03-01 10:05:00", limit=2
    ).splitlines()
    assert lines == [
        f"thread {SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-03-01 10:05:00 | me | No way, when?",
        "2026-03-01 10:06:00 | Sam Smith | First week of June",
    ]


def test_read_thread_since(db_file: Path) -> None:
    lines = read_thread(SAM, since="2026-03-01", limit=1).splitlines()
    assert lines == [
        f"thread {SAM} (Sam Smith): 1 of 3 messages",
        "2026-03-01 10:00:00 | Sam Smith | I'm moving to Denver in June",
        "… 2 later (use since or around)",
    ]


def test_read_thread_unknown(db_file: Path) -> None:
    assert read_thread("+19999999999").startswith("No messages in thread")


def test_run_sql_aggregate(db_file: Path) -> None:
    result = run_sql(
        "SELECT thread_key, COUNT(*) AS n FROM messages GROUP BY thread_key ORDER BY n DESC;"
    )
    assert result.splitlines()[:2] == ["thread_key | n", f"{SAM} | 3"]


def test_run_sql_row_cap(db_file: Path) -> None:
    lines = run_sql("SELECT body FROM messages", limit=2).splitlines()
    assert len(lines) == 4
    assert lines[-1].startswith("… more rows")


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM messages",
        "ATTACH DATABASE 'x.db' AS x",
        "SELECT 1; DELETE FROM messages",
        "PRAGMA query_only = OFF",
        "",
    ],
)
def test_run_sql_rejects_non_select(db_file: Path, query: str) -> None:
    with pytest.raises(ValueError):
        run_sql(query)


@pytest.mark.parametrize(
    "query",
    [
        "WITH doomed AS (SELECT id FROM messages) DELETE FROM messages",
        "WITH x AS (SELECT 1) UPDATE messages SET body = 'gone'",
        "SELECT * FROM pragma_writable_schema",
    ],
)
def test_run_sql_cannot_write(db_file: Path, query: str) -> None:
    with pytest.raises(sqlite3.Error):
        run_sql(query)
    with closing(sqlite3.connect(db_file)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE body = 'gone'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5


def test_connect_is_read_only(db_file: Path) -> None:
    with (
        closing(message_queries.connect()) as conn,
        pytest.raises(sqlite3.OperationalError),
    ):
        conn.execute("DELETE FROM messages")


def test_missing_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(message_queries, "db_path", lambda: tmp_path / "none.db")
    with pytest.raises(FileNotFoundError):
        find_threads("sam")
