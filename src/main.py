"""Entry point for the vpop CLI."""

import argparse


def main():
    """Entry point for the vpop CLI. With no subcommand, runs `sync`."""
    parser = argparse.ArgumentParser(prog="vpop")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("sync", help="pull new message backups into the database")
    ask_parser = subcommands.add_parser(
        "ask", help="ask a question about your messages (no question starts a REPL)"
    )
    ask_parser.add_argument("question", nargs="*", help="the question to ask")
    args = parser.parse_args()

    # Imports are deferred: `sync` needs the Drive secrets at import time, `ask` doesn't.
    if args.command == "ask":
        from assistant.harness import ask, repl

        if args.question:
            print(ask(" ".join(args.question)))
        else:
            repl()
    else:
        from data_gathering.android_messages.sync import sync

        sync()


if __name__ == "__main__":
    main()
