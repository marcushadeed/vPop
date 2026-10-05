"""Tests for the chat's input box, typed into through a pipe instead of a terminal."""

import io
import re
import threading
from pathlib import Path

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from vpop.ui.editor import Editor

ENTER, ALT_ENTER, CTRL_C, CTRL_D, TAB = "\r", "\x1b\r", "\x03", "\x04", "\t"
UP, LEFT = "\x1b[A", "\x1b[D"


class Screen:
    """Where the editor draws, kept as plain text with the escape codes removed."""

    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.output = Vt100_Output(
            self.stream, lambda: Size(rows=20, columns=60), enable_cpr=False
        )

    @property
    def text(self) -> str:
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", self.stream.getvalue())


def type_into(
    keys: str,
    *,
    status: tuple[str, str] = ("model-x", ""),
    history: Path | None = None,
    screen: Screen | None = None,
) -> str:
    """
    Type `keys` into a fresh editor and return what `read()` gives back. The input is
    closed after a few seconds, so a key that should end the read can't hang the test.
    """
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        editor = Editor(
            lambda: status,
            history,
            term_input=pipe,
            term_output=(screen or Screen()).output,
        )
        timeout = threading.Timer(5, pipe.close)
        timeout.start()
        try:
            return editor.read()
        finally:
            timeout.cancel()


def test_enter_sends_the_line() -> None:
    assert type_into(f"hello{ENTER}") == "hello"


def test_enter_on_a_blank_line_does_nothing() -> None:
    assert type_into(f"{ENTER}hi{ENTER}") == "hi"


def test_alt_enter_adds_a_line() -> None:
    assert type_into(f"one{ALT_ENTER}two{ENTER}") == "one\ntwo"


def test_ctrl_c_clears_the_line() -> None:
    assert type_into(f"oops{CTRL_C}fine{ENTER}") == "fine"


def test_ctrl_c_twice_on_an_empty_line_quits() -> None:
    with pytest.raises(EOFError):
        type_into(f"{CTRL_C}{CTRL_C}not read{ENTER}")


def footer_text(editor: Editor) -> str:
    """The footer as it would be drawn now, without its styles."""
    return "".join(fragment[1] for fragment in editor.footer())


def test_a_pending_quit_shows_in_the_footer() -> None:
    editor = Editor(lambda: ("model-x", "↑1 ↓2"))
    assert "↑1 ↓2" in footer_text(editor)
    editor.quit_pending = True
    assert footer_text(editor).rstrip().endswith("press ctrl+c again to quit")
    assert "↑1 ↓2" not in footer_text(editor)


def test_typing_cancels_a_pending_quit() -> None:
    # The second ctrl+c clears "a"; the third only asks again.
    assert type_into(f"{CTRL_C}a{CTRL_C}{CTRL_C}b{ENTER}") == "b"


def test_ctrl_d_on_an_empty_line_quits() -> None:
    with pytest.raises(EOFError):
        type_into(f"{CTRL_D}not read{ENTER}")


def test_ctrl_d_with_text_deletes_forward() -> None:
    assert type_into(f"ab{LEFT}{CTRL_D}{ENTER}") == "a"


def test_tab_completes_a_command() -> None:
    assert type_into(f"/he{TAB}{ENTER}") == "/help"


def test_typing_a_slash_lists_the_commands() -> None:
    editor = Editor(lambda: ("model-x", ""))
    editor.buffer.text = "/n"
    assert (
        footer_text(editor).strip() == "/new   start a new conversation (also /clear)"
    )
    editor.buffer.text = "/"
    assert footer_text(editor).count("\n") == 2  # one line per command


def test_footer_shows_the_status() -> None:
    screen = Screen()
    type_into(f"q{ENTER}", status=("model-x", "↑1 ↓2"), screen=screen)
    assert re.search(r"model-x +↑1 ↓2", screen.text)


def test_history_is_kept_between_sessions(tmp_path: Path) -> None:
    history = tmp_path / "data" / "history"
    assert type_into(f"first{ENTER}", history=history) == "first"
    assert type_into(f"{UP}{ENTER}", history=history) == "first"
