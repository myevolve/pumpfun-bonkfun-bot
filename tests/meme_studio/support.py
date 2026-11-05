"""Shared helpers for meme_studio tests. Not collected by pytest."""
# ruff: noqa: S603, S607, TRY003

from __future__ import annotations

import json
import struct
import subprocess
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sock_drawer.concept.json"
MECHANICS = ROOT / "meme_studio" / "game" / "mechanics.js"
RUNNER = Path(__file__).resolve().parent / "run_mechanics.js"

DISCLOSURE = (
    "Parody and entertainment only. Not investment advice. "
    "This is an automated account."
)
NO_INPUT = {"jump": False, "tap": False, "left": False, "right": False}


def load_fixture_dict() -> dict[str, object]:
    """Return the sock-drawer concept payload."""
    payload: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload


def write_concept(
    tmp: Path, payload: dict[str, object], name: str = "concept.json"
) -> Path:
    """Write a concept payload and return its path."""
    path = tmp / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def tiny_png(red: int = 255, green: int = 0, blue: int = 0) -> bytes:
    """Build a valid 1x1 RGB PNG."""
    raw = b"\x00" + bytes((red, green, blue))
    return _png(1, 1, raw)


def _png(width: int, height: int, raw_scanlines: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw_scanlines))
        + chunk(b"IEND", b"")
    )


def decode_png_rgb(data: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB PNG that uses filter 0 on every row."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a png")
    pos = 8
    width = 0
    height = 0
    idat = b""
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit, color, comp, filt, inter = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (bit, color, comp, filt, inter) != (8, 2, 0, 0, 0):
                raise AssertionError("unsupported png layout")
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = width * 3
    out = bytearray()
    index = 0
    for _row in range(height):
        filt = raw[index]
        index += 1
        row = raw[index : index + stride]
        index += stride
        if filt != 0:
            raise AssertionError(f"filter {filt}")
        out += row
    return width, height, bytes(out)


def step_mechanics(fn: str, payload: dict[str, object]) -> dict[str, object]:
    """Run one mechanics.js call through node."""
    proc = subprocess.run(
        ["node", str(RUNNER), str(MECHANICS), fn, json.dumps(payload)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    result: dict[str, object] = json.loads(proc.stdout)
    return result
