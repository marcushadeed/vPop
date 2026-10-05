"""Tests for the toolbox: routing calls across toolsets, and assembling the system prompt."""

import pytest

from vpop.assistant.prompt import RULES, assemble, join_names
from vpop.assistant.toolbox import Toolbox, ToolError, Toolset


class Weather(Toolset):
    """A live toolset, standing in for one from an API."""

    tool_names = ("forecast",)
    data = "weather forecasts"
    prompt = "Use forecast for the weather."

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
    """A toolset with no data line and no prompt section."""

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
    assert box.db is None


def test_tool_errors_reach_the_model() -> None:
    box = Toolbox([Weather()])
    assert box.call("forecast", {"city": "nowhere"}) == (
        "error: the weather service is down"
    )
    assert box.call("tides", {}) == "error: unknown tool 'tides'; use one of forecast"


def test_duplicate_tool_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="'forecast'"):
        Toolbox([Weather(), Weather()])


def test_prompt_has_each_available_section_then_the_rules() -> None:
    prompt = Toolbox([Weather(), Clock()]).system_prompt
    assert prompt.startswith(
        "You answer questions about the user's own weather forecasts."
    )
    assert "Use forecast for the weather.\n\nIn every answer:" in prompt
    assert prompt.endswith(RULES)


def test_prompt_names_every_kind_of_data() -> None:
    prompt = assemble(["text messages", "", "calendar events"], ["", "", ""])
    assert prompt.startswith(
        "You answer questions about the user's own text messages and calendar events."
    )
    assert prompt.count("\n\n") == 1  # empty sections leave no gaps


@pytest.mark.parametrize(
    ("names", "joined"),
    [([], ""), (["a"], "a"), (["a", "b"], "a and b"), (["a", "b", "c"], "a, b and c")],
)
def test_join_names(names: list[str], joined: str) -> None:
    assert join_names(names) == joined
