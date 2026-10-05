"""
`vpop auth`: set up, check and remove the Anthropic credentials Claude answers with.

`login` saves a pasted API key to `anthropic.env` beside the config file (after checking it
with a free API call), or hands off to the Anthropic CLI's `ant auth login` when that's
installed. `status` says which credential wins and whether it works; `logout` removes the
saved key.
"""

import getpass
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import anthropic

from assistant.claude_harness import (
    API_KEYS_URL,
    credential_source,
    load_anthropic_env,
    make_client,
)
from assistant.harness import AuthError
from config import Config, config_path, set_setting
from paths import anthropic_env_path

KEY_VAR = "ANTHROPIC_API_KEY"
EXPORTED_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def mask(secret: str) -> str:
    """Enough of a key to tell keys apart: `sk-ant-…a1b2`."""
    return f"{secret[:7]}…{secret[-4:]}" if len(secret) > 15 else "…"


def read_env_file(path: Path) -> dict[str, str]:
    """The KEY=VALUE pairs in an env file, the way `load_anthropic_env` reads them."""
    if not path.exists():
        return {}
    pairs: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip().strip("'\"")
    return pairs


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read, tightening an existing file's permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)
    path.chmod(0o600)


def save_key(path: Path, key: str) -> None:
    """Set `ANTHROPIC_API_KEY` in the env file, keeping its other lines."""
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines if line.partition("=")[0].strip() != KEY_VAR]
    write_private(path, "\n".join(kept + [f"{KEY_VAR}={key}"]) + "\n")


def remove_key(path: Path) -> bool:
    """
    Drop `ANTHROPIC_API_KEY` from the env file, deleting the file if nothing else is left.
    Returns whether there was a key to remove.
    """
    if KEY_VAR not in read_env_file(path):
        return False
    kept = [
        line
        for line in path.read_text().splitlines()
        if line.partition("=")[0].strip() != KEY_VAR
    ]
    if any(line.strip() and not line.strip().startswith("#") for line in kept):
        write_private(path, "\n".join(kept) + "\n")
    else:
        path.unlink()
    return True


def readable_by_others(path: Path) -> bool:
    """Whether the group or other users can read or write the file."""
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def check_client(client: anthropic.Anthropic) -> str | None:
    """
    Try the credentials with a free call (listing models). Returns None if they work,
    else why not.
    """
    try:
        client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        return body.get("error", {}).get("message") or exc.message
    except anthropic.APIConnectionError as exc:
        return f"couldn't reach the API ({exc.message})"
    return None


def check_key(key: str) -> str | None:
    """`check_client` for a key that isn't saved anywhere yet."""
    return check_client(anthropic.Anthropic(api_key=key))


def confirm(question: str, default: bool = True) -> bool:
    """Ask a yes/no question; Enter takes the default."""
    choices = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {choices} ").strip().lower()
    except EOFError:
        print()
        return False
    return default if not answer else answer.startswith("y")


def exported_credential() -> str | None:
    """A credential variable set in the shell, which outranks the key file and profiles."""
    return next((var for var in EXPORTED_VARS if os.environ.get(var)), None)


def login(config: Config, check: Callable[[str], str | None] = check_key) -> bool:
    """
    Interactively set up credentials. Returns whether it ended with working ones. `check`
    is how a pasted key is tested (swapped out in tests).
    """
    if not sys.stdin.isatty():
        raise SystemExit("vpop auth login needs a terminal to read the key from")
    exported = exported_credential()
    if exported:
        print(
            f"warning: {exported} is set in your shell, and it wins over anything saved "
            "here. Unset it to use what you set up now.",
            file=sys.stderr,
        )
    if shutil.which("ant") and not confirm(
        "Paste an API key? (No logs in through the browser with `ant auth login`)"
    ):
        done = login_with_cli()
    else:
        done = login_with_key(check)
    if (
        done
        and config.assistant.local_model
        and confirm(
            "The config uses a local model. Answer with Claude instead? "
            "(sets local_model = false)"
        )
    ):
        set_setting(config_path(), "assistant", "local_model", False)
        print(f"updated {config_path()}")
    return done


def login_with_key(check: Callable[[str], str | None]) -> bool:
    """Read a key without echoing it, test it, and save it. Empty input gives up."""
    path = anthropic_env_path()
    print(f"Create a key at {API_KEYS_URL}, then paste it here (it won't be shown).")
    while True:
        try:
            key = getpass.getpass("API key (Enter to cancel): ").strip()
        except EOFError:
            key = ""
        if not key:
            print("cancelled")
            return False
        problem = check(key)
        if problem is None:
            break
        print(f"That key didn't work: {problem}")
    save_key(path, key)
    print(f"saved {mask(key)} to {path}")
    return True


def login_with_cli() -> bool:
    """Run `ant auth login`, then make sure a saved key won't hide the new profile."""
    if subprocess.run(["ant", "auth", "login"], check=False).returncode != 0:
        print("ant auth login didn't finish")
        return False
    path = anthropic_env_path()
    # A key in the file is loaded as ANTHROPIC_API_KEY, which outranks every profile.
    if KEY_VAR in read_env_file(path) and confirm(
        f"{path} has a key, which would be used instead of the profile. Remove it?"
    ):
        remove_key(path)
        print(f"removed the key from {path}")
    return True


def status(config: Config) -> int:
    """Print which credential `vpop ask` would use and whether it works. Returns an exit code."""
    path = anthropic_env_path()
    mode = "a local Ollama model" if config.assistant.local_model else "Claude"
    print(f"vpop ask uses {mode} (local_model in {config_path()})")
    if path.exists() and readable_by_others(path):
        print(f"warning: other users can read {path}; run `chmod 600 {path}`")
    if KEY_VAR in read_env_file(path) and os.environ.get(KEY_VAR):
        print(f"note: the {KEY_VAR} in your shell hides the key saved in {path}")
    try:
        client, source = make_client()
    except AuthError as exc:
        print(f"credentials: none\n{exc}")
        return 1
    secret = client.api_key or client.auth_token
    print(f"credentials: {source}" + (f", {mask(secret)}" if secret else ""))
    problem = check_client(client)
    if problem is None:
        print("check: ok")
        return 0
    print(f"check: failed: {problem}")
    return 1


def logout() -> None:
    """Remove the saved key, and say what else is still providing credentials."""
    path = anthropic_env_path()
    if remove_key(path):
        print(f"removed the key from {path}")
    else:
        print(f"no key saved in {path}")
    exported = exported_credential()
    if exported:
        print(f"{exported} is still set in your shell")
        return
    load_anthropic_env()
    try:
        profile = credential_source(anthropic.Anthropic(), set())
    except anthropic.CredentialsError:
        profile = None
    if profile:
        print(
            "an Anthropic CLI profile is still logged in; `ant auth logout` removes it"
        )


def offer_login(config: Config) -> bool:
    """
    After a missing-credentials error: at a terminal, offer to run `login` right away.
    Returns whether credentials were set up.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if not confirm("Set up Anthropic credentials now?"):
        return False
    return login(config)
