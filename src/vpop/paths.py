"""
Every file location vpop uses, in one place.

Settings and secrets live in the config directory (`$XDG_CONFIG_HOME/vpop`, else
`~/.config/vpop`); downloaded backups, the database and benchmark results live in the data
directory (`$XDG_DATA_HOME/vpop`, else `~/.local/share/vpop`). These functions only compute
paths: whoever writes a file creates its directory.
"""

import os
from pathlib import Path


def xdg_base(var: str, fallback: Path) -> Path:
    """An XDG base directory: the variable when it's set to an absolute path, else `fallback`."""
    value = os.environ.get(var)
    return Path(value) if value and Path(value).is_absolute() else fallback


def config_paths() -> list[Path]:
    """
    Where the config file is looked for, in order: `$XDG_CONFIG_HOME/vpop/config.toml`
    (when the variable is set), then `~/.config/vpop/config.toml`.
    """
    home = Path.home() / ".config"
    bases = [xdg_base("XDG_CONFIG_HOME", home)]
    if bases[0] != home:
        bases.append(home)
    return [base / "vpop" / "config.toml" for base in bases]


def config_path() -> Path:
    """The first config file that exists, else where a new one goes (the first location)."""
    paths = config_paths()
    return next((path for path in paths if path.exists()), paths[0])


def config_dir() -> Path:
    """The directory holding the config file, and the secrets kept beside it."""
    return config_path().parent


def oauth_credentials_path() -> Path:
    """Google OAuth client credentials (downloaded from Cloud Console), beside the config."""
    return config_dir() / "oauth-client-credentials.json"


def oauth_token_path() -> Path:
    """Cached Google OAuth token, written after the first consent, beside the config."""
    return config_dir() / "oauth-token.json"


def anthropic_env_path() -> Path:
    """Optional `anthropic.env` beside the config file, holding `ANTHROPIC_API_KEY=...`."""
    return config_dir() / "anthropic.env"


def legacy_remote_locations_path() -> Path:
    """Where older versions kept the Drive folder id; it now lives in the config file."""
    return config_dir() / "remote-data-locations.env"


def data_dir() -> Path:
    """Persistent user-data root (`$XDG_DATA_HOME/vpop`, else `~/.local/share/vpop`)."""
    return xdg_base("XDG_DATA_HOME", Path.home() / ".local" / "share") / "vpop"


def db_path() -> Path:
    """The SQLite database every source is imported into."""
    return data_dir() / "vpop.db"


def android_messages_raw_dir() -> Path:
    """Downloaded Android Messages XML backups, kept as the source of truth for the DB."""
    return data_dir() / "raw" / "android-messages"


def bench_results_dir() -> Path:
    """Where `vpop bench` writes its result files by default."""
    return data_dir() / "benchmarks"
