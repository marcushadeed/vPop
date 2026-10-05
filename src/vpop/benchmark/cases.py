"""
The benchmark questions and how to check their answers.

Expected counts are computed from the fixture's own spec rather than written in by hand, so
changing `fixture.ACTIVITY` can't silently leave a case with a stale number.
"""

from dataclasses import dataclass

from vpop.benchmark import fixture
from vpop.benchmark.fixture import JORDAN_NEW, JORDAN_OLD, MOM, PRIYA
from vpop.sources.android_messages.model import Direction

# A "no" / "not found" in the answer's first sentence, for questions whose honest answer is
# that nothing matches. Anchoring it to the first sentence keeps "…not once but twice" deep in
# an affirmative answer from counting.
NEGATIVE = (
    r"^[^.!?]*\b(no|not|never|didn'?t|doesn'?t|couldn'?t|can'?t|none|nothing|unable)\b"
)
# An answer that opens by affirming.
AFFIRMATIVE = r"^\W*(yes|yeah|yep)\b"


@dataclass(frozen=True)
class Case:  # pylint: disable=too-many-instance-attributes
    """
    One benchmark question and the checks its final answer must pass.

    Text checks are case-insensitive and run on the answer with markdown stripped. With
    `followups`, each is asked in turn in the same conversation and the checks apply to the
    answer to the last one.
    """

    id: str
    question: str
    tags: tuple[str, ...]
    followups: tuple[str, ...] = ()
    # Every substring must appear.
    must_include: tuple[str, ...] = ()
    # At least one substring must appear.
    any_of: tuple[str, ...] = ()
    # Every regex must match somewhere.
    must_match: tuple[str, ...] = ()
    # No substring may appear.
    must_not_include: tuple[str, ...] = ()
    # No regex may match anywhere.
    must_not_match: tuple[str, ...] = ()
    # This number must appear in the answer (thousands separators allowed).
    expect_number: int | None = None
    # Every number must appear, in any common US format.
    expect_phone_numbers: tuple[str, ...] = ()
    # Every tool must have been called at least once.
    expect_tools: tuple[str, ...] = ()
    # Cap on tool calls that returned an error.
    max_tool_errors: int | None = None


JORDAN_KEYS = (JORDAN_OLD.number, JORDAN_NEW.number)

JORDAN_2025 = fixture.count(
    lambda m: m.thread_key in JORDAN_KEYS and m.timestamp.startswith("2025")
)
MOM_AUG_2026 = fixture.count(
    lambda m: m.thread_key == MOM.number and m.timestamp.startswith("2026-08")
)
SENT_2025 = fixture.count(
    lambda m: m.direction == Direction.OUTGOING and m.timestamp.startswith("2025")
)
PRIYA_IN_LAST_MONTH = fixture.count(
    lambda m: (
        m.thread_key == PRIYA.number
        and m.direction == Direction.INCOMING
        and m.timestamp.startswith("2026-08")
    )
)
THREADS_2024 = len(
    {m.thread_key for m in fixture.all_messages() if m.timestamp.startswith("2024")}
)

CASES: list[Case] = [
    # Lookups: find the person, then the fact.
    Case(
        id="sam-dog-name",
        question="What's the name of Sam's dog?",
        tags=("lookup",),
        must_include=("Biscuit",),
        expect_tools=("find_threads",),
    ),
    Case(
        id="mom-wifi-password",
        question="What's the wifi password my mom sent me?",
        tags=("lookup",),
        must_include=("otter-lantern-42",),
    ),
    Case(
        id="priya-birthday",
        question="When is Priya's birthday?",
        tags=("lookup",),
        any_of=("march 14", "mar 14", "3/14", "14 march", "03-14"),
    ),
    Case(
        id="chris-restaurant",
        question="What restaurant did Chris recommend to me?",
        tags=("lookup",),
        must_include=("Casa Verde",),
    ),
    Case(
        id="unknown-delivery-code",
        question="A number that isn't in my contacts texted me a delivery code. What was the code?",
        tags=("lookup", "unknown-contact"),
        must_include=("7731",),
    ),
    Case(
        id="book-club-pick",
        question="What book did someone in my book club group chat say was the next pick?",
        tags=("lookup", "group"),
        must_include=("Piranesi",),
    ),
    # Context: the answer depends on a later message overriding an earlier one.
    Case(
        id="jordan-final-move",
        question="Where did Jordan end up moving?",
        tags=("context", "multi-thread"),
        must_include=("Austin",),
    ),
    Case(
        id="jordan-original-plan",
        question="Before changing plans, where was Jordan originally going to move?",
        tags=("context", "multi-thread"),
        must_include=("Denver",),
    ),
    Case(
        id="cabin-trip-dates",
        question="What dates did we book the cabin for in the group chat?",
        tags=("context", "group"),
        must_match=(r"oct(ober)?\.?\s*10|10/10|10-12",),
    ),
    Case(
        id="cabin-trip-suggester",
        question="In the group chat, who first suggested renting a cabin?",
        tags=("context", "group"),
        must_include=("Sam",),
    ),
    # A person with more than one number.
    Case(
        id="jordan-numbers",
        question="What phone numbers has Jordan texted me from?",
        tags=("multi-thread",),
        expect_phone_numbers=JORDAN_KEYS,
    ),
    Case(
        id="jordan-new-number-when",
        question="When did Jordan switch to a new phone number? Give the month and year.",
        tags=("multi-thread",),
        any_of=("march 2025", "mar 2025", "2025-03", "3/2025", "03/2025"),
    ),
    # Aggregates.
    Case(
        id="top-contact-2025",
        question="Who did I text with the most in 2025?",
        tags=("aggregate",),
        must_include=("Priya",),
    ),
    Case(
        id="jordan-count-2025",
        question=(
            "How many messages did Jordan and I exchange in 2025, counting every number "
            "Jordan used?"
        ),
        tags=("aggregate", "multi-thread"),
        expect_number=JORDAN_2025,
    ),
    Case(
        id="mom-count-aug-2026",
        question="How many messages were there between me and Mom in August 2026?",
        tags=("aggregate",),
        expect_number=MOM_AUG_2026,
    ),
    Case(
        id="sent-count-2025",
        question="How many text messages did I send in 2025, across all conversations?",
        tags=("aggregate",),
        expect_number=SENT_2025,
    ),
    Case(
        id="threads-2024",
        question="How many distinct conversation threads had at least one message in 2024?",
        tags=("aggregate",),
        expect_number=THREADS_2024,
    ),
    # Relative to the fixed "today".
    Case(
        id="mom-yesterday",
        question="What did my mom text me about yesterday?",
        tags=("date-relative",),
        must_include=("dentist",),
    ),
    Case(
        id="most-recent-texter",
        question="Who texted me most recently?",
        tags=("date-relative",),
        must_include=("Chris",),
    ),
    Case(
        id="priya-last-month",
        question="How many messages did Priya send me last month?",
        tags=("date-relative", "aggregate"),
        expect_number=PRIYA_IN_LAST_MONTH,
    ),
    # The honest answer is that nothing matches.
    Case(
        id="sam-paris-negative",
        question="Did Sam ever mention Paris?",
        tags=("negative",),
        must_match=(NEGATIVE,),
        must_not_match=(AFFIRMATIVE,),
    ),
    Case(
        id="taylor-negative",
        question="What did Taylor say about the concert?",
        tags=("negative",),
        must_match=(NEGATIVE,),
        must_not_match=(AFFIRMATIVE, r"\btaylor (said|wrote|texted|mentioned)\b"),
    ),
    # Multi-turn: the follow-up only makes sense with the first answer in context.
    Case(
        id="chris-restaurant-followup",
        question="What restaurant did Chris recommend?",
        followups=("What neighborhood is it in?",),
        tags=("multi-turn", "context"),
        must_include=("Adams Morgan",),
    ),
    Case(
        id="jordan-followup-when",
        question="Where did Jordan move to?",
        followups=("When did they first tell me about that city?",),
        tags=("multi-turn", "context", "multi-thread"),
        must_match=(r"april\s*10|apr\.?\s*10|2025-04-10|4/10",),
    ),
]

TAGS = sorted({tag for case in CASES for tag in case.tags})
