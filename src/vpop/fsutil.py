"""Small file helpers: crash-safe writes and the `KEY=VALUE` env files kept beside the config."""

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """
    Replace `path` with `text` in one step, so a crash leaves the old file or the new one,
    never half of each. `mode` sets the permissions (e.g. `0o600` for secrets); without it,
    the file keeps the existing file's permissions, or gets the umask default.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
        if mode is None:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read or write."""
    atomic_write(path, text, mode=0o600)


def read_env_file(path: Path) -> dict[str, str]:
    """
    The `KEY=VALUE` pairs in an env file, or nothing if it doesn't exist. Blank lines and
    `#` comments are skipped; a value may be wrapped in single or double quotes.
    """
    if not path.exists():
        return {}
    pairs: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        pairs[key.strip()] = value
    return pairs
