"""
Fixtures for every test. Each test runs with its own empty home, config and data
directories, no Anthropic credentials and a fixed time zone, so nothing reads or writes the
real `~/.config/vpop` or `~/.local/share/vpop`.
"""

import logging
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ANTHROPIC_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point HOME and the XDG directories at temp dirs; clear credentials; pin the time zone."""
    for var in ANTHROPIC_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()
    # `cli.setup_logging` reconfigures the package logger; put it back for the next test.
    logger = logging.getLogger("vpop")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


@pytest.fixture
def no_credentials() -> Path:
    """Where `anthropic.env` goes in the isolated config directory (it doesn't exist)."""
    return Path.home().parent / "xdg" / "vpop" / "anthropic.env"
