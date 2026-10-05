"""Tests for picking a model, the REPL, and explaining backend errors."""

import builtins
from collections.abc import Iterator
from pathlib import Path

import anthropic
import httpx2
import ollama
import pytest
from helpers import build_db, make_message

from vpop.assistant.claude_harness import ClaudeConversation
from vpop.assistant.conversation import Conversation
from vpop.assistant.errors import AuthError, describe_error
from vpop.assistant.ollama_harness import OllamaConversation
from vpop.assistant.session import new_conversation, repl
from vpop.config import ClaudeConfig, Config, parse_config
from vpop.db import DatabaseError
from vpop.paths import db_path


@pytest.fixture
def database() -> Path:
    return build_db(db_path(), [make_message()])


def test_local_model_picks_ollama(database: Path) -> None:
    conversation = new_conversation(Config())
    assert isinstance(conversation, OllamaConversation)
    assert conversation.tools.db == database


def test_remote_model_picks_claude(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = parse_config("[assistant]\nlocal_model = false\nmax_rounds = 5\n")
    conversation = new_conversation(config)
    assert isinstance(conversation, ClaudeConversation)
    assert conversation.max_rounds == 5
    assert conversation.settings == ClaudeConfig()


def test_missing_database_fails_before_any_model_call() -> None:
    with pytest.raises(DatabaseError, match="vpop sync"):
        new_conversation(Config())


class Scripted(Conversation):
    """A conversation whose answers (or failures) are scripted per question."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.asked: list[str] = []

    @property
    def history(self) -> list[object]:
        return []

    def add_question(self, text: str) -> None: ...
    def chat(self) -> object: ...  # type: ignore[override]
    def add_results(self, results: object) -> None: ...  # type: ignore[override]

    def ask(self, question: str) -> str:
        self.asked.append(question)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)


def feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    it: Iterator[str] = iter(lines)

    def fake_input(_: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)


def test_repl_survives_interrupts_and_backend_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    feed(monkeypatch, ["one", "", "two", "three", "four"])
    conversation = Scripted(
        [KeyboardInterrupt(), ConnectionError("Failed to connect to Ollama."), "3", "4"]
    )
    repl(conversation)
    out, err = capsys.readouterr()
    assert conversation.asked == ["one", "two", "three", "four"]
    assert "(interrupted)" in err
    assert "error: Failed to connect to Ollama. Start Ollama with: ollama serve" in err
    assert "3\n\n4\n\n" in out


def test_repl_stops_on_rejected_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    feed(monkeypatch, ["one", "two"])
    conversation = Scripted([AuthError("rejected"), "never"])
    with pytest.raises(AuthError):
        repl(conversation)


def test_repl_surfaces_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    feed(monkeypatch, ["one"])
    with pytest.raises(ZeroDivisionError):
        repl(Scripted([ZeroDivisionError()]))


def api_error(
    cls: type[anthropic.APIStatusError], status: int
) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"message": "Overloaded"}}
    response = httpx2.Response(
        status, json=body, request=request, headers={"request-id": "req_1"}
    )
    return cls("x", response=response, body=body)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ollama.ResponseError("model 'x' not found", 404), "ollama pull"),
        (ollama.ResponseError("boom", 500), "Ollama error (500): boom"),
        (api_error(anthropic.RateLimitError, 429), "rate limit"),
        (api_error(anthropic.InternalServerError, 529), "error 529: Overloaded"),
        (DatabaseError("No database"), "No database"),
    ],
)
def test_describe_error(exc: BaseException, expected: str) -> None:
    message = describe_error(exc)
    assert message is not None and expected in message


def test_describe_error_leaves_bugs_alone() -> None:
    assert describe_error(KeyError("x")) is None
