"""Start a conversation with whichever model the config picks, for one question or a REPL."""

import sys
from pathlib import Path

from vpop.assistant.conversation import Conversation
from vpop.assistant.errors import AuthError, describe_error
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
    Interactive question loop that keeps the conversation history. Exit with Ctrl-D.
    Ctrl-C or a failure the user can act on (see `describe_error`) ends only that question;
    rejected credentials end the session.
    """
    while True:
        try:
            question = input("ask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        try:
            print(conversation.ask(question), end="\n\n")
        except KeyboardInterrupt:
            print("(interrupted)\n", file=sys.stderr)
        except AuthError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            message = describe_error(exc)
            if message is None:
                raise
            print(f"error: {message}\n", file=sys.stderr)
