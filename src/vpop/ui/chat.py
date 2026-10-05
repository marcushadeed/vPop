"""
The chat: a header, then a loop of reading a line and either running a `/command` or asking
the question, with its tool calls and answer shown as they arrive. `answer_once` shows one
question the same way, for `vpop ask "..."` at a terminal.
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from vpop.assistant.conversation import Conversation, Listener, ToolCall
from vpop.assistant.errors import report_failures
from vpop.config import Config
from vpop.db import message_summary
from vpop.paths import history_path
from vpop.ui import render
from vpop.ui.editor import Editor

ALIASES = {"/clear": "/new", "/quit": "/exit"}


def session_cost(conversation: Conversation) -> float | None:
    """
    What the conversation has cost so far: Claude's estimate, when every response had a
    price, else None. Read by name, so a local-only setup never imports the Claude harness.
    """
    cost = getattr(conversation, "cost", None)
    if cost is None or getattr(conversation, "unpriced", None):
        return None
    return float(cost)


class QuestionView(Listener):
    """
    One question's progress. While it runs, a spinner shows how long it has taken, under
    any text streaming in; tool calls and finished text are printed for good as they come.
    """

    def __init__(self, console: Console, after_question: bool) -> None:
        """`after_question`: the question is on the line above, so leave a gap after it."""
        self.console = console
        self.start = time.monotonic()
        # Streamed text not printed for good yet.
        self.pending = ""
        # What was printed last ("question", "tool" or "text"), to put gaps between kinds.
        self.last = "question" if after_question else ""
        self.spinner = Spinner("dots", style="accent")
        self.live = Live(self, console=console, transient=True, refresh_per_second=12)

    @property
    def elapsed(self) -> float:
        """Seconds since the question was asked."""
        return time.monotonic() - self.start

    def __rich__(self) -> RenderableType:
        """The live part: the streamed text so far, and the spinner."""
        took = render.duration(self.elapsed)
        self.spinner.update(
            text=Text(f"Thinking… ({took} · ctrl+c to cancel)", style="muted")
        )
        if self.pending:
            return Group(render.Tail(render.answer(self.pending)), self.spinner)
        return self.spinner

    def text(self, delta: str) -> None:
        self.pending += delta

    def tool_call(self, call: ToolCall) -> None:
        self.commit()
        self.show("tool", render.tool_call(call))

    def commit(self, text: str | None = None) -> None:
        """Print the streamed text for good, or `text` in its place, and start afresh."""
        text = (self.pending if text is None else text).strip()
        self.pending = ""
        if text:
            self.show("text", render.answer(text))

    def show(self, kind: str, renderable: RenderableType) -> None:
        """Print a block, with a blank line before it unless it continues the last one."""
        if self.last and self.last != kind:
            self.console.print()
        self.console.print(renderable)
        self.last = kind


def ask(
    conversation: Conversation, question: str, console: Console, *, after_question: bool
) -> None:
    """
    Ask `question`, showing its progress, then the answer with how long it took and what
    it cost. If it fails, the text streamed so far stays on screen and the error is raised.
    """
    view = QuestionView(console, after_question)
    before = session_cost(conversation)
    conversation.listener = view
    try:
        with view.live:
            try:
                answer = conversation.ask(question)
            except BaseException:
                view.commit()
                raise
            # The returned answer replaces the preview: it can differ from what streamed
            # (a refusal, a note that the answer was cut off, the round limit).
            view.commit(answer)
    finally:
        conversation.listener = Listener()
    after = session_cost(conversation)
    cost = after - before if before is not None and after is not None else None
    console.print(render.stats(view.elapsed, cost))


class Chat:
    """The interactive chat over one conversation at a time."""

    def __init__(
        self,
        conversation: Conversation,
        config: Config,
        new_conversation: Callable[[], Conversation],
        console: Console,
        read: Callable[[], str] | None = None,
    ) -> None:
        """
        `new_conversation` makes the conversation `/new` switches to. `read` returns the
        next line typed, raising `EOFError` to quit; by default it's the input box.
        """
        self.conversation = conversation
        self.new_conversation = new_conversation
        self.console = console
        self.model = render.model_label(config)
        self.read = read or Editor(self.status, history_path()).read

    def status(self) -> tuple[str, str]:
        """The footer: which model answers, and what this conversation has used."""
        conversation = self.conversation
        return self.model, render.usage(conversation.trace, session_cost(conversation))

    def run(self) -> None:
        """
        Run until the user quits. Ctrl+C, or a failure the user can act on (see
        `describe_error`), ends only that question; rejected credentials and unexpected
        errors end the chat by raising.
        """
        self.console.print(render.header(*message_summary(self.conversation.tools.db)))
        self.console.print()
        while True:
            try:
                line = self.read().strip()
            except EOFError:
                return
            if line.startswith("/"):
                if not self.command(line):
                    return
            else:
                self.attempt(self.question, line)
            self.console.print()

    def command(self, line: str) -> bool:
        """Run a slash command. Returns False to quit."""
        name = ALIASES.get(line, line)
        if name == "/exit":
            return False
        if name == "/help":
            self.console.print(render.help_text())
        elif name == "/new":
            self.attempt(self.start_over)
        else:
            self.console.print(
                Text(f"  unknown command {line}; /help lists them", style="warning")
            )
        return True

    def question(self, text: str) -> None:
        """Ask a question in the current conversation."""
        ask(self.conversation, text, self.console, after_question=True)

    def start_over(self) -> None:
        """Switch to a new conversation."""
        self.conversation = self.new_conversation()
        self.console.print(Text("  Started a new conversation.", style="muted"))

    def attempt(self, action: Callable[..., None], *args: str) -> None:
        """Run `action`, reporting an interruption or a failure the user can act on."""
        with report_failures(self.report):
            action(*args)

    def report(self, message: str | None) -> None:
        """Show that an action was interrupted (`message` None), or why it failed."""
        if message is None:
            self.console.print(Text("  ⎿  Interrupted", style="warning"))
        else:
            self.console.print(Text(f"error: {message}", style="error"))


class ConsoleHandler(logging.Handler):
    """
    Prints log records through the chat's console, so they don't break up the spinner:
    warnings in yellow, errors in red, the rest muted. The assistant's progress lines (tool
    calls, usage) are dropped, because the chat shows those itself.
    """

    def __init__(self, console: Console) -> None:
        super().__init__()
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == logging.INFO and record.name.startswith("vpop.assistant"):
            return
        if record.levelno >= logging.ERROR:
            style, prefix = "error", "error: "
        elif record.levelno >= logging.WARNING:
            style, prefix = "warning", "warning: "
        else:
            style, prefix = "muted", ""
        self.console.print(Text(prefix + self.format(record), style=style))


@contextmanager
def routed_logs(console: Console) -> Iterator[None]:
    """Print vpop's log records through `console` for the length of the block."""
    logger = logging.getLogger("vpop")
    handlers = logger.handlers[:]
    logger.handlers[:] = [ConsoleHandler(console)]
    try:
        yield
    finally:
        logger.handlers[:] = handlers


def run_chat(
    conversation: Conversation,
    config: Config,
    new_conversation: Callable[[], Conversation],
) -> None:
    """The interactive chat, at the terminal."""
    console = render.make_console()
    with routed_logs(console):
        Chat(conversation, config, new_conversation, console).run()


def answer_once(conversation: Conversation, question: str) -> None:
    """Answer one question at the terminal, shown as the chat shows it."""
    console = render.make_console()
    with routed_logs(console):
        ask(conversation, question, console, after_question=False)
