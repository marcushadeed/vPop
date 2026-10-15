"""
Decrypt WhatsApp's crypt15 backups with the 64-hex-digit end-to-end backup key.

A crypt15 file is a one-byte header length, an optional `0x01` feature flag, the protobuf
header (field 3 holds the IV, in its field 1), then the zlib-compressed payload under
AES-256-GCM, its 16-byte tag, and usually an MD5 of everything before it. The AES key is
derived from the backup key with HMAC-SHA256.
"""

import hashlib
import hmac
import os
import re
import zlib
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from vpop.sources import SourceError
from vpop.sources.whatsapp.protobuf import first_bytes

TAG_BYTES = 16
MD5_BYTES = 16
FEATURE_FLAG = 0x01
HEADER_IV_FIELD = 3
CHUNK_BYTES = 1 << 20
KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class DecryptError(SourceError):
    """A backup can't be decrypted: the key is wrong, or the file is damaged."""


def parse_key(text: str) -> bytes:
    """The 32-byte backup key from its 64 hex digits; raises `ValueError` for anything else."""
    text = text.strip()
    if not KEY_PATTERN.fullmatch(text):
        raise ValueError("a WhatsApp backup key is 64 hex digits")
    return bytes.fromhex(text)


def encryption_key(backup_key: bytes) -> bytes:
    """The AES-256 key WhatsApp derives from the backup key."""
    seed = hmac.new(b"\x00" * 32, backup_key, hashlib.sha256).digest()
    return hmac.new(seed, b"backup encryption\x01", hashlib.sha256).digest()


def file_md5(path: Path, size: int) -> bytes:
    """The MD5 of the first `size` bytes of a file."""
    digest = hashlib.md5()
    with path.open("rb") as fh:
        remaining = size
        while remaining:
            chunk = fh.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.digest()


def read_header(path: Path) -> tuple[bytes, int]:
    """The IV and the offset the encrypted payload starts at."""
    with path.open("rb") as fh:
        start = fh.read(2)
        if len(start) < 2:
            raise DecryptError(f"{path.name} is too short to be a crypt15 backup")
        offset = 2 if start[1] == FEATURE_FLAG else 1
        fh.seek(offset)
        header = fh.read(start[0])
    try:
        iv = first_bytes(first_bytes(header, HEADER_IV_FIELD), 1)
    except ValueError:
        iv = b""
    if len(iv) != 16:
        raise DecryptError(f"{path.name} doesn't have a crypt15 header")
    return iv, offset + start[0]


def payload(path: Path) -> tuple[bytes, int, int, bytes]:
    """The IV, where the encrypted payload starts and ends, and its GCM tag."""
    iv, start = read_header(path)
    size = path.stat().st_size
    end = size - MD5_BYTES
    with path.open("rb") as fh:
        fh.seek(end)
        trailer = fh.read(MD5_BYTES)
    if file_md5(path, end) != trailer:
        end = size  # Some backups have no MD5 trailer.
    end -= TAG_BYTES
    if end < start:
        raise DecryptError(f"{path.name} is too short to be a crypt15 backup")
    with path.open("rb") as fh:
        fh.seek(end)
        tag = fh.read(TAG_BYTES)
    return iv, start, end, tag


def decrypt(src: Path, backup_key: bytes, dest: Path) -> None:
    """
    Decrypt and decompress `src` into `dest`, which only its owner can read. It's
    streamed, so memory stays flat however big the backup is. Raises `DecryptError` for a
    wrong key or a damaged file, leaving no `dest` behind.
    """
    iv, start, end, tag = payload(src)
    decryptor = Cipher(
        algorithms.AES(encryption_key(backup_key)), modes.GCM(iv, tag)
    ).decryptor()
    inflate = zlib.decompressobj()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as fh:
            fh.seek(start)
            remaining = end - start
            while remaining:
                chunk = fh.read(min(CHUNK_BYTES, remaining))
                remaining -= len(chunk)
                out.write(inflate.decompress(decryptor.update(chunk)))
            decryptor.finalize()
            out.write(inflate.flush())
    except (InvalidTag, zlib.error) as exc:
        dest.unlink(missing_ok=True)
        raise DecryptError(
            f"can't decrypt {src.name}: the WhatsApp backup key is wrong or the file is "
            "damaged"
        ) from exc
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
