"""Tests for `vpop auth`. Keys are checked with a stand-in; no API call is made."""

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from vpop.assistant import auth
from vpop.config import Config, load_config, parse_config


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_mask_keeps_prefix_and_last_four() -> None:
    assert auth.mask("sk-ant-api03-abcdefghijklmnop1234") == "sk-ant-…1234"
    assert auth.mask("short") == "…"


def test_save_key_replaces_key_keeps_other_lines(tmp_path: Path) -> None:
    path = tmp_path / "anthropic.env"
    path.write_text(
        "# comment\nANTHROPIC_BASE_URL=http://proxy\nANTHROPIC_API_KEY=old\n"
    )
    path.chmod(0o644)
    auth.save_key(path, "sk-ant-new")
    assert path.read_text() == (
        "# comment\nANTHROPIC_BASE_URL=http://proxy\nANTHROPIC_API_KEY=sk-ant-new\n"
    )
    assert mode(path) == 0o600


def test_save_key_creates_private_file(tmp_path: Path) -> None:
    path = tmp_path / "vpop" / "anthropic.env"
    auth.save_key(path, "sk-ant-new")
    assert auth.read_env_file(path) == {"ANTHROPIC_API_KEY": "sk-ant-new"}
    assert mode(path) == 0o600
    assert not auth.readable_by_others(path)


def test_remove_key_deletes_file_left_with_only_comments(tmp_path: Path) -> None:
    path = tmp_path / "anthropic.env"
    path.write_text("# my key\nANTHROPIC_API_KEY=sk-ant-x\n")
    assert auth.remove_key(path)
    assert not path.exists()
    assert not auth.remove_key(path)


def test_remove_key_keeps_other_settings(tmp_path: Path) -> None:
    path = tmp_path / "anthropic.env"
    path.write_text("ANTHROPIC_API_KEY=sk-ant-x\nANTHROPIC_BASE_URL=http://proxy\n")
    assert auth.remove_key(path)
    assert path.read_text() == "ANTHROPIC_BASE_URL=http://proxy\n"


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch, no_credentials: Path) -> Path:
    """A terminal session with no `ant` CLI and no credentials; returns the key file."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(auth.shutil, "which", lambda _: None)
    return no_credentials


def script(
    monkeypatch: pytest.MonkeyPatch, keys: list[str], answers: list[str]
) -> None:
    """Feed the hidden key prompt and the yes/no questions."""
    key_iter: Iterator[str] = iter(keys)
    answer_iter: Iterator[str] = iter(answers)
    monkeypatch.setattr(auth.getpass, "getpass", lambda _: next(key_iter))
    monkeypatch.setattr("builtins.input", lambda _: next(answer_iter))


def check(key: str) -> str | None:
    return None if key == "sk-ant-good" else "API key is invalid."


def test_login_retries_bad_key_and_saves_good_one(
    monkeypatch: pytest.MonkeyPatch, terminal: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script(monkeypatch, ["sk-ant-bad", "sk-ant-good"], [])
    config = parse_config("[assistant]\nlocal_model = false\n")
    assert auth.login(config, check=check)
    assert auth.read_env_file(terminal) == {"ANTHROPIC_API_KEY": "sk-ant-good"}
    assert "didn't work: API key is invalid." in capsys.readouterr().out


def test_login_cancel_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, terminal: Path
) -> None:
    script(monkeypatch, [""], [])
    assert not auth.login(Config(), check=check)
    assert not terminal.exists()


def test_login_can_switch_off_local_model(
    monkeypatch: pytest.MonkeyPatch, terminal: Path
) -> None:
    config_file = terminal.parent / "config.toml"
    load_config(config_file)  # writes the default file, local_model = true
    script(monkeypatch, ["sk-ant-good"], [""])
    assert auth.login(load_config(config_file), check=check)
    text = config_file.read_text()
    assert "local_model = false" in text
    assert "# true: a local Ollama model" in text  # comments survive
    assert load_config(config_file).assistant.local_model is False


def test_login_warns_about_exported_key(
    monkeypatch: pytest.MonkeyPatch, terminal: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    script(monkeypatch, ["sk-ant-good"], [])
    auth.login(parse_config("[assistant]\nlocal_model = false\n"), check=check)
    assert "ANTHROPIC_API_KEY is set in your shell" in capsys.readouterr().err


def test_login_needs_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="needs a terminal"):
        auth.login(Config())


def test_offer_login_stays_quiet_off_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert not auth.offer_login(Config())


def test_status_without_credentials_fails(
    no_credentials: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = parse_config("[assistant]\nlocal_model = false\n")
    assert auth.status(config) == 1
    out = capsys.readouterr().out
    assert "credentials: none" in out
    assert "vpop auth login" in out


def test_status_reports_saved_key_and_check(
    monkeypatch: pytest.MonkeyPatch,
    no_credentials: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth.save_key(no_credentials, "sk-ant-api03-abcdefghijklmnop1234")
    monkeypatch.setattr(auth, "check_client", lambda _: None)
    assert auth.status(Config()) == 0
    out = capsys.readouterr().out
    assert f"ANTHROPIC_API_KEY from {no_credentials}, sk-ant-…1234" in out
    assert "check: ok" in out


def test_status_warns_about_readable_key_file(
    monkeypatch: pytest.MonkeyPatch,
    no_credentials: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth.save_key(no_credentials, "sk-ant-api03-abcdefghijklmnop1234")
    no_credentials.chmod(0o644)
    monkeypatch.setattr(auth, "check_client", lambda _: "API key is invalid.")
    assert auth.status(Config()) == 1
    out = capsys.readouterr().out
    assert "other users can read" in out
    assert "check: failed: API key is invalid." in out
