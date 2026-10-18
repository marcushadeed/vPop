"""Tests for the toolbox: routing calls across toolsets."""

import pytest

from vpop.tools.toolbox import Toolbox, ToolError, Toolset, time_range


class Weather(Toolset):
    """A live toolset, standing in for one from an API."""

    tool_names = ("forecast",)

    def forecast(self, city: str) -> str:
        """
        The forecast for a city.

        Args:
            city: The city's name.
        """
        if city == "nowhere":
            raise ToolError("the weather service is down")
        return f"{city}: sunny"


class Clock(Toolset):
    """A toolset with a tool that takes no arguments."""

    tool_names = ("now",)

    def now(self) -> str:
        """The time."""
        return "12:00"


def test_calls_are_routed_to_their_toolset() -> None:
    box = Toolbox([Weather(), Clock()])
    assert box.names == ["forecast", "now"]
    assert [schema["name"] for schema in box.schemas()] == ["forecast", "now"]
    assert box.call("forecast", {"city": "Denver"}) == "Denver: sunny"
    assert box.call("now", {}) == "12:00"


def test_tool_errors_reach_the_model() -> None:
    box = Toolbox([Weather()])
    assert box.call("forecast", {"city": "nowhere"}) == (
        "error: the weather service is down"
    )
    assert box.call("tides", {}) == "error: unknown tool 'tides'; use one of forecast"


def test_duplicate_tool_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="'forecast'"):
        Toolbox([Weather(), Weather()])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-02", ("2024-02-01 00:00:00", "2024-02-29 23:59:59")),
        ("2026-08-01", ("2026-08-01 00:00:00", "2026-08-01 23:59:59")),
        ("2026-08-01 14:30", ("2026-08-01 14:30:00", "2026-08-01 14:30:59")),
        ("2026-08-01T14:30:05", ("2026-08-01 14:30:05", "2026-08-01 14:30:05")),
    ],
)
def test_time_range(value: str, expected: tuple[str, str]) -> None:
    assert time_range(value, "since") == expected


def test_time_range_rejects_impossible_dates() -> None:
    with pytest.raises(ValueError, match="must look like"):
        time_range("2026-02-30", "since")
