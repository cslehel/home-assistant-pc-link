"""Write pclink.ico (a monitor showing a clock, on a green tile) with the standard library only.

Usage: python packaging/make_icon.py [output.ico]
       python packaging/make_icon.py --brand custom_components/pclink/brand

The icon matches the tray icon's "OK" colour.  Each size is drawn with 4x4
supersampling for smooth edges and stored as a 32-bit BGRA DIB, which every
Windows version reads.
"""
from __future__ import annotations

import math
import os
import struct
import sys

SIZES = (16, 24, 32, 48, 64, 128, 256)
GREEN = (0x2E, 0xA0, 0x43)
WHITE = (0xFF, 0xFF, 0xFF)
SS = 4  # supersampling factor
PNG_SIGNATURE = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))


def _in_round_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, radius: float) -> bool:
    """Inside the rectangle (x0, y0)-(x1, y1) with rounded corners."""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return math.hypot(x - cx, y - cy) <= radius


def _near_segment(x: float, y: float, ax: float, ay: float, bx: float, by: float, width: float) -> bool:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (ax + t * dx), y - (ay + t * dy)) <= width


def _pixel(x: float, y: float) -> tuple[tuple[int, int, int], float]:
    """Colour and coverage at a point in unit coordinates (-1..1, y down).

    A green rounded tile with a white monitor on it; the screen shows a clock
    (hour hand towards 10 o'clock, minute hand towards 12)."""
    if not _in_round_rect(x, y, -0.96, -0.96, 0.96, 0.96, 0.36):
        return GREEN, 0.0
    # monitor: frame, stand and foot
    in_frame = _in_round_rect(x, y, -0.68, -0.64, 0.68, 0.30, 0.12)
    in_screen = _in_round_rect(x, y, -0.56, -0.52, 0.56, 0.18, 0.05)
    if in_frame and not in_screen:
        return WHITE, 1.0
    if (-0.11 <= x <= 0.11 and 0.30 <= y <= 0.50) or _in_round_rect(x, y, -0.36, 0.48, 0.36, 0.62, 0.07):
        return WHITE, 1.0
    if in_screen:
        cx, cy = 0.0, -0.15
        hour_angle = math.radians(-60)  # 10 o'clock
        if (_near_segment(x, y, cx, cy, cx, cy - 0.29, 0.055)
                or _near_segment(x, y, cx, cy, cx + 0.24 * math.sin(hour_angle), cy - 0.24 * math.cos(hour_angle), 0.06)):
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


def build_png(size: int) -> bytes:
    """The same drawing as a PNG (RGBA), for the Home Assistant integration's brand images."""
    import zlib

    bgra = render(size)
    stride = size * 4
    raw = bytearray()
    for row in range(size - 1, -1, -1):  # render() is bottom-up
        line = bgra[row * stride:(row + 1) * stride]
        raw.append(0)
        for i in range(0, stride, 4):
            raw += bytes((line[i + 2], line[i + 1], line[i], line[i + 3]))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def write_brand(folder: str) -> None:
    """icon / logo in the sizes Home Assistant expects (custom_components/pclink/brand)."""
    os.makedirs(folder, exist_ok=True)
    for size, names in ((256, ("icon.png", "logo.png")), (512, ("icon@2x.png", "logo@2x.png"))):
        data = build_png(size)
        for name in names:
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(data)
            print(f"wrote {os.path.join(folder, name)}")


def main(argv: list[str]) -> int:
    if len(argv) > 2 and argv[1] == "--brand":
        write_brand(argv[2])
        return 0
    target = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "pclink.ico")
    with open(target, "wb") as fh:
        fh.write(build_ico())
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
