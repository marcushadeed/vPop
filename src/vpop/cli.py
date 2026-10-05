"""Entry point for the vpop CLI."""

import argparse
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vpop.config import (
    EFFORT_CHOICES,
    THINK_CHOICES,
    Config,
    ConfigError,
    load_config,
    render_config,
    write_default_config,
)
from vpop.paths import config_path

if TYPE_CHECKING:
    from vpop.assistant.conversation import Conversation


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
        description="Ask questions about your own messages. Run it without a command to "
        "chat.",
    )
    # A bare `vpop` is `vpop ask` without a question: the chat.
    parser.set_defaults(question=[])
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

    ask = commands.add_parser(
        "ask", help="ask one question, or chat when none is given (the default)"
    )
    ask.add_argument(
        "question",
        nargs="*",
        help="the question to ask. Leave empty to chat",
    )

    config = commands.add_parser("config", help="write or show the config file")
    config_actions = config.add_subparsers(dest="config_command", required=True)
    init = config_actions.add_parser("init", help="write a default config file")
    init.add_argument(
        "--force", action="store_true", help="overwrite an existing config file"
    )
    config_actions.add_parser("show", help="print the settings in effect")

    auth = commands.add_parser(
        "auth", help="set up the Anthropic credentials and Google access"
    )
    auth_actions = auth.add_subparsers(dest="auth_command", required=True)
    auth_actions.add_parser(
        "login", help="save an API key (or log in with the Anthropic CLI)"
    )
    auth_actions.add_parser(
        "status", help="show which credentials are used and check they work"
    )
    auth_actions.add_parser("logout", help="remove the saved API key")
    auth_actions.add_parser(
        "google",
        help="give vPop read-only access to the Google data the enabled sources read "
        "(opens a browser)",
    )

    bench = commands.add_parser(
        "bench", help="benchmark models on fixed questions over a synthetic database"
    )
    bench.add_argument(
        "--provider",
        choices=["ollama", "claude"],
        default="ollama",
        help="which kind of model to benchmark (default: ollama)",
    )
    bench.add_argument(
        "--model",
        action="append",
        default=[],
        help="model to run (repeatable; defaults to the config's model for the provider)",
    )
    bench.add_argument(
        "--think",
        action="append",
        default=[],
        choices=THINK_CHOICES,
        help="Ollama thinking setting (repeatable)",
    )
    bench.add_argument(
        "--effort",
        action="append",
        default=[],
        choices=EFFORT_CHOICES,
        help="Claude effort level (repeatable)",
    )
    bench.add_argument(
        "--case", action="append", default=[], help="only this case id (repeatable)"
    )
    bench.add_argument(
        "--tag",
        action="append",
        default=[],
        help="only cases with this tag (repeatable)",
    )
    bench.add_argument(
        "--repeat", type=positive_int, default=1, help="times to run each case"
    )
    bench.add_argument("--out", type=Path, help="directory for the results file")
    bench.add_argument(
        "--compare",
        nargs="+",
        metavar="RESULTS",
        type=Path,
        help="compare result files instead of running",
    )
    bench.add_argument(
        "--list", action="store_true", help="list case ids and tags instead of running"
    )
    return parser


def positive_int(text: str) -> int:
    """An argparse type for integers of at least 1."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


def main(argv: list[str] | None = None) -> None:
    """Entry point for the vpop CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbosity)
    handlers = {
        "sync": sync_command,
        "ask": ask_command,
        "config": config_command,
        "auth": auth_command,
        "bench": bench_command,
    }
    try:
        handlers[args.command or "ask"](args)
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


def ask_command(args: argparse.Namespace) -> None:
    """Answer one question, or chat with none."""
    # pylint: disable=import-outside-toplevel
    from vpop.assistant.auth import offer_login
    from vpop.assistant.errors import AuthError, MissingCredentialsError, describe_error
    from vpop.assistant.session import new_conversation

    config = load_or_exit()
    for attempt in range(2):
        try:
            converse(new_conversation(config), config, " ".join(args.question))
            return
        except MissingCredentialsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            if attempt or not offer_login(config):
                raise SystemExit(1) from exc
            config = load_or_exit()
        except AuthError as exc:
            raise SystemExit(f"error: {exc}") from exc
        except Exception as exc:  # pylint: disable=broad-exception-caught
            message = describe_error(exc)
            if message is None:
                raise
            raise SystemExit(f"error: {message}") from exc


def converse(conversation: "Conversation", config: Config, question: str) -> None:
    """
    Answer `question`, or chat without one. At a terminal the chat UI shows tool calls and
    the answer as they arrive; piped, only the answer is printed, and a chat reads plain
    lines from stdin.
    """
    # pylint: disable=import-outside-toplevel
    from vpop.assistant.session import new_conversation, repl

    if question:
        if sys.stdout.isatty():
            from vpop.ui.chat import answer_once

            answer_once(conversation, question)
        else:
            print(conversation.ask(question))
    elif sys.stdin.isatty() and sys.stdout.isatty():
        from vpop.ui.chat import run_chat

        run_chat(conversation, config, lambda: new_conversation(config))
    else:
        repl(conversation)


def auth_command(args: argparse.Namespace) -> None:
    """Set up, check or remove the Anthropic credentials, or grant Google access."""
    # pylint: disable=import-outside-toplevel
    from vpop.assistant import auth
    from vpop.sources.registry import google_scopes

    if args.auth_command == "google":
        google_auth_command(google_scopes(load_or_exit()))
    elif args.auth_command == "login":
        if not auth.login(load_or_exit()):
            raise SystemExit(1)
    elif args.auth_command == "status":
        config = load_or_exit()
        code = auth.status(config)
        from vpop.sources.google.auth import status_line

        print(status_line(google_scopes(config)))
        raise SystemExit(code)
    else:
        auth.logout()


def google_auth_command(scopes: list[str]) -> None:
    """`vpop auth google`: ask for consent to every scope the enabled sources need."""
    from vpop.sources.google import auth  # pylint: disable=import-outside-toplevel

    if not scopes:
        raise SystemExit("error: no enabled source reads Google")
    try:
        granted = auth.authorize(scopes)
    except auth.GoogleSetupError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(f"Google access granted: {auth.names(granted)}")


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


def bench_command(args: argparse.Namespace) -> None:
    """Run, list or compare benchmark cases."""
    # pylint: disable=import-outside-toplevel
    from vpop.benchmark import run
    from vpop.benchmark.cases import CASES

    if args.list:
        for case in CASES:
            print(f"{case.id}  [{', '.join(case.tags)}]  {case.question}")
        return
    if args.compare:
        run.compare(args.compare)
        return
    if args.provider == "ollama" and args.effort:
        raise SystemExit("--effort is for --provider claude; use --think with Ollama")
    if args.provider == "claude" and args.think:
        raise SystemExit("--think is for --provider ollama; use --effort with Claude")
    config = load_or_exit()
    if args.provider == "ollama":
        models = args.model or [config.ollama.model]
        options = args.think or [config.ollama.think]
    else:
        models = args.model or [config.claude.model]
        options = args.effort or [config.claude.effort]
    configs = [
        run.BenchConfig(args.provider, model, option)
        for model in models
        for option in options
    ]
    run.run(
        configs,
        case_ids=args.case,
        tags=args.tag,
        repeat=args.repeat,
        out_dir=args.out,
    )


if __name__ == "__main__":
    main()
