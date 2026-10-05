"""
Start a conversation with whichever model the config picks, and the plain question loop used
when stdin isn't a terminal (at a terminal, `vpop.ui.chat` runs the chat instead).
"""

import logging
import sys
from pathlib import Path

from vpop.assistant.conversation import Conversation
from vpop.assistant.errors import report_failures
from vpop.assistant.sql_tools import SqlTools
from vpop.assistant.toolbox import Toolbox, Toolset
from vpop.config import Config
from vpop.db import DatabaseError
from vpop.paths import db_path
from vpop.sources import SourceError
from vpop.sources.base import SourceUnavailable, SyncedSource
from vpop.sources.registry import enabled_sources

log = logging.getLogger(__name__)


def build_toolbox(config: Config, db: Path) -> Toolbox:
    """
    The tools of every enabled source that can be queried now, plus `run_sql` when a synced
    source's data is in the database at `db`. A source that can't be queried is left out
    with a warning; if none can, the first one's error is raised and the rest are warned of.
    """
    toolsets: list[Toolset] = []
    problems: list[SourceError | DatabaseError] = []
    database = False
    for source in enabled_sources(config):
        try:
            toolsets += source.toolsets(db)
        except (SourceUnavailable, DatabaseError) as exc:
            problems.append(exc)
            continue
        database = database or isinstance(source, SyncedSource)
    if not toolsets and not problems:
        raise SourceError("no data sources are enabled")
    for problem in problems[0 if toolsets else 1 :]:
        log.warning("%s", problem)
    if not toolsets:
        raise problems[0]
    if database:
        toolsets.append(SqlTools(db))
    return Toolbox(toolsets, db=db if database else None)


def new_conversation(
    config: Config, db: Path | None = None, today: str | None = None
) -> Conversation:
    """
    A conversation over the user's data (synced sources read the database at `db`, the
    default database if None) with the model the config picks. Raises `DatabaseError` if
    the database isn't usable yet and no other source is available.
    """
    tools = build_toolbox(config, db or db_path())
    max_rounds = config.assistant.max_rounds
    if config.assistant.local_model:
        # pylint: disable=import-outside-toplevel
        from vpop.assistant.ollama_harness import OllamaConversation

        return OllamaConversation(
            tools, config.ollama, max_rounds=max_rounds, today=today
        )
    # Deferred so a local-only setup never imports the Anthropic SDK, and the reverse.
    from vpop.assistant.claude_harness import (  # pylint: disable=import-outside-toplevel
        ClaudeConversation,
    )

    return ClaudeConversation(tools, config.claude, max_rounds=max_rounds, today=today)


def repl(conversation: Conversation) -> None:
    """
    The plain question loop: read questions from stdin, keeping the conversation history.
    Exit with Ctrl-D. Ctrl-C or a failure the user can act on (see `describe_error`) ends
    only that question; rejected credentials end the session.
    """
    while True:
        try:
            question = input("ask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        with report_failures(print_failure):
            print(conversation.ask(question), end="\n\n")


def print_failure(message: str | None) -> None:
    """How `repl` reports a question that ended early: interrupted, or why it failed."""
    print("(interrupted)" if message is None else f"error: {message}", file=sys.stderr)
    print(file=sys.stderr)
