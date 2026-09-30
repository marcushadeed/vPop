"""
Answer questions about the message database with Claude over the Anthropic API.

Used when `local_model = false` in the config. It shares the system prompt, tools and tool
handling with the local harness; only the model calls differ. The tool results the model asks
for (message rows) are sent to Anthropic.
"""

import os
from typing import Any, cast

import anthropic
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaToolParam,
)

from assistant.harness import (
    SYSTEM_PROMPT,
    TOOLS,
    ToolCall,
    Trace,
    call_tool,
    log_tool_call,
    today_label,
)
from config import AssistantConfig, ClaudeConfig
from paths import ANTHROPIC_ENV

# On a safety refusal, the API re-runs the request on a fallback model it picks.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def load_anthropic_env() -> None:
    """
    Load KEY=VALUE pairs from `secrets/anthropic.env`, if present (existing vars win).

    The file is optional: the SDK also finds an exported `ANTHROPIC_API_KEY` or an
    `ant auth login` profile.
    """
    if not ANTHROPIC_ENV.exists():
        return
    for raw_line in ANTHROPIC_ENV.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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
            load_anthropic_env()
            client = anthropic.Anthropic()
        self.client = client
        self.settings = settings or ClaudeConfig()
        self.max_rounds = max_rounds
        self.today = today
        self.verbose = verbose
        self.trace = Trace()
        self.tools = claude_tools()
        self.messages: list[BetaMessageParam] = []

    def chat(self) -> BetaMessage:
        """Send the conversation so far and return the model's reply."""
        response = self.client.beta.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=SYSTEM_PROMPT,
            messages=self.messages,
            tools=self.tools,
            thinking={"type": "adaptive"},
            # The config checks effort against the allowed values.
            output_config=cast(BetaOutputConfigParam, {"effort": self.settings.effort}),
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        self.trace.rounds += 1
        self.trace.prompt_tokens += (
            response.usage.input_tokens
            + (response.usage.cache_read_input_tokens or 0)
            + (response.usage.cache_creation_input_tokens or 0)
        )
        self.trace.output_tokens += response.usage.output_tokens
        return response

    def ask(self, question: str) -> str:
        """Ask a question and return the answer, keeping the exchange in the history."""
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
