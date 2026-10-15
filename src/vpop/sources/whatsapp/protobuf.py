"""
Just enough of the protobuf wire format to read WhatsApp's backup headers and increments,
which have no published schema: fields are read by number, without a `.proto`.
"""

from collections.abc import Iterator

VARINT = 0
FIXED64 = 1
LENGTH_DELIMITED = 2
FIXED32 = 5

Value = int | bytes


def varint(data: bytes, pos: int) -> tuple[int, int]:
    """The varint at `pos`, and the position after it."""
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("protobuf varint runs past the end of the data")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def fields(data: bytes) -> Iterator[tuple[int, Value]]:
    """
    Each field of a protobuf message as (field number, value): an int for varints, bytes
    for everything else. Nested messages are bytes to read with `fields` again.
    """
    pos = 0
    while pos < len(data):
        tag, pos = varint(data, pos)
        number, wire_type = tag >> 3, tag & 7
        value: Value
        if wire_type == VARINT:
            value, pos = varint(data, pos)
        elif wire_type == LENGTH_DELIMITED:
            size, pos = varint(data, pos)
            value, pos = data[pos : pos + size], pos + size
        elif wire_type == FIXED64:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire_type == FIXED32:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        if pos > len(data):
            raise ValueError("protobuf field runs past the end of the data")
        yield number, value


def first(data: bytes, number: int) -> Value | None:
    """The first value of field `number`, or None if the message doesn't have it."""
    return next((value for n, value in fields(data) if n == number), None)


def first_bytes(data: bytes, number: int) -> bytes:
    """The first bytes value of field `number`, or empty."""
    value = first(data, number)
    return value if isinstance(value, bytes) else b""


def first_text(data: bytes, number: int) -> str:
    """The first string value of field `number`, or empty."""
    return first_bytes(data, number).decode("utf-8", errors="replace")


def first_int(data: bytes, number: int) -> int:
    """The first varint value of field `number`, or 0."""
    value = first(data, number)
    return value if isinstance(value, int) else 0
