"""
Answer questions about the user's data with Claude over the Anthropic API.

Used when `local_model = false` in the config. It shares the system prompt, tools and tool
loop with the local harness; only the model calls differ. The tool results the model asks
for (message rows) are sent to Anthropic.
"""

import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Any, cast

import anthropic
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaToolParam,
)

from vpop.assistant.conversation import Conversation, PendingCall, Reply
from vpop.assistant.errors import AuthError, MissingCredentialsError, api_error_detail
from vpop.assistant.toolbox import Toolbox
from vpop.config import AssistantConfig, ClaudeConfig
from vpop.fsutil import read_env_file
from vpop.paths import anthropic_env_path

log = logging.getLogger(__name__)

# Where to create an API key.
API_KEYS_URL = "https://platform.claude.com/settings/keys"

# On a safety refusal, the API re-runs the request on a fallback model it picks.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Settings `anthropic.env` may hold, and the client argument each becomes. A variable set in
# the shell wins over the file.
FILE_SETTINGS = {
    "ANTHROPIC_API_KEY": "api_key",
    "ANTHROPIC_AUTH_TOKEN": "auth_token",
    "ANTHROPIC_BASE_URL": "base_url",
}


@dataclass(frozen=True)
class Price:
    """A model's API prices in dollars per million tokens."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:
        """Writing the 5-minute cache that `cache_control: ephemeral` uses costs 1.25x input."""
        return self.input * 1.25


# Only for the cost estimate printed after each answer; billing is what counts. Includes the
# models a refusal fallback may route to.
PRICES = {
    "claude-fable-5-1": Price(input=10, output=50, cache_read=0.25),
    "claude-mythos-5-1": Price(input=10, output=50, cache_read=0.25),
    "claude-opus-5-5": Price(input=4, output=20, cache_read=0.20),
    "claude-opus-5": Price(input=5, output=25, cache_read=0.50),
    "claude-opus-4-8": Price(input=5, output=25, cache_read=0.50),
    "claude-opus-4-7": Price(input=5, output=25, cache_read=0.50),
    "claude-opus-4-6": Price(input=5, output=25, cache_read=0.50),
    "claude-sonnet-5-5": Price(input=2, output=10, cache_read=0.20),
    "claude-sonnet-5": Price(input=2, output=10, cache_read=0.20),
    "claude-sonnet-4-6": Price(input=3, output=15, cache_read=0.30),
    "claude-haiku-4-5": Price(input=1, output=5, cache_read=0.10),
}


def price_for(model: str) -> Price | None:
    """The price of a model id, also matching a dated snapshot (`claude-haiku-4-5-20251001`)."""
    return PRICES.get(re.sub(r"-\d{8}$", "", model))


def response_cost(response: BetaMessage) -> float | None:
    """
    Estimated dollar cost of one response, priced by the model that served it (a fallback
    model's response at its own rates). None when the model has no known price.
    """
    price = price_for(response.model)
    if price is None:
        return None
    usage = response.usage
    return (
        usage.input_tokens * price.input
        + (usage.cache_read_input_tokens or 0) * price.cache_read
        + (usage.cache_creation_input_tokens or 0) * price.cache_write
        + usage.output_tokens * price.output
    ) / 1_000_000


@dataclass(frozen=True)
class Spend:
    """
    Token and cost totals, so one question's share is the difference of two snapshots.
    `unpriced` names models that served responses but have no price, so `cost` leaves them out.
    """

    uncached: int
    cache_read: int
    cache_write: int
    output: int
    cost: float
    unpriced: frozenset[str]

    def __sub__(self, other: "Spend") -> "Spend":
        """The difference in totals; `unpriced` is this snapshot's, so set it as needed."""
        return Spend(
            self.uncached - other.uncached,
            self.cache_read - other.cache_read,
            self.cache_write - other.cache_write,
            self.output - other.output,
            self.cost - other.cost,
            self.unpriced,
        )


def usage_line(question: Spend, session: Spend) -> str:
    """
    One line of token use and estimated cost, e.g.
    `usage: 3,120 in + 41,800 cached + 2,050 cache write, 780 out · ~$0.03 (session ~$0.11)`.
    """
    tokens = (
        f"usage: {question.uncached:,} in + {question.cache_read:,} cached + "
        f"{question.cache_write:,} cache write, {question.output:,} out"
    )
    if question.unpriced:
        return f"{tokens} (no price for {', '.join(sorted(question.unpriced))})"
    session_part = "" if session.unpriced else f" (session ~${session.cost:.2f})"
    return f"{tokens} · ~${question.cost:.2f}{session_part}"


def file_settings() -> dict[str, str]:
    """
    The settings in `anthropic.env` beside the config file that the shell doesn't already
    set. The file is optional: the SDK also finds an exported `ANTHROPIC_API_KEY` or a
    profile from the Anthropic CLI's `ant auth login`.
    """
    return {
        var: value
        for var, value in read_env_file(anthropic_env_path()).items()
        if var in FILE_SETTINGS and value and not os.environ.get(var)
    }


def new_client(settings: dict[str, str]) -> anthropic.Anthropic:
    """A client using `file_settings()` over whatever the SDK finds itself."""
    kwargs: dict[str, Any] = {
        FILE_SETTINGS[var]: value for var, value in settings.items()
    }
    return anthropic.Anthropic(**kwargs)


def credential_source(client: anthropic.Anthropic, from_file: set[str]) -> str | None:
    """
    Which credential the client will send, as the SDK resolved it, or None if it found
    none. `from_file` names the settings that came from `anthropic.env`.
    """
    for var, value in (
        ("ANTHROPIC_API_KEY", client.api_key),
        ("ANTHROPIC_AUTH_TOKEN", client.auth_token),
    ):
        if value is not None:
            where = anthropic_env_path() if var in from_file else "the environment"
            return f"{var} from {where}"
    if client.credentials is not None:
        return "the Anthropic CLI profile (`ant auth status` shows which)"
    return None


def missing_credentials_message() -> str:
    """What to do when no credentials were found."""
    return (
        "no Anthropic credentials found, and the config has local_model = false.\n"
        "Run `vpop auth login` to set up a key, or give vpop one (create it at "
        f"{API_KEYS_URL}) yourself:\n"
        f"  - put ANTHROPIC_API_KEY=sk-ant-... in {anthropic_env_path()}\n"
        "  - export ANTHROPIC_API_KEY\n"
        "  - log in with the Anthropic CLI: ant auth login\n"
        "Or set local_model = true to answer with a local Ollama model instead."
    )


def rejected_credentials_message(source: str, error: anthropic.APIStatusError) -> str:
    """What to do when the API refused the credentials it was sent."""
    if source.startswith("the Anthropic CLI profile"):
        fix = "Log in again with: ant auth login"
    else:
        fix = f"Check the key at {API_KEYS_URL}, then run `vpop auth login` to replace it."
    return f"Anthropic rejected {source} ({error.status_code}): {api_error_detail(error)}\n{fix}"


def make_client() -> tuple[anthropic.Anthropic, str]:
    """
    A client with the first credentials found, and where they came from. Raises
    `AuthError` if there are none, rather than failing on the first request.
    """
    settings = file_settings()
    try:
        client = new_client(settings)
    except anthropic.CredentialsError as exc:
        # A profile picked by ANTHROPIC_PROFILE or ANTHROPIC_CONFIG_DIR that won't load.
        raise AuthError(
            f"couldn't load the Anthropic CLI profile: {exc}\n"
            "Log in again with: ant auth login"
        ) from exc
    source = credential_source(client, set(settings))
    if source is None:
        raise MissingCredentialsError(missing_credentials_message())
    return client, source


def claude_tools(tools: Toolbox) -> list[BetaToolParam]:
    """The tool definitions in the Anthropic API's shape."""
    return [
        {
            "name": schema["name"],
            "description": schema["description"],
            "input_schema": schema["parameters"],
        }
        for schema in tools.schemas()
    ]


def answer_text(message: BetaMessage) -> str:
    """The text of a response, without its thinking or tool-use blocks."""
    return "\n".join(
        block.text for block in message.content if block.type == "text"
    ).strip()


def refusal_text(message: BetaMessage) -> str:
    """What to show when Claude declined, with the refusal category when there is one."""
    details = message.stop_details
    category = getattr(details, "category", None) if details else None
    return "(Claude declined to answer this question" + (
        f"; category: {category})" if category else ".)"
    )


class ClaudeConversation(Conversation):  # pylint: disable=too-many-instance-attributes
    """
    A multi-turn conversation over the user's data with Claude.

    The history is only ever appended to, which keeps the cached prefix valid from one
    request to the next.
    """

    def __init__(
        self,
        tools: Toolbox,
        settings: ClaudeConfig | None = None,
        *,
        max_rounds: int = AssistantConfig.max_rounds,
        client: anthropic.Anthropic | None = None,
        today: str | None = None,
    ) -> None:
        super().__init__(tools, max_rounds=max_rounds, today=today)
        if client is None:
            client, self.credential_source = make_client()
        else:
            self.credential_source = "the provided client's credentials"
        self.client = client
        self.settings = settings or ClaudeConfig()
        self.tool_definitions = claude_tools(tools)
        self.messages: list[BetaMessageParam] = []
        self.cost = 0.0
        # Models that served a response but have no price, this session and this question.
        self.unpriced: set[str] = set()
        self.question_unpriced: set[str] = set()

    @property
    def history(self) -> list[Any]:
        return self.messages

    def add_question(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def request(self) -> BetaMessage:
        """
        Send the conversation and return the complete response. It streams, so a large
        `max_tokens` doesn't run into the SDK's limit for non-streaming requests, and the
        text is passed to the listener as it arrives, with a line break between text
        blocks as in `answer_text`.
        """
        streamed = False
        try:
            with self.client.beta.messages.stream(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=self.tools.system_prompt,
                messages=self.messages,
                tools=self.tool_definitions,
                thinking={"type": "adaptive"},
                # The config checks effort against the allowed values.
                output_config=cast(
                    BetaOutputConfigParam, {"effort": self.settings.effort}
                ),
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "text":
                        self.listener.text(event.text)
                        streamed = True
                    elif (
                        event.type == "content_block_start"
                        and event.content_block.type == "text"
                        and streamed
                    ):
                        self.listener.text("\n")
                return stream.get_final_message()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AuthError(
                rejected_credentials_message(self.credential_source, exc)
            ) from exc

    def record_usage(self, response: BetaMessage) -> None:
        """Add a response's tokens and estimated cost to the totals."""
        usage = response.usage
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        self.trace.rounds += 1
        self.trace.prompt_tokens += usage.input_tokens + cache_read + cache_write
        self.trace.cache_read_tokens += cache_read
        self.trace.cache_write_tokens += cache_write
        self.trace.output_tokens += usage.output_tokens
        cost = response_cost(response)
        if cost is None:
            self.unpriced.add(response.model)
            self.question_unpriced.add(response.model)
        else:
            self.cost += cost

    def chat(self) -> Reply:
        response = self.request()
        self.record_usage(response)
        # The full content goes back, thinking and fallback blocks included, as the API
        # requires.
        self.messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            return Reply(answer=refusal_text(response))
        if response.stop_reason == "max_tokens":
            # A tool call cut off mid-input can't be run.
            text = answer_text(response) or "(empty response)"
            return Reply(answer=f"{text}\n\n(Cut off at the max_tokens limit.)")
        calls = [
            PendingCall(
                id=block.id,
                name=block.name,
                arguments=dict(block.input) if isinstance(block.input, dict) else {},
            )
            for block in response.content
            if block.type == "tool_use"
        ]
        return Reply(answer=answer_text(response), calls=calls)

    def add_results(self, results: list[tuple[PendingCall, str]]) -> None:
        # All results in one user message, so the model keeps making parallel calls.
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": result,
                        "is_error": result.startswith("error:"),
                    }
                    for call, result in results
                ],
            }
        )

    def spend(self) -> Spend:
        """Token and cost totals for the conversation so far."""
        trace = self.trace
        return Spend(
            uncached=trace.prompt_tokens
            - trace.cache_read_tokens
            - trace.cache_write_tokens,
            cache_read=trace.cache_read_tokens,
            cache_write=trace.cache_write_tokens,
            output=trace.output_tokens,
            cost=self.cost,
            unpriced=frozenset(self.unpriced),
        )

    def ask(self, question: str) -> str:
        """Like `Conversation.ask`, then log the question's token use and estimated cost."""
        before = self.spend()
        self.question_unpriced = set()
        try:
            return super().ask(question)
        finally:
            after = self.spend()
            question_spend = replace(
                after - before, unpriced=frozenset(self.question_unpriced)
            )
            log.info("  %s", usage_line(question_spend, after))
