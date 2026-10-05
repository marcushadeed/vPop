"""Remote data location IDs from `remote-data-locations.env` beside the config file."""

from __future__ import annotations

import os
from pathlib import Path

from vpop.fsutil import read_env_file
from vpop.paths import remote_data_locations_env


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs into os.environ (existing vars win)."""
    if not path.exists():
        raise FileNotFoundError(
            f"Missing remote data locations at {path}. "
            "Create it with MESSAGES_BACKUP_FOLDER_ID=..."
        )
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


_load_env_file(remote_data_locations_env())

_raw_messages_backup_folder_id = os.environ.get("MESSAGES_BACKUP_FOLDER_ID")
if not _raw_messages_backup_folder_id:
    raise ValueError(
        "MESSAGES_BACKUP_FOLDER_ID is not set in "
        f"{remote_data_locations_env()} or the environment."
    )
MESSAGES_BACKUP_FOLDER_ID: str = _raw_messages_backup_folder_id
