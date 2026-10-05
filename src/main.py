"""Entry point for the vpop CLI."""

import argparse
import sys
from pathlib import Path

from config import (
    Config,
    ConfigError,
    config_path,
    load_config,
    render_config,
    write_default_config,
)


def main():
    """Entry point for the vpop CLI. With no subcommand, runs `sync`."""
    parser = argparse.ArgumentParser(prog="vpop")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("sync", help="update database with latest source information")
    ask_parser = subcommands.add_parser(
        "ask", help="ask vPop a question about your data"
    )
    ask_parser.add_argument("question", nargs="*", help="the question to ask. Leave empty to start a conversation")
    config_parser = subcommands.add_parser(
        "config", help="write or show the config file"
    )
    config_actions = config_parser.add_subparsers(dest="config_command", required=True)
    init_parser = config_actions.add_parser(
        "init", help="write a default config file"
    )
    init_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing config file"
    )
    config_actions.add_parser("show", help="print the settings in effect")
    auth_parser = subcommands.add_parser(
        "auth", help="set up the Anthropic credentials Claude answers with"
    )
    auth_actions = auth_parser.add_subparsers(dest="auth_command", required=True)
    auth_actions.add_parser(
        "login", help="save an API key (or log in with the Anthropic CLI)"
    )
    auth_actions.add_parser(
        "status", help="show which credentials are used and check they work"
    )
    auth_actions.add_parser("logout", help="remove the saved API key")
    bench_parser = subcommands.add_parser(
        "bench", help="benchmark models on fixed questions over a synthetic database"
    )
    bench_parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Ollama model (repeatable; defaults to the config's ollama.model)",
    )
    bench_parser.add_argument(
        "--think",
        action="append",
        default=[],
        choices=["on", "off", "default"],
        help="thinking setting (repeatable)",
    )
    bench_parser.add_argument(
        "--case", action="append", default=[], help="only this case id (repeatable)"
    )
    bench_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="only cases with this tag (repeatable)",
    )
    bench_parser.add_argument(
        "--repeat", type=int, default=1, help="times to run each case"
    )
    bench_parser.add_argument(
        "--compare",
        nargs="+",
        metavar="RESULTS",
        help="compare result files instead of running",
    )
    bench_parser.add_argument(
        "--list", action="store_true", help="list case ids and tags instead of running"
    )
    args = parser.parse_args()

    # Imports are deferred: `sync` needs the Drive secrets at import time, `ask` doesn't.
    if args.command == "ask":
        ask_command(args)
    elif args.command == "config":
        config_command(args)
    elif args.command == "auth":
        auth_command(args)
    elif args.command == "bench":
        bench(args)
    else:
        from data_gathering.android_messages.sync import sync

        sync()


def load_or_exit() -> Config:
    """The config file's settings, exiting with its error if it's invalid."""
    try:
        return load_config()
    except ConfigError as exc:
        raise SystemExit(f"bad config: {exc}") from exc


def ask_command(args: argparse.Namespace) -> None:
    """Answer one question, or start a conversation with none."""
    from assistant.auth import offer_login
    from assistant.harness import AuthError, MissingCredentialsError, ask, repl

    config = load_or_exit()
    for attempt in range(2):
        try:
            if args.question:
                print(ask(" ".join(args.question), config))
            else:
                repl(config)
            return
        except MissingCredentialsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            if attempt or not offer_login(config):
                raise SystemExit(1) from exc
        except AuthError as exc:
            raise SystemExit(f"error: {exc}") from exc


def auth_command(args: argparse.Namespace) -> None:
    """Set up, check or remove the Anthropic credentials."""
    from assistant import auth

    if args.auth_command == "login":
        if not auth.login(load_or_exit()):
            raise SystemExit(1)
    elif args.auth_command == "status":
        raise SystemExit(auth.status(load_or_exit()))
    else:
        auth.logout()


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


def bench(args: argparse.Namespace) -> None:
    """Run, list or compare benchmark cases."""
    from assistant.benchmark import run
    from assistant.benchmark.cases import CASES

    if args.list:
        for case in CASES:
            print(f"{case.id}  [{', '.join(case.tags)}]  {case.question}")
    elif args.compare:
        run.compare([Path(p) for p in args.compare])
    else:
        run.run(
            models=args.model or [load_or_exit().ollama.model],
            thinks=args.think,
            case_ids=args.case,
            tags=args.tag,
            repeat=args.repeat,
        )


if __name__ == "__main__":
    main()
