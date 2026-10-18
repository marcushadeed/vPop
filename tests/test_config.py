"""Tests for reading and generating the config file, and the paths it lives at."""

from pathlib import Path

import pytest

from vpop.config import (
    AndroidMessagesConfig,
    Config,
    ConfigError,
    WhatsAppConfig,
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
    config = parse_config("[whatsapp]\nenabled = true\n")
    assert config.whatsapp.enabled is True
    assert config.whatsapp.drive_folder_id == ""
    assert config.android_messages == AndroidMessagesConfig()


def test_generated_file_round_trips(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "vpop" / "config.toml")
    text = path.read_text()
    assert "[android_messages]\n# Google Drive folder that SMS Backup" in text
    assert "[whatsapp]" in text
    assert load_config(path) == Config()


def test_render_round_trips_non_defaults() -> None:
    config = parse_config('[android_messages]\ndrive_folder_id = "abc123"\n')
    assert config.android_messages == AndroidMessagesConfig(drive_folder_id="abc123")
    assert parse_config(render_config(config)) == config


def test_whatsapp_section() -> None:
    assert Config().whatsapp == WhatsAppConfig(enabled=False, drive_folder_id="")
    config = parse_config('[whatsapp]\nenabled = true\ndrive_folder_id = "f1"\n')
    assert config.whatsapp == WhatsAppConfig(enabled=True, drive_folder_id="f1")
    assert "[whatsapp]" in render_config(Config())
    assert parse_config(render_config(config)) == config


def test_removed_sections_load_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    config = parse_config(
        "[assistant]\nlocal_model = false\n[google_calendar]\nenabled = true\n"
        "[whatsapp]\nenabled = true\n"
    )
    assert config == Config(whatsapp=WhatsAppConfig(enabled=True))
    assert "[assistant], [google_calendar] section(s) are no longer used" in caplog.text


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[whatsapp]\nenabled = true\n")
    with pytest.raises(FileExistsError):
        write_default_config(path)
    assert load_config(path).whatsapp.enabled is True
    write_default_config(path, force=True)
    assert load_config(path) == Config()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[whatsapp]\nenabeld = true\n", "unknown setting"),
        ("[whatsap]\nenabled = true\n", "unknown section"),
        ("[whatsapp]\nenabled = 'no'\n", "whatsapp.enabled must be bool, not str"),
        ("[android_messages]\ndrive_folder_id = 1\n", "must be str, not int"),
        ("[whatsapp]\nenabled = 1\n", "must be bool, not int"),
        ("whatsapp = 3\n", "must be a table"),
        ("[whatsapp\n", "invalid TOML"),
    ],
)
def test_bad_config_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(text)


def test_load_error_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[whatsapp]\nenabled = 1\n")
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
    home.write_text("[whatsapp]\nenabled = true\n")
    assert config_path() == home
    assert load_config().whatsapp.enabled is True
    assert not xdg.exists()


def test_existing_xdg_config_wins(homes: tuple[Path, Path]) -> None:
    for path, enabled in zip(homes, ("true", "false"), strict=True):
        path.parent.mkdir(parents=True)
        path.write_text(f"[whatsapp]\nenabled = {enabled}\n")
    assert load_config().whatsapp.enabled is True


def test_no_config_anywhere_creates_one_in_first_location(
    homes: tuple[Path, Path],
) -> None:
    xdg, home = homes
    assert load_config() == Config()
    assert xdg.exists()
    assert not home.exists()


def test_set_setting_replaces_value_keeping_comments(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    set_setting(path, "whatsapp", "enabled", True)
    text = path.read_text()
    assert "enabled = true" in text
    assert "# Sync WhatsApp's end-to-end encrypted" in text
    assert load_config(path).whatsapp.enabled is True


def test_set_setting_adds_missing_key_and_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[whatsapp]\nenabled = true\n")
    set_setting(path, "whatsapp", "drive_folder_id", "w1")
    set_setting(path, "android_messages", "drive_folder_id", "a1")
    config = load_config(path)
    assert config.whatsapp == WhatsAppConfig(enabled=True, drive_folder_id="w1")
    assert config.android_messages.drive_folder_id == "a1"


def test_set_setting_refuses_bad_value(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    before = path.read_text()
    with pytest.raises(ConfigError):
        set_setting(path, "whatsapp", "enabled", "yes")
    assert path.read_text() == before


def test_set_setting_keeps_file_permissions(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    path.chmod(0o600)
    set_setting(path, "whatsapp", "enabled", True)
    assert path.stat().st_mode & 0o777 == 0o600
