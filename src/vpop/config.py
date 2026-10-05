"""
Read and generate the vpop config file (`$XDG_CONFIG_HOME/vpop/config.toml`).

Every setting has its default here. A missing file, section or key falls back to that
default, so the file only needs the settings you want to change. `vpop config init` writes a
file listing every setting at its default, and `load_config` does the same when no file
exists yet.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

from vpop.paths import config_path

log = logging.getLogger(__name__)

THINK_CHOICES = ("on", "off", "default")
EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")


def doc(text: str) -> Any:
    """Attach a comment to a setting; it's written above the key in a generated file."""
    return {"doc": text}


@dataclass(frozen=True)
class AssistantConfig:
    """The `[assistant]` section: which model answers questions."""

    local_model: bool = field(
        default=True,
        metadata=doc(
            "true: a local Ollama model, nothing leaves the machine. "
            "false: Claude over the Anthropic API."
        ),
    )
    max_rounds: int = field(
        default=12,
        metadata=doc("Tool-call rounds allowed per question before giving up."),
    )


@dataclass(frozen=True)
class OllamaConfig:
    """The `[ollama]` section, used when `local_model = true`."""

    model: str = field(
        default="qwen3:8b", metadata=doc("Any Ollama model with tool support.")
    )
    num_ctx: int = field(
        default=16384,
        metadata=doc(
            "Context window in tokens. Ollama's default is small, and a few tool "
            "results fill it quickly."
        ),
    )
    think: str = field(
        default="default",
        metadata=doc(
            'Whether thinking models reason before each step: "on", "off" or '
            '"default" (the model\'s own). Thinking is more accurate but much '
            "slower on CPU."
        ),
    )

    def __post_init__(self) -> None:
        if self.think not in THINK_CHOICES:
            raise ConfigError(
                f"ollama.think must be one of {', '.join(THINK_CHOICES)}, "
                f"not {self.think!r}"
            )


@dataclass(frozen=True)
class ClaudeConfig:
    """The `[claude]` section, used when `local_model = false`."""

    model: str = field(
        default="claude-sonnet-5-5",
        metadata=doc(
            'Claude model id, e.g. "claude-sonnet-5-5" or "claude-opus-5-5" '
            "(smarter, 2x the price)."
        ),
    )
    max_tokens: int = field(
        default=16000, metadata=doc("Output token cap per model response.")
    )
    effort: str = field(
        default="medium",
        metadata=doc('Reasoning effort: "low", "medium", "high", "xhigh" or "max".'),
    )

    def __post_init__(self) -> None:
        if self.effort not in EFFORT_CHOICES:
            raise ConfigError(
                f"claude.effort must be one of {', '.join(EFFORT_CHOICES)}, "
                f"not {self.effort!r}"
            )


@dataclass(frozen=True)
class Config:
    """The whole config file; each field is a `[section]`."""

    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)


class ConfigError(ValueError):
    """The config file can't be parsed or has a bad setting."""


def build_section(cls: type, name: str, values: Any) -> Any:
    """Make a section from its table, rejecting unknown keys and wrongly typed values."""
    if not isinstance(values, dict):
        raise ConfigError(f"[{name}] must be a table")
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(
            f"unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}"
        )
    for key, value in values.items():
        expected = hints[key]
        # bool is a subclass of int, so `num_ctx = true` would otherwise pass.
        if not isinstance(value, expected) or (
            expected is int and isinstance(value, bool)
        ):
            raise ConfigError(
                f"{name}.{key} must be {expected.__name__}, not {type(value).__name__}"
            )
    return cls(**values)


def parse_config(text: str) -> Config:
    """Parse config TOML; anything it leaves out gets its default."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    sections = {f.name: f for f in fields(Config)}
    unknown = set(data) - set(sections)
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
    if isinstance(value, int):
        return str(value)
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
        for setting in fields(values):
            if comments and "doc" in setting.metadata:
                lines.append(f"# {setting.metadata['doc']}")
            value = getattr(values, setting.name)
            lines.append(f"{setting.name} = {toml_value(value)}")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_default_config(path: Path | None = None, force: bool = False) -> Path:
    """Write a config file with every setting at its default and return its path."""
    path = path or config_path()
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (pass --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(Config()))
    return path
