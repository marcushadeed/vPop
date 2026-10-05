"""Tests for the command-line interface."""

import pytest
from helpers import build_db, make_message

from vpop import cli
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


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys)
    assert code == 2
    assert "sync" in out and "ask" in out


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
