"""Write pclink.ico (a green clock face) with the standard library only.

Usage: python packaging/make_icon.py [output.ico]

The icon matches the tray icon's "OK" colour.  Each size is drawn with 4x4
supersampling for smooth edges and stored as a 32-bit BGRA DIB, which every
Windows version reads.
"""
from __future__ import annotations

import math
import os
import struct
import sys

SIZES = (16, 24, 32, 48, 64, 128)
GREEN = (0x2E, 0xA0, 0x43)
WHITE = (0xFF, 0xFF, 0xFF)
SS = 4  # supersampling factor


def _pixel(x: float, y: float) -> tuple[tuple[int, int, int], float]:
    """Colour and coverage at a point in unit coordinates (-1..1, y down)."""
    r = math.hypot(x, y)
    if r > 0.96:
        return GREEN, 0.0
    # white clock hands: hour hand towards 10 o'clock, minute hand towards 12
    def near_segment(ax, ay, bx, by, width):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - (ax + t * dx), y - (ay + t * dy)) <= width

    hour_angle = math.radians(-60)  # 10 o'clock
    if near_segment(0, 0, 0, -0.62, 0.075) or near_segment(0, 0, 0.45 * math.sin(hour_angle), -0.45 * math.cos(hour_angle), 0.085):
        return WHITE, 1.0
    if 0.72 <= r <= 0.82:  # white ring
        return WHITE, 1.0
    return GREEN, 1.0


def render(size: int) -> bytes:
    """Bottom-up BGRA rows."""
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0.0, 0.0, 0.0]
            alpha = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    ux = ((px + (sx + 0.5) / SS) / size) * 2 - 1
                    uy = ((py + (sy + 0.5) / SS) / size) * 2 - 1
                    colour, cover = _pixel(ux, uy)
                    alpha += cover
                    for i in range(3):
                        acc[i] += colour[i] * cover
            n = SS * SS
            a = alpha / n
            if alpha:
                r, g, b = (int(round(c / alpha)) for c in acc)
            else:
                r = g = b = 0
            row += bytes((b, g, r, int(round(a * 255))))
        rows.append(bytes(row))
    return b"".join(reversed(rows))


def dib(size: int) -> bytes:
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    mask_row = ((size + 31) // 32) * 4  # 1-bpp AND mask, rows padded to 32 bits; alpha makes it unused
    return header + render(size) + b"\x00" * (mask_row * size)


def build_ico(sizes=SIZES) -> bytes:
    images = [dib(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images)


def main(argv: list[str]) -> int:
    target = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "pclink.ico")
    with open(target, "wb") as fh:
        fh.write(build_ico())
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
