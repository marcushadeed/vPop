"""Entry point for the vpop CLI."""

import argparse
import logging
import os
import shlex
import subprocess
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

    config = commands.add_parser("config", help="write, show or edit the config file")
    config_actions = config.add_subparsers(dest="config_command", required=True)
    init = config_actions.add_parser("init", help="write a default config file")
    init.add_argument(
        "--force", action="store_true", help="overwrite an existing config file"
    )
    config_actions.add_parser("show", help="print the settings in effect")
    config_actions.add_parser(
        "edit",
        help="open the config file in $VISUAL or $EDITOR (vi if neither is set)",
    )
    config_actions.add_parser("path", help="print where the config file is")

    auth = commands.add_parser("auth", help="set up Google access")
    auth_actions = auth.add_subparsers(dest="auth_command", required=True)
    auth_actions.add_parser(
        "google",
        help="give vPop read-only access to the Google data the enabled sources read "
        "(opens a browser)",
    )

    schedule = commands.add_parser(
        "schedule", help="keep the data fresh: run vpop sync on a systemd user timer"
    )
    schedule_actions = schedule.add_subparsers(dest="schedule_command", required=True)
    install = schedule_actions.add_parser("install", help="write and start the timer")
    install.add_argument(
        "--every",
        default="hourly",
        metavar="WHEN",
        help="a systemd calendar time, e.g. hourly, daily or *:0/30 (default: hourly)",
    )
    schedule_actions.add_parser("remove", help="stop the timer and delete it")
    schedule_actions.add_parser(
        "status", help="show the next run and how each source's last sync went"
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
        "schedule": schedule_command,
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
        # Asking Google for consent opens a browser, so only when someone is at a terminal;
        # run by the timer, an expired token is an error saying to run `vpop auth google`.
        sync(
            config,
            rebuild=args.rebuild,
            offline=args.offline,
            interactive=sys.stdin.isatty(),
        )
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


def schedule_command(args: argparse.Namespace) -> None:
    """Install, remove or show the sync timer."""
    from vpop import schedule  # pylint: disable=import-outside-toplevel

    try:
        if args.schedule_command == "install":
            directory = schedule.install(args.every)
            print(f"vpop sync now runs {args.every} (units in {directory})")
            print("check it with: vpop schedule status")
        elif args.schedule_command == "remove":
            removed = schedule.remove()
            print("removed the sync timer" if removed else "no sync timer to remove")
        else:
            print(schedule.status())
    except schedule.ScheduleError as exc:
        raise SystemExit(f"error: {exc}") from exc


def mcp_command(_: argparse.Namespace) -> None:
    """Serve the tools over stdio until Claude closes the connection."""
    from vpop.tools.server import serve  # pylint: disable=import-outside-toplevel

    serve(load_or_exit())


def edit_config() -> None:
    """Open the config file in the user's editor (writing it first if needed), then check it."""
    path = config_path()
    if not path.exists():
        write_default_config(path)
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    try:
        result = subprocess.run([*editor, str(path)], check=False)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"error: editor not found: {editor[0]} (set $VISUAL or $EDITOR)"
        ) from exc
    if result.returncode != 0:
        raise SystemExit(f"error: {editor[0]} exited with status {result.returncode}")
    # Report a bad edit now, rather than on the timer's next sync.
    load_or_exit()


def config_command(args: argparse.Namespace) -> None:
    """Write, edit or locate the config file, or print the settings in effect."""
    if args.config_command == "init":
        try:
            path = write_default_config(force=args.force)
        except FileExistsError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"wrote {path}")
    elif args.config_command == "edit":
        edit_config()
    elif args.config_command == "path":
        print(config_path())
    else:
        config = load_or_exit()
        print(f"# from {config_path()}")
        print(render_config(config, comments=False), end="")


if __name__ == "__main__":
    main()
