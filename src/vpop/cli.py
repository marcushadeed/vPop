"""Entry point for the vpop CLI."""

import argparse
import logging
import sys

from vpop.config import (
    Config,
    ConfigError,
    load_config,
    render_config,
    write_default_config,
)
from vpop.paths import config_path


class Formatter(logging.Formatter):
    """Plain messages, with `warning:` / `error:` in front of the serious ones."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.levelno >= logging.WARNING:
            return f"{record.levelname.lower()}: {message}"
        return message


def setup_logging(verbosity: int) -> None:
    """Log to stderr: warnings with `-q`, progress and tool calls by default, all with `-v`."""
    level = {-1: logging.WARNING, 0: logging.INFO}.get(verbosity, logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(Formatter("%(message)s"))
    root = logging.getLogger("vpop")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="vpop",
        description="Sync your messages into a local database, and serve them to Claude "
        "through read-only tools.",
    )
    noise = parser.add_mutually_exclusive_group()
    noise.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        const=1,
        default=0,
        dest="verbosity",
        help="show debug output",
    )
    noise.add_argument(
        "-q",
        "--quiet",
        action="store_const",
        const=-1,
        dest="verbosity",
        help="only show warnings and errors",
    )
    commands = parser.add_subparsers(dest="command", metavar="command")

    sync = commands.add_parser(
        "sync", help="update the database from your message backups"
    )
    sync.add_argument(
        "--rebuild",
        action="store_true",
        help="recreate the database from every downloaded backup",
    )
    sync.add_argument(
        "--offline",
        action="store_true",
        help="don't download new backups, only import what's already downloaded",
    )

    config = commands.add_parser("config", help="write or show the config file")
    config_actions = config.add_subparsers(dest="config_command", required=True)
    init = config_actions.add_parser("init", help="write a default config file")
    init.add_argument(
        "--force", action="store_true", help="overwrite an existing config file"
    )
    config_actions.add_parser("show", help="print the settings in effect")

    auth = commands.add_parser("auth", help="set up Google access")
    auth_actions = auth.add_subparsers(dest="auth_command", required=True)
    auth_actions.add_parser(
        "google",
        help="give vPop read-only access to the Google data the enabled sources read "
        "(opens a browser)",
    )

    commands.add_parser(
        "mcp",
        help="serve the read-only tools to Claude over stdio (Claude Code starts this)",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point for the vpop CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbosity)
    handlers = {
        "sync": sync_command,
        "config": config_command,
        "auth": auth_command,
        "mcp": mcp_command,
    }
    if args.command is None:
        parser.print_help()
        return
    try:
        handlers[args.command](args)
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def load_or_exit() -> Config:
    """The config file's settings, exiting with its error if it's invalid."""
    try:
        return load_config()
    except ConfigError as exc:
        raise SystemExit(f"bad config: {exc}") from exc


def sync_command(args: argparse.Namespace) -> None:
    """Download new backups and import them."""
    # pylint: disable=import-outside-toplevel
    from vpop.db import DatabaseError
    from vpop.sources import SourceError
    from vpop.sources.sync import sync

    config = load_or_exit()
    try:
        sync(config, rebuild=args.rebuild, offline=args.offline)
    except (SourceError, DatabaseError) as exc:
        raise SystemExit(f"error: {exc}") from exc


def auth_command(_: argparse.Namespace) -> None:
    """`vpop auth google`: ask for consent to every scope the enabled sources need."""
    # pylint: disable=import-outside-toplevel
    from vpop.sources.google import auth
    from vpop.sources.registry import google_scopes

    scopes = google_scopes(load_or_exit())
    if not scopes:
        raise SystemExit("error: no enabled source reads Google")
    try:
        granted = auth.authorize(scopes)
    except auth.GoogleSetupError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(f"Google access granted: {auth.names(granted)}")


def mcp_command(_: argparse.Namespace) -> None:
    """Serve the tools over stdio until Claude closes the connection."""
    from vpop.tools.server import serve  # pylint: disable=import-outside-toplevel

    serve(load_or_exit())


def config_command(args: argparse.Namespace) -> None:
    """Write a default config file, or print the settings in effect."""
    if args.config_command == "init":
        try:
            path = write_default_config(force=args.force)
        except FileExistsError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"wrote {path}")
    else:
        config = load_or_exit()
        print(f"# from {config_path()}")
        print(render_config(config, comments=False), end="")


if __name__ == "__main__":
    main()
