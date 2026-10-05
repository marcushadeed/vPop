"""
The chat's input box: a `›` line between two rules, with a footer under it, drawn by
prompt_toolkit in the normal flow of the terminal rather than full screen. Once a line is
sent, the rules and footer are cleared and only `› question` stays in the transcript.
"""

from collections.abc import Callable
from pathlib import Path

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from vpop.ui import render

# The prompt_toolkit side of `render.THEME`.
STYLE = Style.from_dict(
    {
        "rule": "ansibrightblack",
        "prompt": "ansicyan bold",
        "muted": "ansibrightblack",
        "command": "ansicyan",
        "warning": "ansiyellow",
    }
)

QUIT_HINT = "press ctrl+c again to quit"

# The footer's left and right text: which model answers, and what the conversation used.
Status = Callable[[], tuple[str, str]]


def hidden_once_sent(window: Window) -> ConditionalContainer:
    """Show `window` only while the box is being typed in."""
    return ConditionalContainer(window, filter=Condition(lambda: not get_app().is_done))


class Editor:
    """
    Reads what the user types. `status` is asked for the footer's text each time it's
    drawn, and lines sent are kept in `history_file` when given. `term_input` and
    `term_output` stand in for the terminal in tests.
    """

    def __init__(
        self,
        status: Status,
        history_file: Path | None = None,
        *,
        term_input: Input | None = None,
        term_output: Output | None = None,
    ) -> None:
        self.status = status
        self.history: History = InMemoryHistory()
        if history_file:
            history_file.parent.mkdir(parents=True, exist_ok=True)
            self.history = FileHistory(history_file)
        self.term_input = term_input
        self.term_output = term_output
        self.buffer = Buffer()
        # Set by a ctrl+c on an empty line; a second one then quits.
        self.quit_pending = False

    def read(self) -> str:
        """
        Show the box and return the line sent (which may have several lines). Raises
        `EOFError` when the user quits, as `input()` does.
        """
        self.quit_pending = False
        self.buffer = Buffer(
            multiline=True,
            history=self.history,
            accept_handler=self.accept,
            on_text_changed=self.typed,
        )
        app: Application[str] = Application(
            layout=self.layout(),
            key_bindings=self.key_bindings(),
            style=STYLE,
            input=self.term_input,
            output=self.term_output,
        )
        return app.run()

    def layout(self) -> Layout:
        """Rule, input, rule, footer."""
        return Layout(
            HSplit(
                [
                    hidden_once_sent(Window(char="─", height=1, style="class:rule")),
                    Window(
                        BufferControl(self.buffer),
                        wrap_lines=True,
                        dont_extend_height=True,
                        get_line_prefix=self.line_prefix,
                    ),
                    hidden_once_sent(Window(char="─", height=1, style="class:rule")),
                    hidden_once_sent(
                        Window(
                            FormattedTextControl(self.footer), dont_extend_height=True
                        )
                    ),
                ]
            )
        )

    def key_bindings(self) -> KeyBindings:
        """The keys `render.KEYS` describes; everything else is prompt_toolkit's default."""
        bindings = KeyBindings()
        bindings.add("enter")(self.send)
        bindings.add("escape", "enter")(self.new_line)
        bindings.add("tab")(self.complete)
        bindings.add("c-c")(self.cancel)
        bindings.add("c-d")(self.quit)
        return bindings

    @staticmethod
    def line_prefix(line: int, wrap: int) -> StyleAndTextTuples:
        """`› ` before the first line; continuation lines are indented to match."""
        return [("class:prompt", "› ")] if line == 0 and wrap == 0 else [("", "  ")]

    def footer(self) -> StyleAndTextTuples:
        """The commands matching a `/` being typed, else the status line."""
        matches = render.slash_matches(self.buffer.text)
        if matches:
            width = max(len(name) for name in render.COMMANDS)
            lines: StyleAndTextTuples = []
            for name, about in matches:
                if lines:
                    lines.append(("", "\n"))
                lines += [
                    ("class:command", f"  {name:<{width}}  "),
                    ("class:muted", about),
                ]
            return lines
        left, right = self.status()
        style = "class:muted"
        if self.quit_pending:
            right, style = QUIT_HINT, "class:warning"
        right = f"{right}  " if right else ""
        line = render.spread(f"  {left}", right, get_app().output.get_size().columns)
        if not right or not line.endswith(right):
            return [("class:muted", line)]
        return [("class:muted", line[: -len(right)]), (style, right)]

    def accept(self, buffer: Buffer) -> bool:
        """End the read with the buffer's text, keeping it on screen."""
        get_app().exit(result=buffer.text)
        return True

    def typed(self, _buffer: Buffer) -> None:
        """Any change to the text takes back a pending quit."""
        self.quit_pending = False

    def send(self, event: KeyPressEvent) -> None:
        """Enter: send the line, unless it's blank."""
        if event.current_buffer.text.strip():
            event.current_buffer.validate_and_handle()

    @staticmethod
    def new_line(event: KeyPressEvent) -> None:
        """Alt+Enter: start a new line."""
        event.current_buffer.newline(copy_margin=False)

    @staticmethod
    def complete(event: KeyPressEvent) -> None:
        """Tab: complete a slash command when only one matches."""
        buffer = event.current_buffer
        matches = render.slash_matches(buffer.text)
        if len(matches) == 1:
            buffer.text = matches[0][0]
            buffer.cursor_position = len(buffer.text)

    def cancel(self, event: KeyPressEvent) -> None:
        """Ctrl+C: clear the line; on an empty one, ask for another, then quit."""
        if event.current_buffer.text:
            event.current_buffer.reset()
        elif self.quit_pending:
            self.leave(event)
        else:
            self.quit_pending = True

    def quit(self, event: KeyPressEvent) -> None:
        """Ctrl+D: quit on an empty line, else delete the character under the cursor."""
        if event.current_buffer.text:
            event.current_buffer.delete()
        else:
            self.leave(event)

    @staticmethod
    def leave(event: KeyPressEvent) -> None:
        """End the read with `EOFError`, clearing the box rather than leaving an empty `›`."""
        event.app.erase_when_done = True
        event.app.exit(exception=EOFError())
