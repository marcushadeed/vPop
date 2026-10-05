"""
Start a conversation with whichever model the config picks, and the plain question loop used
when stdin isn't a terminal (at a terminal, `vpop.ui.chat` runs the chat instead).
"""

import sys
from pathlib import Path

from vpop.assistant.conversation import Conversation
from vpop.assistant.errors import report_failures
from vpop.assistant.tools import MessageTools
from vpop.config import Config
from vpop.db import check_readable
from vpop.paths import db_path


def new_conversation(
    config: Config, db: Path | None = None, today: str | None = None
) -> Conversation:
    """
    A conversation over the database at `db` (the default database if None) with the
    model the config picks. Raises `DatabaseError` if the database isn't usable yet.
    """
    db = db or db_path()
    check_readable(db)
    tools = MessageTools(db)
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
