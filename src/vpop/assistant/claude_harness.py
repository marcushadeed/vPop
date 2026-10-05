"""
Answer questions about the message database with Claude over the Anthropic API.

Used when `local_model = false` in the config. It shares the system prompt, tools and tool
handling with the local harness; only the model calls differ. The tool results the model asks
for (message rows) are sent to Anthropic.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, cast

import anthropic
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaToolParam,
)

from vpop.assistant.harness import (
    SYSTEM_PROMPT,
    TOOLS,
    AuthError,
    MissingCredentialsError,
    ToolCall,
    Trace,
    call_tool,
    log_tool_call,
    today_label,
)
from vpop.config import AssistantConfig, ClaudeConfig
from vpop.fsutil import read_env_file
from vpop.paths import anthropic_env_path

log = logging.getLogger(__name__)

# Where to create an API key.
API_KEYS_URL = "https://platform.claude.com/settings/keys"

# On a safety refusal, the API re-runs the request on a fallback model it picks.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


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


# Only for the cost estimate printed after each answer; billing is what counts.
PRICES = {
    "claude-sonnet-5-5": Price(input=2, output=10, cache_read=0.20),
    "claude-opus-5-5": Price(input=4, output=20, cache_read=0.20),
    "claude-fable-5-1": Price(input=10, output=50, cache_read=0.25),
    "claude-haiku-4-5": Price(input=1, output=5, cache_read=0.10),
}


def response_cost(response: BetaMessage) -> float | None:
    """
    Estimated dollar cost of one response, priced by the model that served it (a fallback
    model's response at its own rates). None when the model isn't in `PRICES`.
    """
    price = PRICES.get(response.model)
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
    """Token and cost totals, so one question's share is the difference of two snapshots."""

    uncached: int
    cache_read: int
    cache_write: int
    output: int
    cost: float
    unpriced: frozenset[str]

    def __sub__(self, other: "Spend") -> "Spend":
        return Spend(
            self.uncached - other.uncached,
            self.cache_read - other.cache_read,
            self.cache_write - other.cache_write,
            self.output - other.output,
            self.cost - other.cost,
            self.unpriced - other.unpriced,
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


def load_anthropic_env() -> set[str]:
    """
    Load KEY=VALUE pairs from `anthropic.env` beside the config file, if present (existing
    vars win). Returns the names it set.

    The file is optional: the SDK also finds an exported `ANTHROPIC_API_KEY` or a profile
    from the Anthropic CLI's `ant auth login`.
    """
    loaded: set[str] = set()
    for key, value in read_env_file(anthropic_env_path()).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.add(key)
    return loaded


def credential_source(client: anthropic.Anthropic, from_file: set[str]) -> str | None:
    """
    Which credential the client will send, as the SDK resolved it, or None if it found
    none. `from_file` is what `load_anthropic_env` set.
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
    # The body's own message ("API key is invalid.") reads better than the SDK's repr.
    body = error.body if isinstance(error.body, dict) else {}
    detail = body.get("error", {}).get("message") or error.message
    return f"Anthropic rejected {source} ({error.status_code}): {detail}\n{fix}"


def make_client() -> tuple[anthropic.Anthropic, str]:
    """
    A client with the first credentials the SDK finds, and where they came from. Raises
    `AuthError` if it finds none, rather than failing on the first request.
    """
    from_file = load_anthropic_env()
    try:
        client = anthropic.Anthropic()
    except anthropic.CredentialsError as exc:
        # A profile picked by ANTHROPIC_PROFILE or ANTHROPIC_CONFIG_DIR that won't load.
        raise AuthError(
            f"couldn't load the Anthropic CLI profile: {exc}\n"
            "Log in again with: ant auth login"
        ) from exc
    source = credential_source(client, from_file)
    if source is None:
        raise MissingCredentialsError(missing_credentials_message())
    return client, source


def claude_tools() -> list[BetaToolParam]:
    """The local harness's tool definitions in the Anthropic API's shape."""
    tools: list[BetaToolParam] = []
    for tool in TOOLS:
        assert tool.function is not None and tool.function.parameters is not None
        tools.append(
            {
                "name": tool.function.name or "",
                "description": tool.function.description or "",
                "input_schema": tool.function.parameters.model_dump(exclude_none=True),
            }
        )
    return tools


def answer_text(message: BetaMessage) -> str:
    """The text of a response, without its thinking or tool-use blocks."""
    return "\n".join(
        block.text for block in message.content if block.type == "text"
    ).strip()


class ClaudeConversation:  # pylint: disable=too-many-instance-attributes
    """
    A multi-turn conversation over the database with Claude.

    The history is only ever appended to, which keeps the cached prefix valid from one
    request to the next.
    """

    def __init__(
        self,
        settings: ClaudeConfig | None = None,
        max_rounds: int = AssistantConfig.max_rounds,
        client: anthropic.Anthropic | None = None,
        today: str | None = None,
        verbose: bool = True,
    ) -> None:
        """Same `today` and `verbose` as the local `Conversation`."""
        if client is None:
            client, self.credential_source = make_client()
        else:
            self.credential_source = "the provided client's credentials"
        self.client = client
        self.settings = settings or ClaudeConfig()
        self.max_rounds = max_rounds
        self.today = today
        self.verbose = verbose
        self.trace = Trace()
        self.tools = claude_tools()
        self.messages: list[BetaMessageParam] = []
        self.cost = 0.0
        # Models that served a response but have no entry in `PRICES`.
        self.unpriced: set[str] = set()

    def chat(self) -> BetaMessage:
        """Send the conversation so far and return the model's reply."""
        try:
            response = self.client.beta.messages.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=SYSTEM_PROMPT,
                messages=self.messages,
                tools=self.tools,
                thinking={"type": "adaptive"},
                # The config checks effort against the allowed values.
                output_config=cast(
                    BetaOutputConfigParam, {"effort": self.settings.effort}
                ),
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AuthError(
                rejected_credentials_message(self.credential_source, exc)
            ) from exc
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
        else:
            self.cost += cost
        return response

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
        """
        Ask a question and return the answer, keeping the exchange in the history. With
        `verbose`, logs the question's token use and estimated cost.
        """
        before = self.spend()
        answer = self.answer(question)
        if self.verbose:
            after = self.spend()
            log.info("  %s", usage_line(after - before, after))
        return answer

    def answer(self, question: str) -> str:
        """Run the tool loop for a question until the model answers or gives up."""
        self.messages.append(
            {
                "role": "user",
                "content": f"(Today is {today_label(self.today)}.)\n\n{question}",
            }
        )
        for _ in range(self.max_rounds):
            reply = self.chat()
            # The full content goes back, thinking blocks included, as the API requires.
            self.messages.append(
                {"role": "assistant", "content": reply.content}  # type: ignore[typeddict-item]
            )
            if reply.stop_reason == "refusal":
                return "(Claude declined to answer this question.)"
            tool_uses = [block for block in reply.content if block.type == "tool_use"]
            if not tool_uses:
                text = answer_text(reply) or "(empty response)"
                if reply.stop_reason == "max_tokens":
                    text += "\n\n(Cut off at the max_tokens limit.)"
                return text
            results: list[dict[str, Any]] = []
            for block in tool_uses:
                arguments = dict(block.input) if isinstance(block.input, dict) else {}
                if self.verbose:
                    log_tool_call(block.name, arguments)
                result = call_tool(block.name, arguments)
                self.trace.tool_calls.append(ToolCall(block.name, arguments, result))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": result.startswith("error:"),
                    }
                )
            # All results in one user message, so the model keeps making parallel calls.
            self.messages.append(
                {"role": "user", "content": results}  # type: ignore[typeddict-item]
            )
        self.trace.hit_round_limit = True
        return (
            f"(Stopped after {self.max_rounds} rounds of tool calls "
            "without a final answer.)"
        )
