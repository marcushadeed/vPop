"""Tests for the command-line interface."""

import sys

import pytest

from vpop import cli
from vpop.paths import config_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Run the CLI; returns its exit code (0 if it returned normally), stdout and stderr."""
    try:
        cli.main(list(argv))
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
    out, err = capsys.readouterr()
    return code, out, err


def test_help_lists_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--help")
    assert code == 0
    assert "sync" in out and "auth" in out
    assert "ask" not in out and "bench" not in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys)
    assert code == 0
    assert out.startswith("usage: vpop")


def test_config_init_and_show(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "config", "init")
    assert code == 0 and str(config_path()) in out
    code, _, err = run(capsys, "config", "init")
    assert code == 1 and "--force" in err
    code, out, _ = run(capsys, "config", "show")
    assert "[whatsapp]\nenabled = false" in out and "#" not in out.splitlines()[1]


def test_bad_config_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    config_path().parent.mkdir(parents=True)
    config_path().write_text("[whatsapp]\nenabled = 1\n")
    code, _, err = run(capsys, "config", "show")
    assert code == 1 and "bad config" in err and "must be bool" in err


def test_config_path_prints_the_location(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "config", "path")
    assert code == 0 and out == f"{config_path()}\n"
    assert not config_path().exists()


def test_config_edit_creates_and_opens_the_file(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VISUAL", "true")
    code, _, _ = run(capsys, "config", "edit")
    assert code == 0 and "[whatsapp]\n# " in config_path().read_text()


def test_config_edit_reports_a_bad_edit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "VISUAL", """sh -c 'printf "[whatsapp]\\nenabled = 1\\n" > "$1"' sh"""
    )
    code, _, err = run(capsys, "config", "edit")
    assert code == 1 and "bad config" in err and "must be bool" in err


def test_config_edit_without_an_editor(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VISUAL", "no-such-editor-xyz")
    code, _, err = run(capsys, "config", "edit")
    assert code == 1 and "editor not found: no-such-editor-xyz" in err


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
