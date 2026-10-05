"""Tests for the read-only message query tools."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from helpers import SAM, build_db, make_message, message_toolbox, outgoing

from vpop.assistant import sql_tools, toolbox
from vpop.assistant.toolbox import Toolbox, time_range, tool_schema
from vpop.sources.android_messages.tools import MessageTools

ALEX = "+13015550000"
GROUP = f"{SAM},{ALEX}"


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return build_db(
        tmp_path / "vpop.db",
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
                body="lunch? 50% off_today",
                timestamp="2026-04-02 12:00:00",
            ),
            make_message(
                thread_key=GROUP,
                sender=ALEX,
                contact_name="Sam Smith, Alex Jones",
                body="group hello",
                timestamp="2026-05-03 18:00:00",
            ),
        ],
    )


@pytest.fixture
def tools(db_file: Path) -> MessageTools:
    return MessageTools(db_file)


@pytest.fixture
def box(db_file: Path) -> Toolbox:
    return message_toolbox(db_file)


@pytest.mark.parametrize(
    "query", ["240-555-1234", "(240) 555 1234", "+12405551234", "5551234"]
)
def test_find_threads_phone_formats(tools: MessageTools, query: str) -> None:
    result = tools.find_threads(query)
    assert f"{SAM} | Sam Smith | 3 msgs" in result
    assert GROUP in result
    assert not any(line.startswith(f"{ALEX} |") for line in result.splitlines())


def test_find_threads_by_name(tools: MessageTools) -> None:
    lines = tools.find_threads("sam").splitlines()
    # Header, then most recently active first: the group, then Sam's 1:1 thread.
    assert lines[1].startswith(GROUP)
    assert lines[2].startswith(f"{SAM} | Sam Smith | 3 msgs | 2026-03-01 10:00:00")


def test_find_threads_no_match(tools: MessageTools) -> None:
    assert tools.find_threads("nobody") == "No threads match 'nobody'."
    assert tools.find_threads("%") == "No threads match '%'."


def test_search_words_match_word_forms(tools: MessageTools) -> None:
    assert tools.search_messages(text="DENVER") == (
        f"2026-03-01 10:00:00 | Sam Smith [{SAM}] | Sam Smith | I'm moving to Denver in June"
    )
    assert "Denver" in tools.search_messages(text="move denv")
    assert tools.search_messages(text="moving paris") == "No messages match."


def test_search_text_without_words_points_to_contains(box: Toolbox) -> None:
    assert "use contains" in box.call("search_messages", {"text": "%%"})


def test_contains_is_a_literal_substring(tools: MessageTools) -> None:
    assert "lunch?" in tools.search_messages(contains="50% OFF_")
    assert tools.search_messages(contains="_").count("\n") == 0  # only the lunch line
    assert tools.search_messages(contains="%x") == "No messages match."


def test_search_group_sender_gets_name(tools: MessageTools) -> None:
    assert tools.search_messages(text="group") == (
        f"2026-05-03 18:00:00 | {GROUP} | Alex Jones | group hello"
    )


def test_search_thread_filter_drops_thread_column(tools: MessageTools) -> None:
    result = tools.search_messages(thread_key="2405551234", sender="me")
    assert result == "2026-03-01 10:05:00 | me | No way, when?"


def test_search_date_bounds(tools: MessageTools) -> None:
    assert tools.search_messages(
        since="2026-04-01", until="2026-04-02"
    ).splitlines() == [
        f"2026-04-02 12:00:00 | Alex Jones [{ALEX}] | Alex Jones | lunch? 50% off_today"
    ]
    assert tools.search_messages(until="2026-03-01").count("\n") == 2
    assert tools.search_messages(since="2026-04", until="2026-04").count("\n") == 0


@pytest.mark.parametrize(
    ("bad", "field"), [("2026/08/01", "since"), ("August 2026", "until")]
)
def test_bad_dates_are_errors(box: Toolbox, bad: str, field: str) -> None:
    result = box.call("search_messages", {field: bad})
    assert result.startswith(f"error: {field} must look like")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-02", ("2024-02-01 00:00:00", "2024-02-29 23:59:59")),
        ("2026-08-01", ("2026-08-01 00:00:00", "2026-08-01 23:59:59")),
        ("2026-08-01 14:30", ("2026-08-01 14:30:00", "2026-08-01 14:30:59")),
        ("2026-08-01T14:30:05", ("2026-08-01 14:30:05", "2026-08-01 14:30:05")),
    ],
)
def test_time_range(value: str, expected: tuple[str, str]) -> None:
    assert time_range(value, "since") == expected


def test_time_range_rejects_impossible_dates() -> None:
    with pytest.raises(ValueError, match="must look like"):
        time_range("2026-02-30", "since")


def test_search_bad_direction(tools: MessageTools) -> None:
    with pytest.raises(ValueError):
        tools.search_messages(direction="sideways")


def test_search_me_conflicts_with_incoming(tools: MessageTools) -> None:
    with pytest.raises(ValueError):
        tools.search_messages(sender="me", direction="incoming")


def test_search_more_line_and_offset(tools: MessageTools) -> None:
    first = tools.search_messages(limit=2).splitlines()
    assert len(first) == 3
    assert first[-1] == "… 3 more (use offset=2)"
    assert tools.search_messages(limit=2, offset=4).splitlines() == [
        f"2026-03-01 10:00:00 | Sam Smith [{SAM}] | Sam Smith | I'm moving to Denver in June"
    ]


def test_body_is_truncated_to_one_line(db_file: Path) -> None:
    build_db(db_file, [make_message(body="line one\nline two " + "x" * 500)])
    body = (
        MessageTools(db_file).search_messages(contains="line one").rsplit(" | ", 1)[-1]
    )
    assert body.startswith("line one ⏎ line two")
    assert len(body) == toolbox.BODY_CHARS
    assert body.endswith("…")


def test_unsent_and_attachments_are_shown(db_file: Path) -> None:
    build_db(
        db_file,
        [
            outgoing(
                body="did this go?", was_sent=False, timestamp="2026-06-01 08:00:00"
            ),
            make_message(
                body="",
                attachments="image/jpeg,video/mp4",
                timestamp="2026-06-01 09:00:00",
            ),
        ],
    )
    lines = MessageTools(db_file).read_thread(SAM, since="2026-06").splitlines()
    assert lines[1:] == [
        "2026-06-01 08:00:00 | me (not sent) | did this go?",
        "2026-06-01 09:00:00 | Sam Smith | [image/jpeg] [video/mp4]",
    ]


def test_read_thread_latest(tools: MessageTools) -> None:
    assert tools.read_thread(SAM, limit=2).splitlines() == [
        f"thread {SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-03-01 10:05:00 | me | No way, when?",
        "2026-03-01 10:06:00 | Sam Smith | First week of June",
    ]


def test_read_thread_around(tools: MessageTools) -> None:
    lines = tools.read_thread(
        "2405551234", around="2026-03-01 10:05:00", limit=2
    ).splitlines()
    assert lines == [
        f"thread {SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-03-01 10:05:00 | me | No way, when?",
        "2026-03-01 10:06:00 | Sam Smith | First week of June",
    ]


def test_read_thread_since(tools: MessageTools) -> None:
    assert tools.read_thread(SAM, since="2026-03-01", limit=1).splitlines() == [
        f"thread {SAM} (Sam Smith): 1 of 3 messages",
        "2026-03-01 10:00:00 | Sam Smith | I'm moving to Denver in June",
        "… 2 later (use since or around)",
    ]


def test_read_thread_counts_messages_sharing_a_second(tmp_path: Path) -> None:
    same = "2026-07-01 12:00:00"
    path = build_db(
        tmp_path / "vpop.db",
        [
            make_message(body=f"burst {i}", timestamp=same, epoch_ms=1000 + i)
            for i in range(4)
        ],
    )
    lines = MessageTools(path).read_thread(SAM, limit=2).splitlines()
    assert lines[1] == "… 2 earlier (use until or around)"
    assert [line.split(" | ")[-1] for line in lines[2:]] == ["burst 2", "burst 3"]


def test_read_thread_group_key_with_spaces(tools: MessageTools) -> None:
    assert "group hello" in tools.read_thread(GROUP.replace(",", ", "))


def test_read_thread_unknown(tools: MessageTools) -> None:
    assert tools.read_thread("+19999999999").startswith("No messages in thread")


def test_run_sql_aggregate(box: Toolbox) -> None:
    result = box.functions["run_sql"](
        "SELECT thread_key, COUNT(*) AS n FROM messages GROUP BY thread_key ORDER BY n DESC;"
    )
    assert result.splitlines()[:2] == ["thread_key | n", f"{SAM} | 3"]


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
    monkeypatch.setattr(sql_tools, "SQL_TIMEOUT_SECONDS", 0.2)
    result = box.call(
        "run_sql",
        {
            "query": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT COUNT(*) FROM c"
        },
    )
    assert result.startswith("error: query stopped after 0.2s")


def test_results_are_capped(box: Toolbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toolbox, "MAX_RESULT_CHARS", 80)
    result = box.call("search_messages", {})
    assert len(result.splitlines()[0]) <= 80
    assert result.splitlines()[-1].startswith("… output cut at 80 characters")


def test_call_drops_empty_arguments_and_checks_the_rest(box: Toolbox) -> None:
    assert "Denver" in box.call("search_messages", {"text": "denver", "sender": None})
    assert box.call("search_messages", {"bogus": 1}).startswith("error:")
    assert box.call("drop_table", {}).startswith("error: unknown tool")


def test_missing_database_is_a_tool_error(tmp_path: Path) -> None:
    result = message_toolbox(tmp_path / "none.db").call(
        "find_threads", {"name_or_number": "x"}
    )
    assert result.startswith("error: No database at")


def test_tool_schema_from_signature_and_docstring(box: Toolbox) -> None:
    schema = tool_schema(box.functions["search_messages"])
    assert schema["name"] == "search_messages"
    assert schema["description"].startswith("Search messages")
    assert "Args:" not in schema["description"]
    params = schema["parameters"]
    assert params["required"] == []
    assert params["properties"]["limit"]["type"] == "integer"
    assert params["properties"]["text"]["type"] == "string"
    assert "moving" in params["properties"]["text"]["description"]
    find = tool_schema(box.functions["find_threads"])
    assert find["parameters"]["required"] == ["name_or_number"]
