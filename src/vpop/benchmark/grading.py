"""Grade a harness answer against a case's checks. Pure functions, no model calls."""

import re
from dataclasses import dataclass

from vpop.assistant.conversation import Trace
from vpop.benchmark.cases import Case


@dataclass(frozen=True)
class Check:
    """The outcome of one check on an answer."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Grade:
    """All check outcomes for one answer."""

    checks: list[Check]

    @property
    def passed(self) -> bool:
        """Whether every check passed."""
        return all(check.passed for check in self.checks)

    @property
    def score(self) -> float:
        """Fraction of checks that passed."""
        if not self.checks:
            return 1.0
        return sum(check.passed for check in self.checks) / len(self.checks)


def normalize(answer: str) -> str:
    """Casefold, drop markdown emphasis and code marks, and collapse whitespace."""
    text = re.sub(r"[*_`#]", "", answer)
    # Curly quotes and apostrophes, so "didn’t" matches "didn't".
    text = text.translate(str.maketrans("‘’“”", "''\"\""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def numbers_in(text: str) -> set[int]:
    """Integers in `text`, reading `1,234` as one number."""
    return {
        int(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+|\d+", text)
    }


def phone_numbers_in(text: str) -> set[str]:
    """Ten-digit US numbers in `text`, whatever their punctuation."""
    found = set()
    for match in re.findall(r"\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", text):
        digits = re.sub(r"\D", "", match)
        found.add(digits[-10:])
    return found


def grade(case: Case, answer: str, trace: Trace) -> Grade:
    """Run every check `case` configures against `answer` and the conversation's trace."""
    text = normalize(answer)
    checks: list[Check] = []

    for needle in case.must_include:
        checks.append(Check(f"includes {needle!r}", needle.casefold() in text, ""))
    if case.any_of:
        hits = [n for n in case.any_of if n.casefold() in text]
        checks.append(
            Check(
                f"includes one of {list(case.any_of)}",
                bool(hits),
                f"found {hits}" if hits else "",
            )
        )
    for pattern in case.must_match:
        checks.append(
            Check(f"matches /{pattern}/", re.search(pattern, text) is not None, "")
        )
    for needle in case.must_not_include:
        checks.append(Check(f"excludes {needle!r}", needle.casefold() not in text, ""))
    if case.expect_number is not None:
        found = numbers_in(text)
        checks.append(
            Check(
                f"states {case.expect_number}",
                case.expect_number in found,
                f"numbers in answer: {sorted(found)}",
            )
        )
    if case.expect_phone_numbers:
        phones = phone_numbers_in(text)
        for number in case.expect_phone_numbers:
            checks.append(
                Check(
                    f"lists {number}", number[-10:] in phones, f"found {sorted(phones)}"
                )
            )
    called = {call.name for call in trace.tool_calls}
    for tool in case.expect_tools:
        checks.append(
            Check(f"called {tool}", tool in called, f"called {sorted(called)}")
        )
    if case.max_tool_errors is not None:
        errors = sum(call.is_error for call in trace.tool_calls)
        checks.append(
            Check(
                f"at most {case.max_tool_errors} tool errors",
                errors <= case.max_tool_errors,
                f"{errors} errors",
            )
        )
    # Every question needs data, so an answer without a tool call is a guess, even a "no".
    checks.append(
        Check("used a tool", bool(trace.tool_calls), f"{len(trace.tool_calls)} calls")
    )
    # Every case needs a real answer, not a give-up.
    checks.append(
        Check(
            "finished within round limit",
            not trace.hit_round_limit,
            f"{trace.rounds} rounds",
        )
    )
    return Grade(checks)
