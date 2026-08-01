"""Shared local data directory helpers for vpop."""

from __future__ import annotations

import os
from pathlib import Path


def vpop_data_dir() -> Path:
    """Persistent user-data root (`$XDG_DATA_HOME/vpop`, else `~/.local/share/vpop`)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "vpop"


def android_messages_raw_dir() -> Path:
    """Local directory for downloaded Android Messages XML dumps."""
    path = vpop_data_dir() / "raw" / "android-messages"
    path.mkdir(parents=True, exist_ok=True)
    return path
