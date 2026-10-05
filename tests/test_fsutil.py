"""Tests for the file helpers."""

import stat
from pathlib import Path

import pytest

from vpop.fsutil import atomic_write, read_env_file, write_private


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_atomic_write_creates_parents_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.txt"
    atomic_write(path, "one")
    atomic_write(path, "two")
    assert path.read_text() == "two"
    assert [p.name for p in path.parent.iterdir()] == ["b.txt"]


def test_atomic_write_keeps_existing_mode(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("x")
    path.chmod(0o640)
    atomic_write(path, "y")
    assert mode(path) == 0o640


def test_failed_write_keeps_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f"
    path.write_text("old")

    def boom(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


def test_write_private_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("x")
    path.chmod(0o644)
    write_private(path, "y")
    assert mode(path) == 0o600


def test_read_env_file(tmp_path: Path) -> None:
    path = tmp_path / "x.env"
    path.write_text(
        "# comment\n\nA=1\n B = two words \nC='quoted'\nD=\"dq\"\nE='mismatched\"\n"
        "F=a=b\nnot a pair\n"
    )
    assert read_env_file(path) == {
        "A": "1",
        "B": "two words",
        "C": "quoted",
        "D": "dq",
        "E": "'mismatched\"",
        "F": "a=b",
    }
    assert read_env_file(tmp_path / "missing.env") == {}
