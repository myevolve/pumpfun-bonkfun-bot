"""Minimal RGB PNG encoder used by the procedural fallback."""
# ruff: noqa: TC003, TRY003

from __future__ import annotations

import struct
import zlib
from pathlib import Path

_BIT_DEPTH = 8
_COLOR_RGB = 2
_COMPRESSION = 1


def write_rgb_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    """Write an 8-bit RGB PNG using filter 0 on every row.

    Args:
        path: Destination file.
        width: Image width in pixels.
        height: Image height in pixels.
        rgb: Packed RGB bytes, length ``width * height * 3``.

    Raises:
        ValueError: The buffer length does not match the dimensions.
    """
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError("rgb buffer does not match dimensions")
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        start = y * stride
        raw.extend(rgb[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", width, height, _BIT_DEPTH, _COLOR_RGB, 0, 0, 0)
    payload = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
    payload += _chunk(b"IDAT", zlib.compress(bytes(raw), _COMPRESSION))
    payload += _chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)
