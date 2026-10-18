"""Tests for the toolbox: routing calls across toolsets."""

import pytest

from vpop.assistant.toolbox import Toolbox, ToolError, Toolset


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
