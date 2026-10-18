"""Tests for the message tools, over text messages and WhatsApp together."""

from contextlib import closing
from pathlib import Path

import pytest
from helpers import (
    MESSAGE_STORES,
    SAM,
    build_db,
    epoch,
    make_message,
    message_toolbox,
    outgoing,
)

from vpop import db
from vpop.sources.android_messages.model import Direction
from vpop.sources.whatsapp.model import WhatsAppMessage
from vpop.sources.whatsapp.store import insert_messages, refresh_chats
from vpop.tools import toolbox
from vpop.tools.messages import MessageTools
from vpop.tools.toolbox import Toolbox, tool_schema

ALEX = "+13015550000"
GROUP = f"{SAM},{ALEX}"
BOOK_CLUB = "120363000@g.us"


def wa(  # pylint: disable=too-many-arguments
    key_id: str,
    chat: str,
    body: str,
    timestamp: str,
    *,
    sender: str | None = None,
    outgoing: bool = False,
    attachments: str = "",
) -> WhatsAppMessage:
    return WhatsAppMessage(
        key_id=key_id,
        direction=Direction.OUTGOING if outgoing else Direction.INCOMING,
        chat_key=chat,
        sender="" if outgoing else (sender or chat),
        epoch_ms=epoch(timestamp),
        timestamp=timestamp,
        body=body,
        attachments=attachments,
    )


@pytest.fixture
def texts(tmp_path: Path) -> Path:
    """A database with text messages only: WhatsApp was never synced."""
    return build_db(
        tmp_path / "vpop.db",
        [
            make_message(
                body="I'm moving to Denver in June", timestamp="2026-03-01 10:00:00"
            ),
            outgoing(body="No way, when?", timestamp="2026-03-01 10:05:00"),
            make_message(body="First week of June", timestamp="2026-03-01 10:06:00"),
            make_message(
                thread_key=ALEX,
                sender=ALEX,
                contact_name="Alex Jones",
                body="lunch? 50% off_today",
                timestamp="2026-04-02 12:00:00",
            ),
            make_message(
                thread_key=GROUP,
                sender=ALEX,
                contact_name="Sam Smith, Alex Jones",
                body="group hello",
                timestamp="2026-05-03 18:00:00",
            ),
        ],
    )


@pytest.fixture
def both(tmp_path: Path) -> Path:
    """A database with text messages and WhatsApp messages, interleaved in time."""
    path = build_db(
        tmp_path / "both.db",
        [
            make_message(body="a text", timestamp="2026-08-01 09:00:00"),
            make_message(
                body="texting about Denver too", timestamp="2026-09-01 10:01:30"
            ),
        ],
    )
    with closing(db.connect(path)) as conn:
        insert_messages(
            conn,
            [
                wa("1", SAM, "Are you moving to Denver?", "2026-09-01 10:00:00"),
                wa("2", SAM, "Yes, in June", "2026-09-01 10:01:00", outgoing=True),
                wa("3", SAM, "", "2026-09-01 10:02:00", attachments="image/jpeg"),
                wa(
                    "4",
                    BOOK_CLUB,
                    "book is 50% off_today",
                    "2026-09-02 18:00:00",
                    sender=ALEX,
                ),
                wa("5", BOOK_CLUB, "count me in", "2026-09-02 18:05:00", sender=SAM),
                wa(
                    "6",
                    BOOK_CLUB,
                    "who's that",
                    "2026-09-02 18:06:00",
                    sender="777@lid",
                ),
            ],
        )
        refresh_chats(conn, {BOOK_CLUB: "Book club"})
    return path


@pytest.fixture
def tools(texts: Path) -> MessageTools:
    return MessageTools(texts, MESSAGE_STORES)


@pytest.fixture
def box(texts: Path) -> Toolbox:
    return message_toolbox(texts)


@pytest.fixture
def mixed(both: Path) -> MessageTools:
    return MessageTools(both, MESSAGE_STORES)


@pytest.mark.parametrize(
    "query", ["240-555-1234", "(240) 555 1234", "+12405551234", "5551234"]
)
def test_find_conversations_phone_formats(tools: MessageTools, query: str) -> None:
    result = tools.find_conversations(query)
    assert f"text:{SAM} | Sam Smith | 3 msgs" in result
    assert f"text:{GROUP} | Sam Smith, Alex Jones (group)" in result
    assert not any(line.startswith(f"text:{ALEX} |") for line in result.splitlines())


def test_find_conversations_by_name(tools: MessageTools) -> None:
    lines = tools.find_conversations("sam").splitlines()
    # Header, then most recently active first: the group, then Sam's 1:1 thread.
    assert lines[0] == "conversation | name | messages | first → last"
    assert lines[1].startswith(f"text:{GROUP}")
    assert lines[2] == (
        f"text:{SAM} | Sam Smith | 3 msgs | 2026-03-01 10:00:00 → 2026-03-01 10:06:00"
    )


def test_find_conversations_no_match(tools: MessageTools) -> None:
    assert tools.find_conversations("nobody") == "No conversations match 'nobody'."
    assert tools.find_conversations("%") == "No conversations match '%'."
    with pytest.raises(ValueError, match="empty"):
        tools.find_conversations("  ")


def test_find_conversations_in_every_source(mixed: MessageTools) -> None:
    assert mixed.find_conversations("sam").splitlines()[1:] == [
        f"whatsapp:{SAM} | Sam Smith | 3 msgs | 2026-09-01 10:00:00 → 2026-09-01 10:02:00",
        f"text:{SAM} | Sam Smith | 2 msgs | 2026-08-01 09:00:00 → 2026-09-01 10:01:30",
    ]
    assert f"whatsapp:{BOOK_CLUB} | Book club (group) | 3 msgs" in (
        mixed.find_conversations("book")
    )
    assert "whatsapp:" not in mixed.find_conversations("sam", source="text")


def test_search_words_match_word_forms(tools: MessageTools) -> None:
    assert tools.search_messages(text="DENVER") == (
        f"2026-03-01 10:00:00 | Sam Smith [text:{SAM}] | Sam Smith | "
        "I'm moving to Denver in June"
    )
    assert "Denver" in tools.search_messages(text="move denv")
    assert tools.search_messages(text="moving paris") == "No messages match."


def test_search_text_without_words_points_to_contains(box: Toolbox) -> None:
    assert "use contains" in box.call("search_messages", {"text": "%%"})


def test_contains_is_a_literal_substring(tools: MessageTools) -> None:
    assert "lunch?" in tools.search_messages(contains="50% OFF_")
    assert tools.search_messages(contains="_").count("\n") == 0  # only the lunch line
    assert tools.search_messages(contains="%x") == "No messages match."


def test_search_group_sender_gets_name(tools: MessageTools) -> None:
    assert tools.search_messages(text="group") == (
        f"2026-05-03 18:00:00 | Sam Smith, Alex Jones [text:{GROUP}] | Alex Jones | "
        "group hello"
    )


def test_search_conversation_filter_drops_conversation_column(
    tools: MessageTools,
) -> None:
    result = tools.search_messages(conversation="text:2405551234", sender="me")
    assert result == "2026-03-01 10:05:00 | me | No way, when?"


def test_search_date_bounds(tools: MessageTools) -> None:
    assert tools.search_messages(
        since="2026-04-01", until="2026-04-02"
    ).splitlines() == [
        (
            f"2026-04-02 12:00:00 | Alex Jones [text:{ALEX}] | Alex Jones | "
            "lunch? 50% off_today"
        )
    ]
    assert tools.search_messages(until="2026-03-01").count("\n") == 2
    assert tools.search_messages(since="2026-04", until="2026-04").count("\n") == 0


@pytest.mark.parametrize(
    ("bad", "field"), [("2026/08/01", "since"), ("August 2026", "until")]
)
def test_bad_dates_are_errors(box: Toolbox, bad: str, field: str) -> None:
    result = box.call("search_messages", {field: bad})
    assert result.startswith(f"error: {field} must look like")


def test_search_bad_direction(tools: MessageTools) -> None:
    with pytest.raises(ValueError, match="direction"):
        tools.search_messages(direction="sideways")


def test_search_me_conflicts_with_incoming(tools: MessageTools) -> None:
    with pytest.raises(ValueError, match='sender="me"'):
        tools.search_messages(sender="me", direction="incoming")


def test_search_more_line_and_offset(tools: MessageTools) -> None:
    first = tools.search_messages(limit=2).splitlines()
    assert len(first) == 3
    assert first[-1] == "… 3 more (use offset=2)"
    assert tools.search_messages(limit=2, offset=4).splitlines() == [
        (
            f"2026-03-01 10:00:00 | Sam Smith [text:{SAM}] | Sam Smith | "
            "I'm moving to Denver in June"
        )
    ]
    assert tools.search_messages(offset=10) == "No messages past offset 10."


def test_body_is_truncated_to_one_line(texts: Path) -> None:
    build_db(texts, [make_message(body="line one\nline two " + "x" * 500)])
    result = MessageTools(texts, MESSAGE_STORES).search_messages(contains="line one")
    body = result.rsplit(" | ", 1)[-1]
    assert body.startswith("line one ⏎ line two")
    assert len(body) == toolbox.BODY_CHARS
    assert body.endswith("…")


def test_unsent_and_attachments_are_shown(texts: Path) -> None:
    build_db(
        texts,
        [
            outgoing(
                body="did this go?", was_sent=False, timestamp="2026-06-01 08:00:00"
            ),
            make_message(
                body="",
                attachments="image/jpeg,video/mp4",
                timestamp="2026-06-01 09:00:00",
            ),
        ],
    )
    tools = MessageTools(texts, MESSAGE_STORES)
    lines = tools.read_conversation(f"text:{SAM}", since="2026-06").splitlines()
    assert lines[1:] == [
        "2026-06-01 08:00:00 | me (not sent) | did this go?",
        "2026-06-01 09:00:00 | Sam Smith | [image/jpeg] [video/mp4]",
    ]


def test_read_conversation_latest(tools: MessageTools) -> None:
    assert tools.read_conversation(f"text:{SAM}", limit=2).splitlines() == [
        f"text:{SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-03-01 10:05:00 | me | No way, when?",
        "2026-03-01 10:06:00 | Sam Smith | First week of June",
    ]


def test_read_conversation_around(tools: MessageTools) -> None:
    lines = tools.read_conversation(
        "text:2405551234", around="2026-03-01 10:05:00", limit=2
    ).splitlines()
    assert lines == [
        f"text:{SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-03-01 10:05:00 | me | No way, when?",
        "2026-03-01 10:06:00 | Sam Smith | First week of June",
    ]


def test_read_conversation_since(tools: MessageTools) -> None:
    assert tools.read_conversation(
        f"text:{SAM}", since="2026-03-01", limit=1
    ).splitlines() == [
        f"text:{SAM} (Sam Smith): 1 of 3 messages",
        "2026-03-01 10:00:00 | Sam Smith | I'm moving to Denver in June",
        "… 2 later (use since or around)",
    ]


def test_read_conversation_counts_messages_sharing_a_second(tmp_path: Path) -> None:
    same = "2026-07-01 12:00:00"
    path = build_db(
        tmp_path / "vpop.db",
        [
            make_message(body=f"burst {i}", timestamp=same, epoch_ms=1000 + i)
            for i in range(4)
        ],
    )
    lines = MessageTools(path, MESSAGE_STORES).read_conversation(f"text:{SAM}", limit=2)
    assert lines.splitlines()[1] == "… 2 earlier (use until or around)"
    assert [line.split(" | ")[-1] for line in lines.splitlines()[2:]] == [
        "burst 2",
        "burst 3",
    ]


def test_read_conversation_group_key_with_spaces(tools: MessageTools) -> None:
    assert "group hello" in tools.read_conversation("text:" + GROUP.replace(",", ", "))


def test_read_conversation_unknown(tools: MessageTools) -> None:
    assert tools.read_conversation("text:+19999999999") == (
        "No messages in text:+19999999999 for that range. "
        "Check the id with find_conversations."
    )


def test_search_interleaves_sources_by_time(mixed: MessageTools) -> None:
    assert mixed.search_messages(text="denver").splitlines() == [
        (
            f"2026-09-01 10:01:30 | Sam Smith [text:{SAM}] | Sam Smith | "
            "texting about Denver too"
        ),
        (
            f"2026-09-01 10:00:00 | Sam Smith [whatsapp:{SAM}] | Sam Smith | "
            "Are you moving to Denver?"
        ),
    ]


def test_paging_is_correct_across_sources(mixed: MessageTools) -> None:
    everything = mixed.search_messages().splitlines()
    assert len(everything) == 8
    pages = [
        line
        for offset in range(0, 8, 3)
        for line in mixed.search_messages(limit=3, offset=offset).splitlines()
        if not line.startswith("…")
    ]
    assert pages == everything
    assert mixed.search_messages(limit=3).splitlines()[-1] == "… 5 more (use offset=3)"


def test_a_number_finds_its_sender_in_every_source(mixed: MessageTools) -> None:
    result = mixed.search_messages(sender="240-555-1234")
    assert f"[whatsapp:{BOOK_CLUB}] | Sam Smith | count me in" in result
    assert f"[text:{SAM}] | Sam Smith | a text" in result
    assert f"[whatsapp:{SAM}] | Sam Smith | Are you moving to Denver?" in result
    assert "Yes, in June" not in result  # sent by me


def test_whatsapp_groups_name_their_senders(mixed: MessageTools) -> None:
    result = mixed.search_messages(conversation=f"whatsapp:{BOOK_CLUB}")
    assert result.splitlines() == [
        "2026-09-02 18:06:00 | 777@lid | who's that",
        "2026-09-02 18:05:00 | Sam Smith | count me in",
        f"2026-09-02 18:00:00 | {ALEX} | book is 50% off_today",
    ]


def test_source_filter(mixed: MessageTools, box: Toolbox) -> None:
    result = mixed.search_messages(source="whatsapp")
    assert "[text:" not in result and "[whatsapp:" in result
    assert mixed.search_messages(source="Text").count("[text:") == 2
    assert box.call("search_messages", {"source": "sms"}) == (
        "error: source must be one of text, whatsapp, not 'sms'"
    )
    assert box.call(
        "search_messages", {"source": "text", "conversation": f"whatsapp:{SAM}"}
    ) == (f"error: conversation 'whatsapp:{SAM}' is in whatsapp, not text")


def test_conversations_are_routed_by_their_source(mixed: MessageTools) -> None:
    assert mixed.read_conversation(
        f"whatsapp:{SAM}", around="2026-09-01 10:01:00", limit=2
    ).splitlines() == [
        f"whatsapp:{SAM} (Sam Smith): 2 of 3 messages",
        "… 1 earlier (use until or around)",
        "2026-09-01 10:01:00 | me | Yes, in June",
        "2026-09-01 10:02:00 | Sam Smith | [image/jpeg]",
    ]
    assert mixed.read_conversation(f"TEXT:{SAM}").startswith(
        f"text:{SAM} (Sam Smith): 2 of 2 messages"
    )


@pytest.mark.parametrize("conversation", [SAM, f"signal:{SAM}", "", BOOK_CLUB])
def test_a_conversation_without_a_known_source_is_an_error(
    box: Toolbox, conversation: str
) -> None:
    result = box.call("read_conversation", {"conversation": conversation or " "})
    assert result.startswith(
        "error: conversation must start with its source, one of text:, whatsapp: "
        '(e.g. "text:+12405551234", from find_conversations)'
    )


def test_an_unsynced_source_says_to_sync(box: Toolbox, tmp_path: Path) -> None:
    unsynced = "error: no WhatsApp messages have been synced yet; run `vpop sync`"
    assert box.call("search_messages", {"source": "whatsapp"}) == unsynced
    assert box.call("read_conversation", {"conversation": f"whatsapp:{SAM}"}) == (
        unsynced
    )
    # Searching everything searches what has been synced.
    assert "Denver" in box.call("search_messages", {"text": "denver"})
    empty = message_toolbox(build_db(tmp_path / "empty.db", []))
    assert empty.call("find_conversations", {"name_or_number": "sam"}) == (
        "error: no text messages and WhatsApp messages have been synced yet; "
        "run `vpop sync`"
    )


def test_missing_database_is_a_tool_error(tmp_path: Path) -> None:
    result = message_toolbox(tmp_path / "none.db").call(
        "find_conversations", {"name_or_number": "x"}
    )
    assert result.startswith("error: No database at")


def test_results_are_capped(box: Toolbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toolbox, "MAX_RESULT_CHARS", 80)
    result = box.call("search_messages", {})
    assert len(result.splitlines()[0]) <= 80
    assert result.splitlines()[-1].startswith("… output cut at 80 characters")


def test_call_drops_empty_arguments_and_checks_the_rest(box: Toolbox) -> None:
    assert "Denver" in box.call("search_messages", {"text": "denver", "sender": None})
    assert box.call("search_messages", {"bogus": 1}).startswith("error:")
    assert box.call("drop_table", {}).startswith("error: unknown tool")


def test_tool_schema_from_signature_and_docstring(box: Toolbox) -> None:
    schema = tool_schema(box.functions["search_messages"])
    assert schema["name"] == "search_messages"
    assert schema["description"].startswith("Search messages in every source")
    assert "Args:" not in schema["description"]
    params = schema["parameters"]
    assert params["required"] == []
    assert params["properties"]["limit"]["type"] == "integer"
    assert params["properties"]["text"]["type"] == "string"
    assert "moving" in params["properties"]["text"]["description"]
    find = tool_schema(box.functions["find_conversations"])
    assert find["parameters"]["required"] == ["name_or_number"]
