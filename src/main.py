"""Entry point for the vpop CLI."""

import argparse
from pathlib import Path


def main():
    """Entry point for the vpop CLI. With no subcommand, runs `sync`."""
    parser = argparse.ArgumentParser(prog="vpop")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("sync", help="pull new message backups into the database")
    ask_parser = subcommands.add_parser(
        "ask", help="ask a question about your messages (no question starts a REPL)"
    )
    ask_parser.add_argument("question", nargs="*", help="the question to ask")
    bench_parser = subcommands.add_parser(
        "bench", help="benchmark models on fixed questions over a synthetic database"
    )
    bench_parser.add_argument(
        "--model", action="append", default=[], help="Ollama model (repeatable)"
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
        from assistant.harness import ask, repl

        if args.question:
            print(ask(" ".join(args.question)))
        else:
            repl()
    elif args.command == "bench":
        bench(args)
    else:
        from data_gathering.android_messages.sync import sync

        sync()


def bench(args: argparse.Namespace) -> None:
    """Run, list or compare benchmark cases."""
    from assistant.benchmark import run
    from assistant.benchmark.cases import CASES
    from assistant.harness import MODEL

    if args.list:
        for case in CASES:
            print(f"{case.id}  [{', '.join(case.tags)}]  {case.question}")
    elif args.compare:
        run.compare([Path(p) for p in args.compare])
    else:
        run.run(
            models=args.model or [MODEL],
            thinks=args.think,
            case_ids=args.case,
            tags=args.tag,
            repeat=args.repeat,
        )


if __name__ == "__main__":
    main()
