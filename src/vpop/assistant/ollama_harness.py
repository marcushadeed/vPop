"""
Answer questions about the message database with a local Ollama model; nothing leaves the
machine. The model never sees the database directly: it calls the read-only tools in
`tools` and pulls in only the rows it needs.
"""

import logging
from typing import Any

import ollama

from vpop.assistant.conversation import Conversation, PendingCall, Reply
from vpop.assistant.prompt import SYSTEM_PROMPT
from vpop.assistant.tools import MessageTools
from vpop.config import AssistantConfig, OllamaConfig

log = logging.getLogger(__name__)

# Warn when a prompt fills this much of the context window: past it, Ollama starts dropping
# the oldest messages without saying so.
CONTEXT_WARNING = 0.9


def parse_think(value: str) -> bool | None:
    """Map "on"/"off" to True/False; "default" (the model's own choice) to None."""
    return {"on": True, "off": False}.get(value)


def ollama_tools(tools: MessageTools) -> list[dict[str, Any]]:
    """The tool definitions in Ollama's (OpenAI-style) shape."""
    return [{"type": "function", "function": schema} for schema in tools.schemas()]


class OllamaConversation(Conversation):
    """A multi-turn conversation over the database with a local Ollama model."""

    def __init__(
        self,
        tools: MessageTools,
        settings: OllamaConfig | None = None,
        *,
        max_rounds: int = AssistantConfig.max_rounds,
        client: ollama.Client | None = None,
        today: str | None = None,
    ) -> None:
        super().__init__(tools, max_rounds=max_rounds, today=today)
        self.client = client or ollama.Client()
        self.settings = settings or OllamaConfig()
        self.tool_definitions = ollama_tools(tools)
        self.messages: list[dict[str, Any] | ollama.Message] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    @property
    def history(self) -> list[Any]:
        return self.messages

    def add_question(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def chat(self) -> Reply:
        response = self.client.chat(
            model=self.settings.model,
            messages=self.messages,
            tools=self.tool_definitions,
            think=parse_think(self.settings.think),
            options={"num_ctx": self.settings.num_ctx, "temperature": 0},
        )
        prompt_tokens = response.prompt_eval_count or 0
        self.trace.rounds += 1
        self.trace.prompt_tokens += prompt_tokens
        self.trace.output_tokens += response.eval_count or 0
        self.trace.model_ns += response.total_duration or 0
        if prompt_tokens >= CONTEXT_WARNING * self.settings.num_ctx:
            log.warning(
                "the conversation is using %d of %d context tokens; older messages may be "
                "dropped. Raise ollama.num_ctx or start a new conversation.",
                prompt_tokens,
                self.settings.num_ctx,
            )
        message = response.message
        self.messages.append(message)
        calls = [
            PendingCall(
                id=str(i),
                name=call.function.name,
                arguments=dict(call.function.arguments),
            )
            for i, call in enumerate(message.tool_calls or [])
        ]
        return Reply(answer=message.content or "", calls=calls)

    def add_results(self, results: list[tuple[PendingCall, str]]) -> None:
        for call, result in results:
            self.messages.append(
                {"role": "tool", "tool_name": call.name, "content": result}
            )
