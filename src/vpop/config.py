"""
Read and generate the vpop config file (`$XDG_CONFIG_HOME/vpop/config.toml`).

Every setting has its default here. A missing file, section or key falls back to that
default, so the file only needs the settings you want to change. `vpop config init` writes a
file listing every setting at its default, and `load_config` does the same when no file
exists yet.
"""

import logging
import tomllib
from dataclasses import Field, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

from vpop.fsutil import atomic_write
from vpop.paths import config_path

log = logging.getLogger(__name__)

Think = Literal["on", "off", "default"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
THINK_CHOICES: tuple[str, ...] = get_args(Think)
EFFORT_CHOICES: tuple[str, ...] = get_args(Effort)


def setting(default: Any, doc: str, minimum: int | None = None) -> Any:
    """
    A setting's default, the comment written above it in a generated file, and optionally
    the smallest value it accepts.
    """
    # Called in class bodies, where dataclass picks the field up.
    return field(  # pylint: disable=invalid-field-call
        default=default, metadata={"doc": doc, "minimum": minimum}
    )


@dataclass(frozen=True)
class AssistantConfig:
    """The `[assistant]` section: which model answers questions."""

    local_model: bool = setting(
        True,
        "true: a local Ollama model, nothing leaves the machine. "
        "false: Claude over the Anthropic API.",
    )
    max_rounds: int = setting(
        12, "Tool-call rounds allowed per question before giving up.", minimum=1
    )


@dataclass(frozen=True)
class OllamaConfig:
    """The `[ollama]` section, used when `local_model = true`."""

    model: str = setting("qwen3:8b", "Any Ollama model with tool support.")
    num_ctx: int = setting(
        16384,
        "Context window in tokens. Ollama's default is small, and a few tool results "
        "fill it quickly.",
        minimum=2048,
    )
    think: Think = setting(
        "default",
        'Whether thinking models reason before each step: "on", "off" or "default" (the '
        "model's own). Thinking is more accurate but much slower on CPU.",
    )


@dataclass(frozen=True)
class ClaudeConfig:
    """The `[claude]` section, used when `local_model = false`."""

    model: str = setting(
        "claude-sonnet-5-5",
        'Claude model id, e.g. "claude-sonnet-5-5" or "claude-opus-5-5" (smarter, 2x '
        "the price).",
    )
    max_tokens: int = setting(16000, "Output token cap per model response.", minimum=1)
    effort: Effort = setting(
        "medium", 'Reasoning effort: "low", "medium", "high", "xhigh" or "max".'
    )


@dataclass(frozen=True)
class AndroidMessagesConfig:
    """The `[android_messages]` section: where `vpop sync` finds the message backups."""

    drive_folder_id: str = setting(
        "",
        "Google Drive folder that SMS Backup & Restore uploads to: the id at the end of "
        "the folder's URL (https://drive.google.com/drive/folders/<id>).",
    )


@dataclass(frozen=True)
class GoogleCalendarConfig:
    """The `[google_calendar]` section: reading Google Calendar while answering."""

    enabled: bool = setting(
        False,
        "Let vPop read your Google Calendar while answering: read-only and live, nothing "
        "is stored. Uses the same Google OAuth client as the message backups; run "
        "`vpop auth google` after turning it on.",
    )
    exclude_calendars: tuple[str, ...] = setting(
        (),
        'Calendars to leave out, by name or id, e.g. ["Holidays in United States"].',
    )


@dataclass(frozen=True)
class Config:
    """The whole config file; each field is a `[section]`."""

    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    android_messages: AndroidMessagesConfig = field(
        default_factory=AndroidMessagesConfig
    )
    google_calendar: GoogleCalendarConfig = field(default_factory=GoogleCalendarConfig)


class ConfigError(ValueError):
    """The config file can't be parsed or has a bad setting."""


def type_name(hint: Any) -> str:
    """How a setting's type is named in an error message."""
    if get_origin(hint) is Literal:
        return "one of " + ", ".join(get_args(hint))
    if get_origin(hint) is tuple:
        return "a list of strings"
    return str(hint.__name__)


def check_value(name: str, spec: Field[Any], hint: Any, value: Any) -> Any:
    """
    Return `value` if it suits the setting (an int is fine for a float, a list of strings
    becomes a tuple), else raise.
    """
    if get_origin(hint) is Literal:
        if value not in get_args(hint):
            raise ConfigError(f"{name} must be {type_name(hint)}, not {value!r}")
        return value
    if get_origin(hint) is tuple:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{name} must be {type_name(hint)}, not {value!r}")
        return tuple(value)
    # bool is a subclass of int, so `num_ctx = true` would otherwise pass.
    if isinstance(value, bool) and hint is not bool:
        ok = False
    elif hint is float:
        ok = isinstance(value, int | float)
        value = float(value) if ok else value
    else:
        ok = isinstance(value, hint)
    if not ok:
        raise ConfigError(
            f"{name} must be {type_name(hint)}, not {type(value).__name__}"
        )
    minimum = spec.metadata.get("minimum")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, not {value}")
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
        key: check_value(f"{name}.{key}", specs[key], hints[key], value)
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
    unknown = set(data) - sections
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    hints = get_type_hints(Config)
    return Config(
        **{name: build_section(hints[name], name, data[name]) for name in data}
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
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, tuple | list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
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
