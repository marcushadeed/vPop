"""
Read and generate the vpop config file (`$XDG_CONFIG_HOME/vpop/config.toml`).

Every setting has its default here. A missing file, section or key falls back to that
default, so the file only needs the settings you want to change. `vpop config init` writes a
file listing every setting at its default, and `load_config` does the same when no file
exists yet.
"""

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

from vpop.fsutil import atomic_write
from vpop.paths import config_path

log = logging.getLogger(__name__)

# Sections of older versions, for the chat and Google Calendar. A file that still has them
# loads, with a warning to delete them.
REMOVED_SECTIONS = ("assistant", "ollama", "claude", "google_calendar")


def setting(default: Any, doc: str) -> Any:
    """A setting's default, and the comment written above it in a generated file."""
    # Called in class bodies, where dataclass picks the field up.
    return field(default=default, metadata={"doc": doc})  # pylint: disable=invalid-field-call


@dataclass(frozen=True)
class AndroidMessagesConfig:
    """The `[android_messages]` section: where `vpop sync` finds the message backups."""

    drive_folder_id: str = setting(
        "",
        "Google Drive folder that SMS Backup & Restore uploads to: the id at the end of "
        "the folder's URL (https://drive.google.com/drive/folders/<id>).",
    )


@dataclass(frozen=True)
class WhatsAppConfig:
    """The `[whatsapp]` section: where `vpop sync` finds the WhatsApp backups."""

    enabled: bool = setting(
        False,
        "Sync WhatsApp's end-to-end encrypted Android backups (msgstore*.crypt15) from "
        "Google Drive. The 64-hex-digit backup key goes in whatsapp-key beside this file, "
        "readable only by you.",
    )
    drive_folder_id: str = setting(
        "",
        "Google Drive folder the WhatsApp backups are uploaded to: the id at the end of "
        "the folder's URL (https://drive.google.com/drive/folders/<id>).",
    )


@dataclass(frozen=True)
class Config:
    """The whole config file; each field is a `[section]`."""

    android_messages: AndroidMessagesConfig = field(
        default_factory=AndroidMessagesConfig
    )
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)


class ConfigError(ValueError):
    """The config file can't be parsed or has a bad setting."""


def check_value(name: str, hint: type, value: Any) -> Any:
    """Return `value` if it suits the setting's type, else raise."""
    if not isinstance(value, hint):
        raise ConfigError(f"{name} must be {hint.__name__}, not {type(value).__name__}")
    return value


def build_section(cls: type, name: str, values: Any) -> Any:
    """Make a section from its table, rejecting unknown keys and bad values."""
    if not isinstance(values, dict):
        raise ConfigError(f"[{name}] must be a table")
    hints = get_type_hints(cls)
    specs = {f.name: f for f in fields(cls)}
    unknown = set(values) - set(specs)
    if unknown:
        raise ConfigError(
            f"unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}"
        )
    checked = {
        key: check_value(f"{name}.{key}", hints[key], value)
        for key, value in values.items()
    }
    return cls(**checked)


def parse_config(text: str) -> Config:
    """Parse config TOML; anything it leaves out gets its default."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    sections = {f.name for f in fields(Config)}
    removed = sorted(set(data) & set(REMOVED_SECTIONS))
    if removed:
        log.warning(
            "the config's %s section(s) are no longer used; delete them",
            ", ".join(f"[{name}]" for name in removed),
        )
    unknown = set(data) - sections - set(REMOVED_SECTIONS)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    hints = get_type_hints(Config)
    return Config(
        **{
            name: build_section(hints[name], name, values)
            for name, values in data.items()
            if name in sections
        }
    )


def load_config(path: Path | None = None) -> Config:
    """
    Read the config file. If it doesn't exist, write one with the defaults and return
    those; if it can't be written, warn and return the defaults anyway.
    """
    path = path or config_path()
    if not path.exists():
        try:
            write_default_config(path)
        except OSError as exc:
            log.warning("couldn't create %s: %s", path, exc)
        else:
            log.info("created default config at %s", path)
        return Config()
    try:
        return parse_config(path.read_text())
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def toml_value(value: Any) -> str:
    """A TOML literal for a setting value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"can't write {type(value).__name__} to TOML")


def render_config(config: Config, comments: bool = True) -> str:
    """Write a config as TOML, with each setting's description as a comment above it."""
    lines: list[str] = []
    for section in fields(Config):
        values = getattr(config, section.name)
        if lines:
            lines.append("")
        lines.append(f"[{section.name}]")
        for spec in fields(values):
            if comments and spec.metadata.get("doc"):
                lines.append(f"# {spec.metadata['doc']}")
            lines.append(f"{spec.name} = {toml_value(getattr(values, spec.name))}")
    return "\n".join(lines) + "\n"


def set_setting(path: Path, section: str, key: str, value: Any) -> None:
    """
    Change one setting in the config file, keeping its comments and layout. Adds the key
    (and its section) if the file leaves them out.
    """
    lines = path.read_text().splitlines() if path.exists() else []
    line = f"{key} = {toml_value(value)}"
    header = f"[{section}]"
    start = next((i for i, text in enumerate(lines) if text.strip() == header), None)
    if start is None:
        lines += ([""] if lines else []) + [header, line]
    else:
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("[")
            ),
            len(lines),
        )
        index = next(
            (
                i
                for i in range(start + 1, end)
                if lines[i].split("=", 1)[0].strip() == key
            ),
            None,
        )
        if index is None:
            lines.insert(start + 1, line)
        else:
            lines[index] = line
    text = "\n".join(lines) + "\n"
    parse_config(text)  # Refuse to write a file that won't load.
    atomic_write(path, text)


def write_default_config(path: Path | None = None, force: bool = False) -> Path:
    """Write a config file with every setting at its default and return its path."""
    path = path or config_path()
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (pass --force to overwrite)")
    atomic_write(path, render_config(Config()))
    return path
