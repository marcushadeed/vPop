"""
A synthetic message history with known answers, for benchmarking the harness.

Everything is generated from the constants below with a fixed seed, so every run sees the same
database. Filler chatter gives each thread a known monthly volume; `FACTS` plants the specific
messages the benchmark questions ask about. Filler phrases never mention anything a question
asks about, so a fact can only be found where it was planted.
"""

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from data_gathering.android_messages.xml_to_sqlite import (
    Direction,
    Message,
    add_messages_to_sqlite,
)

# The date the model is told it is. Filler stops before it; the last facts land the day before.
TODAY = "2026-09-15"
SEED = 20260915


@dataclass(frozen=True)
class Contact:
    """A 1:1 thread: one contact on one number."""

    name: str
    number: str


PRIYA = Contact("Priya Patel", "+13015550111")
JORDAN_OLD = Contact("Jordan Lee", "+12025550101")
JORDAN_NEW = Contact("Jordan Lee", "+12025550102")
SAM = Contact("Sam Rivera", "+14105550121")
MOM = Contact("Mom", "+12405550131")
CHRIS = Contact("Chris Wong", "+12025550151")
UNKNOWN = Contact("(Unknown)", "+15715550141")
# Only ever appears in the book club group, so she has no name of her own.
DANA_NUMBER = "+12025550161"


@dataclass(frozen=True)
class Group:
    """A group thread and the numbers that post in it."""

    key: str
    contact_name: str
    members: tuple[str, ...]


TRIP_GROUP = Group(
    key=",".join(sorted((PRIYA.number, SAM.number, CHRIS.number))),
    contact_name="Priya Patel, Sam Rivera, Chris Wong",
    members=(PRIYA.number, SAM.number, CHRIS.number),
)
BOOK_CLUB = Group(
    key="8a1f33c0-6d2e-4b7a-9f10-5c3e2d7b4a91@rcs.google.com",
    contact_name="Priya Patel, (Unknown)",
    members=(PRIYA.number, DANA_NUMBER),
)

Thread = Contact | Group

# Filler volume: (thread, first month, last month, messages per month).
ACTIVITY: list[tuple[Thread, str, str, int]] = [
    (PRIYA, "2023-06", "2024-12", 15),
    (PRIYA, "2025-01", "2025-12", 40),
    (PRIYA, "2026-01", "2026-09", 15),
    (JORDAN_OLD, "2023-01", "2025-02", 20),
    (JORDAN_NEW, "2025-03", "2026-09", 20),
    (SAM, "2023-01", "2026-09", 12),
    (MOM, "2023-01", "2026-09", 25),
    (CHRIS, "2024-01", "2026-09", 8),
    (TRIP_GROUP, "2025-06", "2026-09", 10),
    (BOOK_CLUB, "2024-01", "2026-09", 6),
]

FILLER = [
    "ok",
    "sounds good",
    "haha yeah",
    "lol",
    "on my way",
    "running 5 min late",
    "did you see that game last night",
    "good morning!",
    "call you later?",
    "sure thing",
    "thanks!",
    "how's work going",
    "can't talk right now, text you after",
    "that's hilarious",
    "omg same",
    "nice",
    "what are you up to this weekend",
    "just got home",
    "👍",
    "happy friday",
    "ugh mondays",
    "k",
    "for real",
    "I'll let you know",
    "no worries",
    "love that",
    "traffic is terrible today",
    "want to grab coffee sometime",
    "that works for me",
    "see you soon",
]


@dataclass(frozen=True)
class Fact:
    """A planted message. `sender` is `me` for outgoing, else the sender's number."""

    thread: Thread
    timestamp: str
    sender: str
    body: str


ME = "me"

FACTS: list[Fact] = [
    # Jordan changes plans across a number change: the correction is on the new number.
    Fact(
        JORDAN_OLD,
        "2024-11-03 19:12:00",
        JORDAN_OLD.number,
        "Big news: I'm moving to Denver in June!",
    ),
    Fact(JORDAN_OLD, "2024-11-03 19:14:30", ME, "No way!! Congrats, that's huge"),
    Fact(
        JORDAN_NEW,
        "2025-03-02 10:00:00",
        JORDAN_NEW.number,
        "Hey it's Jordan, this is my new number. Save it!",
    ),
    Fact(
        JORDAN_NEW,
        "2025-04-10 20:41:00",
        JORDAN_NEW.number,
        "Change of plans, we're moving to Austin instead of Denver. Better job offer.",
    ),
    Fact(JORDAN_NEW, "2025-04-10 20:43:00", ME, "Austin is great, the food alone"),
    # Sam's dog.
    Fact(
        SAM,
        "2024-05-18 14:02:00",
        SAM.number,
        "We adopted a puppy!! His name is Biscuit",
    ),
    Fact(SAM, "2024-05-18 14:05:00", ME, "BISCUIT. I need pictures immediately"),
    # Mom's wifi password, and yesterday's reminder.
    Fact(
        MOM,
        "2025-12-24 16:30:00",
        MOM.number,
        "The new wifi password is otter-lantern-42",
    ),
    Fact(
        MOM,
        "2026-09-14 09:05:00",
        MOM.number,
        "Don't forget your dentist appointment Thursday at 3",
    ),
    Fact(MOM, "2026-09-14 09:20:00", ME, "I won't, thanks mom"),
    # Priya's birthday.
    Fact(
        PRIYA,
        "2025-03-01 11:00:00",
        ME,
        "When's your birthday again? I want to plan something",
    ),
    Fact(PRIYA, "2025-03-01 11:03:00", PRIYA.number, "March 14! You're sweet"),
    # Chris's restaurant, with the neighborhood in the next message.
    Fact(
        CHRIS, "2026-06-20 18:30:00", ME, "Need a dinner spot for Saturday, any ideas?"
    ),
    Fact(
        CHRIS,
        "2026-06-20 18:34:00",
        CHRIS.number,
        "You have to try Casa Verde, best mole I've ever had",
    ),
    Fact(
        CHRIS,
        "2026-06-20 18:35:00",
        CHRIS.number,
        "It's in Adams Morgan, right off 18th street",
    ),
    # A delivery code from an unknown number.
    Fact(
        UNKNOWN,
        "2026-07-08 13:15:00",
        UNKNOWN.number,
        "Your package is out for delivery. Use code 7731 at the locker.",
    ),
    # Group trip: Sam suggests it, dates settled later.
    Fact(
        TRIP_GROUP,
        "2026-07-12 20:00:00",
        SAM.number,
        "We should rent a cabin in Shenandoah this fall",
    ),
    Fact(TRIP_GROUP, "2026-07-12 20:04:00", PRIYA.number, "I'm in!!"),
    Fact(
        TRIP_GROUP,
        "2026-08-02 17:45:00",
        CHRIS.number,
        "Booked the cabin for October 10-12. Venmo me $140 each",
    ),
    # Book club pick.
    Fact(
        BOOK_CLUB,
        "2026-08-28 21:00:00",
        DANA_NUMBER,
        "Next pick is Piranesi, let's meet at the end of September",
    ),
    # The most recent message before TODAY.
    Fact(
        CHRIS,
        "2026-09-14 21:30:00",
        CHRIS.number,
        "you still up? just finished that show you recommended",
    ),
]


def months(first: str, last: str) -> Iterator[tuple[int, int]]:
    """Yield (year, month) from `first` to `last` inclusive, both `YYYY-MM`."""
    year, month = map(int, first.split("-"))
    end = tuple(map(int, last.split("-")))
    while (year, month) <= end:
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def days_in(year: int, month: int) -> int:
    """Number of days to spread filler over, stopping before TODAY in its own month."""
    if f"{year:04d}-{month:02d}" == TODAY[:7]:
        return int(TODAY[8:]) - 2
    if month == 2:
        return 29 if year % 4 == 0 else 28
    return 30 if month in (4, 6, 9, 11) else 31


def thread_key(thread: Thread) -> str:
    """The `thread_key` a thread is stored under."""
    return thread.number if isinstance(thread, Contact) else thread.key


def message(thread: Thread, timestamp: str, sender: str, body: str) -> Message:
    """Build a stored message; `sender` is `me` for outgoing."""
    outgoing = sender == ME
    return Message(
        direction=Direction.OUTGOING if outgoing else Direction.INCOMING,
        was_sent=True,
        thread_key=thread_key(thread),
        sender="" if outgoing else sender,
        contact_name=thread.name
        if isinstance(thread, Contact)
        else thread.contact_name,
        body=body,
        timestamp=timestamp,
    )


def filler(rng: random.Random) -> list[Message]:
    """Generate the background chatter described by `ACTIVITY`."""
    out = []
    for thread, first, last, per_month in ACTIVITY:
        senders = (
            [thread.number] if isinstance(thread, Contact) else list(thread.members)
        )
        for year, month in months(first, last):
            stamps: set[str] = set()
            while len(stamps) < per_month:
                stamps.add(
                    f"{year:04d}-{month:02d}-{rng.randint(1, days_in(year, month)):02d} "
                    f"{rng.randint(8, 22):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
                )
            for stamp in sorted(stamps):
                sender = ME if rng.random() < 0.5 else rng.choice(senders)
                out.append(message(thread, stamp, sender, rng.choice(FILLER)))
    return out


def all_messages() -> list[Message]:
    """Every message in the fixture, filler and facts, in timestamp order."""
    planted = [message(f.thread, f.timestamp, f.sender, f.body) for f in FACTS]
    return sorted(filler(random.Random(SEED)) + planted, key=lambda m: m.timestamp)


def count(predicate: Callable[[Message], bool]) -> int:
    """Count fixture messages matching `predicate`, for computing expected answers."""
    return sum(1 for m in all_messages() if predicate(m))


def build_fixture_db() -> None:
    """Write the fixture into the database at `db_path()` (point it with XDG_DATA_HOME)."""
    add_messages_to_sqlite(all_messages())
