"""Screenshot capture for the parent-only screenshot-on-demand feature.

Captures the whole virtual desktop of *this* session with GDI, downscaled by
``StretchBlt`` (HALFTONE) to ``max_width`` before a single ``GetDIBits`` read,
then encodes to JPEG (via Pillow when installed) or PNG (built-in encoder, no
dependencies).  Runs entirely inside the user's session; the service never
touches the desktop itself.
"""
from __future__ import annotations

import ctypes
import io
import logging
import struct
import sys
import zlib
from ctypes import wintypes
from typing import Any

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
HALFTONE = 4
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def bgr_rows_to_rgb(raw: bytes, width: int, height: int, stride: int) -> bytearray:
    """Convert padded bottom-up-or-top-down BGR rows into tight RGB rows (top-down assumed)."""
    out = bytearray(width * 3 * height)
    row_len = width * 3
    for y in range(height):
        row = raw[y * stride: y * stride + row_len]
        rgb = bytearray(row)
        rgb[0::3] = row[2::3]
        rgb[2::3] = row[0::3]
        out[y * row_len:(y + 1) * row_len] = rgb
    return out


def encode_png(rgb: bytes, width: int, height: int) -> bytes:
    """Minimal PNG writer (8-bit RGB, filter 0) - no Pillow needed."""
    row_len = width * 3
    filtered = bytearray()
    for y in range(height):
        filtered.append(0)
        filtered += rgb[y * row_len:(y + 1) * row_len]
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(filtered), 6)) + _png_chunk(b"IEND", b""))


def encode_image(rgb: bytes, width: int, height: int, fmt: str = "jpeg", quality: int = 60) -> tuple[bytes, str]:
    """Encode tight RGB rows; returns (bytes, actual_format).  JPEG needs Pillow, else PNG."""
    if fmt == "jpeg":
        try:
            from PIL import Image  # type: ignore

            img = Image.frombuffer("RGB", (width, height), bytes(rgb), "raw", "RGB", 0, 1)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=max(10, min(95, int(quality))), optimize=True)
            return buf.getvalue(), "jpeg"
        except ImportError:
            log.debug("Pillow not installed; screenshot encoded as PNG")
        except Exception as exc:  # noqa: BLE001
            log.debug("JPEG encode failed (%s); falling back to PNG", exc)
    return encode_png(rgb, width, height), "png"


def capture_screen(max_width: int = 1280, fmt: str = "jpeg", quality: int = 60) -> dict[str, Any]:
    """Grab the virtual desktop.  Returns {data, format, width, height} or {error}."""
    if not _IS_WIN:
        return {"error": "screenshots are only available on Windows"}
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        return {"error": "no display in this session"}
    scale = min(1.0, float(max_width) / w) if max_width and max_width > 0 else 1.0
    tw, th = max(1, int(w * scale)), max(1, int(h * scale))
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, tw, th)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.SetStretchBltMode(mem, HALFTONE)
        if not gdi32.StretchBlt(mem, 0, 0, tw, th, hdc, x, y, w, h, SRCCOPY | CAPTUREBLT):
            return {"error": "StretchBlt failed (secure desktop or no access)"}
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = tw
        bmi.bmiHeader.biHeight = -th  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = BI_RGB
        stride = (tw * 3 + 3) & ~3
        buf = ctypes.create_string_buffer(stride * th)
        lines = gdi32.GetDIBits(mem, bmp, 0, th, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if lines != th:
            return {"error": "GetDIBits failed"}
        rgb = bgr_rows_to_rgb(buf.raw, tw, th, stride)
        data, actual = encode_image(rgb, tw, th, fmt, quality)
        return {"data": data, "format": actual, "width": tw, "height": th}
    except Exception as exc:  # noqa: BLE001
        log.warning("screenshot failed: %s", exc)
        return {"error": str(exc)}
    finally:
        try:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(0, hdc)
        except Exception:  # noqa: BLE001
            pass
