"""Project and secrets path constants."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = REPO_ROOT / "secrets"
OAUTH_CREDENTIALS_PATH = SECRETS_DIR / "oauth-client-credentials.json"
REMOTE_DATA_LOCATIONS_ENV = SECRETS_DIR / "remote-data-locations.env"
