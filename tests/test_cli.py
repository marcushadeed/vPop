"""Tests for the command-line interface."""

import re
import sys

import pytest
from helpers import ScriptedOllama, build_db, make_message, text_reply, tool_reply

from vpop import cli
from vpop.assistant.conversation import Conversation
from vpop.paths import config_path, db_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Run the CLI; returns its exit code (0 if it returned normally), stdout and stderr."""
    try:
        cli.main(list(argv))
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if isinstance(exc.code, str):
            print(exc.code, file=__import__("sys").stderr)
    out, err = capsys.readouterr()
    return code, out, err


def test_help_lists_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--help")
    assert code == 0
    assert "sync" in out and "ask" in out


def test_no_command_is_ask_without_a_question(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = run(capsys)
    assert code == 1
    assert "Run `vpop sync` first" in err


def at_terminal(monkeypatch: pytest.MonkeyPatch, *streams: str) -> None:
    """Make the named streams ("stdin", "stdout") look like a terminal."""
    for name in streams:
        monkeypatch.setattr(getattr(sys, name), "isatty", lambda: True)


def answers(monkeypatch: pytest.MonkeyPatch, *replies: object) -> None:
    """Make the local model reply with `replies` in turn."""
    monkeypatch.setattr("ollama.Client.chat", ScriptedOllama(list(replies)).chat)


def test_no_command_at_a_terminal_opens_the_chat(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    build_db(db_path(), [make_message()])
    opened: list[Conversation] = []
    monkeypatch.setattr(
        "vpop.ui.chat.run_chat", lambda conversation, *_: opened.append(conversation)
    )
    at_terminal(monkeypatch, "stdin", "stdout")
    code, _, _ = run(capsys)
    assert code == 0
    assert len(opened) == 1


def test_ask_prints_just_the_answer_when_piped(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    build_db(db_path(), [make_message()])
    answers(monkeypatch, tool_reply("search_messages", text="hello"), text_reply("Hi."))
    code, out, _ = run(capsys, "ask", "what did sam say?")
    assert (code, out) == (0, "Hi.\n")


def test_ask_at_a_terminal_shows_its_progress(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    build_db(db_path(), [make_message()])
    answers(monkeypatch, tool_reply("search_messages", text="hello"), text_reply("Hi."))
    at_terminal(monkeypatch, "stdout")
    code, out, _ = run(capsys, "ask", "what did sam say?")
    shown = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out)
    assert code == 0
    assert '● search_messages(text: "hello")' in shown
    assert "● Hi." in shown


def test_config_init_and_show(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "config", "init")
    assert code == 0 and str(config_path()) in out
    code, _, err = run(capsys, "config", "init")
    assert code == 1 and "--force" in err
    code, out, _ = run(capsys, "config", "show")
    assert "local_model = true" in out and "#" not in out.splitlines()[1]


def test_bad_config_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    config_path().parent.mkdir(parents=True)
    config_path().write_text("[assistant]\nmax_rounds = 0\n")
    code, _, err = run(capsys, "config", "show")
    assert code == 1 and "bad config" in err and "at least 1" in err


def test_ask_without_database_explains(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "ask", "hi")
    assert code == 1
    assert "Run `vpop sync` first" in err


def test_ask_with_ollama_down_explains(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    build_db(db_path(), [make_message()])

    def down(*_: object, **__: object) -> None:
        raise ConnectionError("Failed to connect to Ollama.")

    monkeypatch.setattr("ollama.Client.chat", down)
    code, _, err = run(capsys, "ask", "hi")
    assert code == 1
    assert "ollama serve" in err


def test_sync_without_folder_explains(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "sync")
    assert code == 1
    assert "drive_folder_id" in err


def test_offline_sync_logs_a_summary(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "sync", "--offline")
    assert code == 0
    assert "0 file(s) imported" in err
    code, _, err = run(capsys, "-q", "sync", "--offline")
    assert err == ""


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["bench", "--effort", "low"], "--effort is for --provider claude"),
        (["bench", "--provider", "claude", "--think", "on"], "--think is for"),
        (["bench", "--repeat", "0"], "must be at least 1"),
    ],
)
def test_bench_rejects_mismatched_options(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    code, _, err = run(capsys, *argv)
    assert code != 0 and message in err


def test_bench_list(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "bench", "--list")
    assert code == 0 and "sam-dog-name" in out
