"""Tests for the benchmark fixture, cases, grader and runner. No model is called."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from helpers import ScriptedOllama, text_reply, tool_reply

from vpop.assistant.conversation import ToolCall, Trace
from vpop.assistant.tools import MessageTools
from vpop.benchmark import cases, fixture, run
from vpop.benchmark.cases import CASES, Case
from vpop.benchmark.grading import grade, numbers_in, phone_numbers_in
from vpop.paths import bench_results_dir

OLLAMA = run.BenchConfig("ollama", "m", "default")


@pytest.fixture(scope="module")
def fixture_db() -> Iterator[Path]:
    with run.fixture_db() as path:
        yield path


@pytest.fixture
def tools(fixture_db: Path) -> MessageTools:
    return MessageTools(fixture_db)


def query(path: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, params).fetchall()


def test_fixture_is_deterministic() -> None:
    assert fixture.all_messages() == fixture.all_messages()


def test_case_ids_are_unique() -> None:
    assert len({case.id for case in CASES}) == len(CASES)


def test_nothing_after_today(fixture_db: Path) -> None:
    [(latest,)] = query(fixture_db, "SELECT MAX(timestamp) FROM messages")
    assert latest < fixture.TODAY


def test_expected_text_is_in_the_database(fixture_db: Path) -> None:
    """Every substring a case requires appears in some body, contact name or timestamp."""
    rows = query(fixture_db, "SELECT body, contact_name, timestamp FROM messages")
    haystack = "\n".join("\n".join(row) for row in rows).casefold()
    for case in CASES:
        for needle in case.must_include:
            assert needle.casefold() in haystack, (case.id, needle)
        if case.any_of:
            assert any(n.casefold() in haystack for n in case.any_of), case.id


@pytest.mark.parametrize("word", ["paris", "taylor", "concert"])
def test_negative_cases_have_no_match(fixture_db: Path, word: str) -> None:
    rows = query(
        fixture_db,
        "SELECT COUNT(*) FROM messages WHERE body LIKE ? OR contact_name LIKE ?",
        f"%{word}%",
        f"%{word}%",
    )
    assert rows == [(0,)]


def test_aggregate_expectations_match_sql(fixture_db: Path) -> None:
    jordan = (fixture.JORDAN_OLD.number, fixture.JORDAN_NEW.number)
    assert query(
        fixture_db,
        "SELECT COUNT(*) FROM messages WHERE thread_key IN (?, ?) AND timestamp LIKE '2025%'",
        *jordan,
    ) == [(cases.JORDAN_2025,)]
    assert query(
        fixture_db,
        "SELECT COUNT(*) FROM messages WHERE thread_key = ? AND timestamp LIKE '2026-08%'",
        fixture.MOM.number,
    ) == [(cases.MOM_AUG_2026,)]
    assert query(
        fixture_db,
        "SELECT COUNT(*) FROM messages WHERE direction = 'outgoing' "
        "AND timestamp LIKE '2025%'",
    ) == [(cases.SENT_2025,)]
    assert query(
        fixture_db,
        "SELECT COUNT(*) FROM messages WHERE thread_key = ? AND direction = 'incoming' "
        "AND timestamp LIKE '2026-08%'",
        fixture.PRIYA.number,
    ) == [(cases.PRIYA_IN_LAST_MONTH,)]
    assert query(
        fixture_db,
        "SELECT COUNT(DISTINCT thread_key) FROM messages WHERE timestamp LIKE '2024%'",
    ) == [(cases.THREADS_2024,)]


def test_top_contact_2025_is_unambiguous(fixture_db: Path) -> None:
    rows = query(
        fixture_db,
        "SELECT thread_key, COUNT(*) AS n FROM messages WHERE timestamp LIKE '2025%' "
        "GROUP BY thread_key ORDER BY n DESC LIMIT 2",
    )
    assert rows[0][0] == fixture.PRIYA.number
    assert rows[0][1] > 1.5 * rows[1][1]


def test_most_recent_message_is_from_chris(fixture_db: Path) -> None:
    rows = query(
        fixture_db,
        "SELECT thread_key FROM messages WHERE direction = 'incoming' "
        "ORDER BY timestamp DESC LIMIT 1",
    )
    assert rows == [(fixture.CHRIS.number,)]


def test_numbers_in_reads_thousands_separators() -> None:
    assert numbers_in("about 1,234 texts in 12 months") == {1234, 12}


@pytest.mark.parametrize(
    "text",
    [
        "on 2024-08-01",
        "on 8/14/2026",
        "at 8:30",
        "on August 8th",
        "on the 8th of August",
        "from (202) 555-0108",
    ],
)
def test_numbers_in_ignores_dates_times_and_phones(text: str) -> None:
    assert 8 not in numbers_in(text)


def test_select_cases_rejects_unknown_tags() -> None:
    with pytest.raises(SystemExit, match="unknown tag"):
        run.select_cases([], ["lookups"])


@pytest.mark.parametrize(
    "text",
    [
        "+12025550101",
        "(202) 555-0101",
        "202-555-0101",
        "1 202 555 0101",
        "202.555.0101",
    ],
)
def test_phone_numbers_in_formats(text: str) -> None:
    assert phone_numbers_in(f"from {text}.") == {"2025550101"}


def make_case(**overrides: Any) -> Case:
    fields: dict[str, Any] = {"id": "t", "question": "q", "tags": ("x",)}
    fields.update(overrides)
    return Case(**fields)


def test_grade_text_checks() -> None:
    case = make_case(
        must_include=("Biscuit",),
        any_of=("march 14", "3/14"),
        must_match=(r"\bdog\b",),
        must_not_include=("cat",),
    )
    trace = Trace(tool_calls=[ToolCall("search_messages", {}, "")])
    assert grade(case, "His **dog** is Biscuit, born 3/14.", trace).passed
    result = grade(case, "The cat is Biscuit.", trace)
    assert not result.passed
    assert [c.name for c in result.checks if not c.passed] == [
        "includes one of ['march 14', '3/14']",
        "matches /\\bdog\\b/",
        "excludes 'cat'",
    ]
    assert result.score == pytest.approx(3 / 6)


def test_grade_negative_pattern_handles_curly_apostrophe() -> None:
    case = make_case(must_match=(cases.NEGATIVE,), must_not_match=(cases.AFFIRMATIVE,))
    trace = Trace(tool_calls=[ToolCall("search_messages", {}, "No messages match.")])
    assert grade(case, "Sam didn’t mention it.", trace).passed
    assert grade(
        case, "I searched Sam's messages and found no mention of Paris.", trace
    ).passed
    assert not grade(case, "Sam said Paris was great.", trace).passed
    assert not grade(
        case, "Sam mentioned Paris twice. Not once did he say Rome.", trace
    ).passed
    assert not grade(case, "Yes, but not recently.", trace).passed


def test_grade_fails_answers_without_tool_calls() -> None:
    case = make_case(must_match=(cases.NEGATIVE,))
    failed = [c.name for c in grade(case, "No, never.", Trace()).checks if not c.passed]
    assert failed == ["used a tool"]


def test_grade_number_and_trace_checks() -> None:
    case = make_case(expect_number=1234, expect_tools=("run_sql",), max_tool_errors=0)
    trace = Trace(tool_calls=[ToolCall("run_sql", {}, "n\n1234")])
    assert grade(case, "You sent 1,234 messages.", trace).passed
    bad = Trace(
        tool_calls=[ToolCall("search_messages", {}, "error: nope")],
        hit_round_limit=True,
    )
    failed = [
        c.name for c in grade(case, "You sent 12 messages.", bad).checks if not c.passed
    ]
    assert failed == [
        "states 1234",
        "called run_sql",
        "at most 0 tool errors",
        "finished within round limit",
    ]


def test_bench_config_labels_and_settings() -> None:
    assert OLLAMA.label == "m think=default"
    claude = run.BenchConfig("claude", "claude-sonnet-5-5", "low")
    assert claude.label == "claude-sonnet-5-5 effort=low"
    assert claude.settings().effort == "low"  # type: ignore[union-attr]


def test_run_case_records_trace_and_grades(tools: MessageTools) -> None:
    case = next(c for c in CASES if c.id == "sam-dog-name")
    client = ScriptedOllama(
        [
            tool_reply("find_threads", name_or_number="Sam"),
            tool_reply("search_messages", text="puppy", direction="bogus"),
            text_reply("Sam's dog is named Biscuit."),
        ]
    )
    record = run.run_case(case, OLLAMA, tools, client)
    assert record["passed"]
    assert record["rounds"] == 3
    assert record["prompt_tokens"] == 300 and record["output_tokens"] == 30
    assert [c["name"] for c in record["tool_calls"]] == [
        "find_threads",
        "search_messages",
    ]
    assert [c["is_error"] for c in record["tool_calls"]] == [False, True]


def test_run_case_hits_round_limit(tools: MessageTools) -> None:
    case = make_case(must_include=("x",))
    client = ScriptedOllama([tool_reply("find_threads", name_or_number="Sam")] * 12)
    record = run.run_case(case, OLLAMA, tools, client)
    assert record["hit_round_limit"]
    assert not record["passed"]


def test_run_case_records_backend_errors(tools: MessageTools) -> None:
    client = ScriptedOllama([ConnectionError("Failed to connect to Ollama.")])
    record = run.run_case(make_case(), OLLAMA, tools, client)
    assert record["error"] == "ConnectionError: Failed to connect to Ollama."
    assert not record["passed"]


def test_run_case_multi_turn_grades_last_answer(tools: MessageTools) -> None:
    case = next(c for c in CASES if c.id == "chris-restaurant-followup")
    client = ScriptedOllama(
        [
            tool_reply("search_messages", text="restaurant"),
            text_reply("Casa Verde."),
            text_reply("Adams Morgan."),
        ]
    )
    record = run.run_case(case, OLLAMA, tools, client)
    assert record["answers"] == ["Casa Verde.", "Adams Morgan."]
    assert record["passed"]


def test_run_writes_results_to_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = ScriptedOllama(
        [tool_reply("find_threads", name_or_number="Sam"), text_reply("Biscuit")]
    )
    monkeypatch.setattr(run, "ollama_client", lambda _: client)
    path = run.run([OLLAMA], case_ids=["sam-dog-name"])
    assert path.parent == bench_results_dir()
    [record] = [json.loads(line) for line in path.read_text().splitlines()]
    assert record["passed"] and record["provider"] == "ollama"
    assert "results:" in capsys.readouterr().out


def test_matrix_flags_disagreement() -> None:
    a = [{"case": "one", "passed": True}, {"case": "two", "passed": True}]
    b = [{"case": "one", "passed": True}, {"case": "two", "passed": False}]
    lines = run.matrix({"a": a, "b": b}).splitlines()
    assert lines[-2].startswith("one") and not lines[-2].endswith("*")
    assert lines[-1].startswith("two") and lines[-1].endswith("*")
