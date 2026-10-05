"""Tests for reading and generating the config file, and the paths it lives at."""

from pathlib import Path

import pytest

from vpop.assistant.harness import Conversation, Settings, new_conversation
from vpop.config import (
    ClaudeConfig,
    Config,
    ConfigError,
    load_config,
    parse_config,
    render_config,
    set_setting,
    write_default_config,
)
from vpop.paths import config_path, config_paths, data_dir


def test_missing_file_is_created_with_defaults(tmp_path: Path) -> None:
    path = tmp_path / "vpop" / "config.toml"
    assert load_config(path) == Config()
    assert path.read_text() == render_config(Config())


def test_unwritable_location_still_gives_defaults(tmp_path: Path) -> None:
    blocker = tmp_path / "vpop"
    blocker.write_text("a file where the directory should be")
    assert load_config(blocker / "config.toml") == Config()


def test_empty_file_gives_defaults() -> None:
    assert parse_config("") == Config()


def test_unspecified_fields_keep_defaults() -> None:
    config = parse_config(
        "[assistant]\nlocal_model = false\n[ollama]\nnum_ctx = 4096\n"
    )
    assert config.assistant.local_model is False
    assert config.assistant.max_rounds == Config().assistant.max_rounds
    assert config.ollama.num_ctx == 4096
    assert config.ollama.model == Config().ollama.model
    assert config.claude == ClaudeConfig()


def test_generated_file_round_trips(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "vpop" / "config.toml")
    text = path.read_text()
    assert "local_model = true" in text
    assert "# true: a local Ollama model" in text
    assert load_config(path) == Config()


def test_render_round_trips_non_defaults() -> None:
    config = parse_config('[claude]\nmodel = "claude-sonnet-5-5"\neffort = "high"\n')
    assert parse_config(render_config(config)) == config


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[assistant]\nlocal_model = false\n")
    with pytest.raises(FileExistsError):
        write_default_config(path)
    assert load_config(path).assistant.local_model is False
    write_default_config(path, force=True)
    assert load_config(path) == Config()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[assistant]\nlocal_modle = true\n", "unknown setting"),
        ("[olama]\nmodel = 'x'\n", "unknown section"),
        ("[assistant]\nlocal_model = 'no'\n", "must be bool"),
        ("[ollama]\nnum_ctx = true\n", "must be int"),
        ("[ollama]\nthink = 'maybe'\n", "ollama.think must be one of on, off, default"),
        ("[claude]\neffort = 'huge'\n", "claude.effort must be one of"),
        ("[assistant]\nmax_rounds = 0\n", "max_rounds must be at least 1"),
        ("[ollama]\nnum_ctx = 512\n", "num_ctx must be at least 2048"),
        ("assistant = 3\n", "must be a table"),
        ("[assistant\n", "invalid TOML"),
    ],
)
def test_bad_config_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(text)


def test_load_error_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[assistant]\nlocal_model = 1\n")
    with pytest.raises(ConfigError, match=str(path)):
        load_config(path)


@pytest.fixture
def homes(tmp_path: Path) -> tuple[Path, Path]:
    """The XDG and home config paths the isolated environment points at."""
    return (
        tmp_path / "xdg" / "vpop" / "config.toml",
        tmp_path / "home" / ".config" / "vpop" / "config.toml",
    )


def test_config_paths_prefer_xdg(homes: tuple[Path, Path]) -> None:
    assert config_paths() == list(homes)


def test_config_paths_without_xdg(
    monkeypatch: pytest.MonkeyPatch, homes: tuple[Path, Path]
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config_paths() == [homes[1]]


def test_relative_xdg_paths_are_ignored(
    monkeypatch: pytest.MonkeyPatch, homes: tuple[Path, Path], tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    assert config_paths() == [homes[1]]
    assert data_dir() == tmp_path / "home" / ".local" / "share" / "vpop"


def test_existing_home_config_is_used(homes: tuple[Path, Path]) -> None:
    xdg, home = homes
    home.parent.mkdir(parents=True)
    home.write_text("[assistant]\nlocal_model = false\n")
    assert config_path() == home
    assert load_config().assistant.local_model is False
    assert not xdg.exists()


def test_existing_xdg_config_wins(homes: tuple[Path, Path]) -> None:
    for path, local in zip(homes, ("true", "false"), strict=True):
        path.parent.mkdir(parents=True)
        path.write_text(f"[assistant]\nlocal_model = {local}\n")
    assert load_config().assistant.local_model is True


def test_no_config_anywhere_creates_one_in_first_location(
    homes: tuple[Path, Path],
) -> None:
    xdg, home = homes
    assert load_config() == Config()
    assert xdg.exists()
    assert not home.exists()


def test_settings_from_config() -> None:
    config = parse_config(
        "[assistant]\nmax_rounds = 3\n[ollama]\nmodel = 'm:1b'\nthink = 'off'\n"
    )
    assert Settings.from_config(config) == Settings(
        model="m:1b", num_ctx=Config().ollama.num_ctx, think=False, max_rounds=3
    )


def test_local_model_picks_ollama_conversation() -> None:
    conversation = new_conversation(Config())
    assert isinstance(conversation, Conversation)


def test_remote_model_picks_claude_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vpop.assistant.claude_harness import ClaudeConversation

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = parse_config("[assistant]\nlocal_model = false\nmax_rounds = 5\n")
    conversation = new_conversation(config)
    assert isinstance(conversation, ClaudeConversation)
    assert conversation.max_rounds == 5
    assert conversation.settings == ClaudeConfig()


def test_set_setting_replaces_value_keeping_comments(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    set_setting(path, "assistant", "local_model", False)
    text = path.read_text()
    assert "local_model = false" in text
    assert "# true: a local Ollama model" in text
    assert load_config(path).assistant.local_model is False


def test_set_setting_adds_missing_key_and_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[claude]\nmodel = "claude-opus-5-5"\n')
    set_setting(path, "claude", "effort", "high")
    set_setting(path, "assistant", "local_model", False)
    config = load_config(path)
    assert config.claude.effort == "high"
    assert config.claude.model == "claude-opus-5-5"
    assert config.assistant.local_model is False


def test_set_setting_refuses_bad_value(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    before = path.read_text()
    with pytest.raises(ConfigError):
        set_setting(path, "claude", "effort", "extreme")
    assert path.read_text() == before


def test_set_setting_keeps_file_permissions(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    path.chmod(0o600)
    set_setting(path, "claude", "effort", "high")
    assert path.stat().st_mode & 0o777 == 0o600
