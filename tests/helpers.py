"""Builders shared by the tests."""

from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vpop import db
from vpop.sources.android_messages import source as android_messages
from vpop.sources.android_messages.model import Direction, Message
from vpop.sources.android_messages.store import insert_messages
from vpop.sources.whatsapp import source as whatsapp
from vpop.tools.messages import MessageTools
from vpop.tools.sql import SqlTools
from vpop.tools.toolbox import Toolbox

SAM = "+12405551234"


def epoch(timestamp: str) -> int:
    """Epoch milliseconds for a `YYYY-MM-DD HH:MM:SS` string read as UTC."""
    return int(datetime.fromisoformat(timestamp).replace(tzinfo=UTC).timestamp() * 1000)


def make_message(**overrides: Any) -> Message:
    """An incoming message from Sam, with `epoch_ms` following `timestamp` unless given."""
    fields: dict[str, Any] = {
        "direction": Direction.INCOMING,
        "was_sent": True,
        "thread_key": SAM,
        "sender": SAM,
        "contact_name": "Sam Smith",
        "body": "hello",
        "timestamp": "2026-08-01 09:00:00",
    }
    fields.update(overrides)
    fields.setdefault("epoch_ms", epoch(fields["timestamp"]))
    return Message(**fields)


def outgoing(**overrides: Any) -> Message:
    """A message the user sent to Sam."""
    return make_message(direction=Direction.OUTGOING, sender="", **overrides)


def build_db(path: Path, messages: Iterable[Message]) -> Path:
    """A database at `path` holding `messages`."""
    with closing(db.connect(path)) as conn:
        insert_messages(conn, messages)
    return path


# Every message source's store, as when WhatsApp is enabled.
MESSAGE_STORES = [android_messages.MESSAGES, whatsapp.MESSAGES]


def message_toolbox(path: Path) -> Toolbox:
    """The message tools over every message source, and run_sql."""
    return Toolbox([MessageTools(path, MESSAGE_STORES), SqlTools(path)])
