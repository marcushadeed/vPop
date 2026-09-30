"""Tests for reading and generating the config file."""

from pathlib import Path

import pytest

from assistant.harness import Conversation, Settings, new_conversation
from config import (
    ClaudeConfig,
    Config,
    ConfigError,
    config_path,
    load_config,
    parse_config,
    render_config,
    write_default_config,
)


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nope.toml") == Config()


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
        ("[ollama]\nthink = 'maybe'\n", "ollama.think must be one of"),
        ("[claude]\neffort = 'huge'\n", "claude.effort must be one of"),
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


def test_config_path_follows_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "vpop" / "config.toml"


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
    from assistant.claude_harness import ClaudeConversation

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = parse_config("[assistant]\nlocal_model = false\nmax_rounds = 5\n")
    conversation = new_conversation(config)
    assert isinstance(conversation, ClaudeConversation)
    assert conversation.max_rounds == 5
    assert conversation.settings == ClaudeConfig()
