#!/usr/bin/env python3
"""Generate GuidePilot's application icon with no third-party libraries.

Writes a 256x256 PNG: a rounded dark panel with a bright guidance arrow and a
highlight rectangle, which is what the app draws over the user's screen.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 256
BACKGROUND = (11, 18, 32)
PANEL = (23, 33, 54)
ACCENT = (94, 234, 212)
HIGHLIGHT = (250, 204, 21)


def blend(base: tuple[int, int, int], top: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    return tuple(int(round(b + (t - b) * alpha)) for b, t in zip(base, top))


def in_rounded_rect(x: int, y: int, x0: int, y0: int, x1: int, y1: int, radius: int) -> bool:
    """True inside a rectangle with rounded corners."""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    # Clamp to the inner rectangle; only the corner regions are further away
    # than the corner radius, and those must fall inside the corner circle.
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def in_rect(x: int, y: int, x0: int, y0: int, x1: int, y1: int) -> bool:
    return x0 <= x <= x1 and y0 <= y <= y1


def pixel(x: int, y: int) -> tuple[int, int, int]:
    colour = BACKGROUND
    if in_rounded_rect(x, y, 8, 8, SIZE - 9, SIZE - 9, 34):
        colour = PANEL
    # The control being highlighted.
    if in_rounded_rect(x, y, 150, 56, 226, 96, 8):
        colour = blend(colour, HIGHLIGHT, 0.18)
    if in_rect(x, y, 150, 56, 226, 59) or in_rect(x, y, 150, 93, 226, 96) or in_rect(x, y, 150, 56, 153, 96) or in_rect(x, y, 223, 56, 226, 96):
        colour = HIGHLIGHT
    # Panel lines suggesting a form.
    for top in (140, 168, 196):
        if in_rect(x, y, 40, top, 150, top + 8):
            colour = blend(colour, (148, 163, 184), 0.55)
    # Guidance arrow pointing up at the highlighted control: a shaft plus a
    # triangular head whose apex is just below the box.
    if 112 <= y <= 190 and 182 <= x <= 196:
        colour = ACCENT
    if 104 <= y <= 132:
        half = (y - 104)  # narrow apex at the top, widening into the shaft
        if 188 - half <= x <= 188 + half:
            colour = ACCENT
    return colour


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "packaging/guidepilot.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # filter: none
        for x in range(SIZE):
            raw.extend(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    target.write_bytes(png)
    print(f"wrote {target} ({len(png)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
