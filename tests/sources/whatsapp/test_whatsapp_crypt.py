"""Tests for crypt15 decryption and the protobuf reader."""

from pathlib import Path

import pytest
from whatsapp_backups import KEY, KEY_HEX, encrypt, pb

from vpop.sources.whatsapp.crypt import DecryptError, decrypt, parse_key
from vpop.sources.whatsapp.protobuf import fields, first_int, first_text

PLAIN = b"SQLite format 3\x00" + bytes(range(256)) * 50


@pytest.mark.parametrize("feature_flag", [True, False])
@pytest.mark.parametrize("md5", [True, False])
def test_round_trip(tmp_path: Path, feature_flag: bool, md5: bool) -> None:
    src = tmp_path / "msgstore.db.crypt15"
    src.write_bytes(encrypt(PLAIN, feature_flag=feature_flag, md5=md5))
    dest = tmp_path / "msgstore.db"
    decrypt(src, KEY, dest)
    assert dest.read_bytes() == PLAIN
    assert dest.stat().st_mode & 0o777 == 0o600


def test_wrong_key_leaves_nothing_behind(tmp_path: Path) -> None:
    src = tmp_path / "msgstore.db.crypt15"
    src.write_bytes(encrypt(PLAIN))
    dest = tmp_path / "msgstore.db"
    with pytest.raises(DecryptError, match="key is wrong"):
        decrypt(src, bytes(32), dest)
    assert not dest.exists()


def test_damaged_file_is_refused(tmp_path: Path) -> None:
    data = bytearray(encrypt(PLAIN))
    data[200] ^= 0xFF
    src = tmp_path / "msgstore.db.crypt15"
    src.write_bytes(bytes(data))
    with pytest.raises(DecryptError):
        decrypt(src, KEY, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("data", [b"", b"\x05", b"\x03abc" + bytes(40)])
def test_not_a_crypt15_file(tmp_path: Path, data: bytes) -> None:
    src = tmp_path / "x.crypt15"
    src.write_bytes(data)
    with pytest.raises(DecryptError):
        decrypt(src, KEY, tmp_path / "out")


def test_parse_key() -> None:
    assert parse_key(f"  {KEY_HEX.upper()}\n") == KEY
    for bad in ("", KEY_HEX[:-1], KEY_HEX[:-1] + "g"):
        with pytest.raises(ValueError, match="64 hex digits"):
            parse_key(bad)


def test_protobuf_reader() -> None:
    message = pb((1, 300), (2, "hé"), (2, "second"), (3, pb((1, 7))))
    assert [n for n, _ in fields(message)] == [1, 2, 2, 3]
    assert first_int(message, 1) == 300
    assert first_text(message, 2) == "hé"
    assert first_int(message, 9) == 0
    assert first_text(message, 9) == ""


def test_truncated_protobuf_is_an_error() -> None:
    with pytest.raises(ValueError):
        list(fields(pb((2, "hello"))[:-2]))
    with pytest.raises(ValueError):
        list(fields(b"\x08\x80"))
