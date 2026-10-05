"""Project and secrets paths."""

from __future__ import annotations

from pathlib import Path

from config import config_dir

REPO_ROOT = Path(__file__).resolve().parents[1]


def oauth_credentials_path() -> Path:
    """Google OAuth client credentials (downloaded from Cloud Console), beside the config."""
    return config_dir() / "oauth-client-credentials.json"


def oauth_token_path() -> Path:
    """Cached Google OAuth token, written after the first consent, beside the config."""
    return config_dir() / "oauth-token.json"


def remote_data_locations_env() -> Path:
    """`remote-data-locations.env` beside the config, holding Drive folder IDs."""
    return config_dir() / "remote-data-locations.env"


def anthropic_env_path() -> Path:
    """Optional `anthropic.env` beside the config file, holding `ANTHROPIC_API_KEY=...`."""
    return config_dir() / "anthropic.env"
