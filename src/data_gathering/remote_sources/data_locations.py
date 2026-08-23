"""Remote data location IDs from secrets/remote-data-locations.env."""

from __future__ import annotations

import os
from pathlib import Path

from paths import REMOTE_DATA_LOCATIONS_ENV


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs into os.environ (existing vars win)."""
    if not path.exists():
        raise FileNotFoundError(
            f"Missing remote data locations at {path}. "
            "Create secrets/remote-data-locations.env with MESSAGES_BACKUP_FOLDER_ID=..."
        )
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


_load_env_file(REMOTE_DATA_LOCATIONS_ENV)

_raw_messages_backup_folder_id = os.environ.get("MESSAGES_BACKUP_FOLDER_ID")
if not _raw_messages_backup_folder_id:
    raise ValueError(
        "MESSAGES_BACKUP_FOLDER_ID is not set in "
        f"{REMOTE_DATA_LOCATIONS_ENV} or the environment."
    )
MESSAGES_BACKUP_FOLDER_ID: str = _raw_messages_backup_folder_id
