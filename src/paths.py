"""Project and secrets paths."""

from __future__ import annotations

from pathlib import Path

from config import config_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = REPO_ROOT / "secrets"
OAUTH_CREDENTIALS_PATH = SECRETS_DIR / "oauth-client-credentials.json"
OAUTH_TOKEN_PATH = SECRETS_DIR / "oauth-token.json"
REMOTE_DATA_LOCATIONS_ENV = SECRETS_DIR / "remote-data-locations.env"


def anthropic_env_path() -> Path:
    """Optional `anthropic.env` beside the config file, holding `ANTHROPIC_API_KEY=...`."""
    return config_dir() / "anthropic.env"
