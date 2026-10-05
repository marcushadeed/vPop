"""Fixtures shared by the assistant tests."""

from pathlib import Path

import pytest


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    An environment with no Anthropic credentials: no env vars, no CLI profile, an empty
    config directory. Returns where `anthropic.env` goes.
    """
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
    ):
        # Set first so teardown also clears what `load_anthropic_env` writes to os.environ.
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "vpop" / "anthropic.env"
