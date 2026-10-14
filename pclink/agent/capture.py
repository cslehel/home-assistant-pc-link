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
MAX_CAPTURE_WIDTH = 3840   # widest image taken (0 / "full size" means this): bounds memory and time


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


class CaptureError(Exception):
    """The screen could not be read; the text is what the parent is told."""


MIN_WIDTH = 320            # below this a screenshot shows nothing; give up instead
MIN_JPEG_QUALITY = 30
QUALITY_STEP = 15
MAX_ATTEMPTS = 10
DEFAULT_LINE_LIMIT = 8 * 1024 * 1024   # ipc.MAX_LINE_LARGE; the agent passes the real value


def image_budget(line_limit: int) -> int:
    """Bytes an image may have to travel as one base64 JSON line of ``line_limit`` bytes (pure).

    Base64 makes four characters of three bytes; 4 kB are left for the rest of
    the message and a tenth as margin.
    """
    return max(0, int((int(line_limit) - 4096) * 3 // 4 * 0.9))


def fit_capture(grab, encode, max_bytes: int, width: int, fmt: str = "jpeg", quality: int = 60) -> dict[str, Any]:
    """Shrink a screenshot until it fits ``max_bytes`` (pure loop; unit-tested with fakes).

    ``grab(width)`` returns ``(rgb, width, height)``; ``encode(rgb, width, height,
    fmt, quality)`` returns ``(data, actual_format)``.  JPEG first gives up
    quality (cheap: the same pixels are encoded again), then - like PNG from
    the start - the picture is taken again smaller.  Returns ``{data, format,
    width, height}`` or ``{error}`` when even the smallest useful size is too big.
    """
    quality = int(quality)
    rgb, w, h = grab(int(width))
    size = 0
    for _ in range(MAX_ATTEMPTS):
        data, actual = encode(rgb, w, h, fmt, quality)
        size = len(data)
        if size <= max_bytes:
            return {"data": data, "format": actual, "width": w, "height": h}
        if actual == "jpeg" and quality > MIN_JPEG_QUALITY:
            quality = max(MIN_JPEG_QUALITY, quality - QUALITY_STEP)
            continue
        # the size goes roughly with the area: aim a little below the limit, at least a quarter fewer pixels
        factor = min(0.85, (max_bytes / float(size)) ** 0.5 * 0.9) if size else 0.5
        smaller = int(w * factor)
        if smaller < MIN_WIDTH or smaller >= w:
            break
        rgb, w, h = grab(smaller)
    return {"error": f"the screenshot does not fit the {max_bytes // 1024} kB that can be sent "
                     f"({size // 1024} kB at {w} px); lower screenshots.max_width or use jpeg"}


def _gdi():
    """Private ``user32`` / ``gdi32`` with full prototypes: handles are pointers, not 32-bit ints."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    HDC, HGDIOBJ = wintypes.HANDLE, wintypes.HANDLE
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [HDC]
    gdi32.CreateCompatibleDC.restype = HDC
    gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = HGDIOBJ
    gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
    gdi32.SelectObject.restype = HGDIOBJ
    gdi32.SetStretchBltMode.argtypes = [HDC, ctypes.c_int]
    gdi32.SetStretchBltMode.restype = ctypes.c_int
    gdi32.SetBrushOrgEx.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi32.SetBrushOrgEx.restype = wintypes.BOOL
    gdi32.StretchBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [HDC, HGDIOBJ, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    return user32, gdi32


class _PerMonitorDpi:
    """Real pixels for the calling thread only, for the duration of a capture.

    (``SetProcessDPIAware`` used to be called on every screenshot: it changes
    the whole process, and that after the tray window already exists.)
    """

    def __init__(self, user32):
        self._user32 = user32
        self._old = None

    def __enter__(self):
        try:
            fn = self._user32.SetThreadDpiAwarenessContext   # Windows 10 1607+
            fn.argtypes = [wintypes.HANDLE]
            fn.restype = wintypes.HANDLE
            self._old = fn(wintypes.HANDLE(-4))               # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        except Exception:  # noqa: BLE001 - older Windows: the picture is taken at the scaled size
            self._old = None
        return self

    def __exit__(self, *exc):
        if self._old:
            try:
                self._user32.SetThreadDpiAwarenessContext(self._old)
            except Exception:  # noqa: BLE001
                pass
        return False


def _grab(user32, gdi32, max_width: int) -> tuple[bytearray, int, int]:
    """The virtual desktop scaled to ``max_width`` as tight RGB rows; every GDI object is released."""
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        raise CaptureError("no display in this session")
    max_width = MAX_CAPTURE_WIDTH if not max_width or max_width <= 0 else min(int(max_width), MAX_CAPTURE_WIDTH)
    scale = min(1.0, float(max_width) / w)
    tw, th = max(1, int(w * scale)), max(1, int(h * scale))
    hdc = user32.GetDC(None)
    if not hdc:
        raise CaptureError("the screen is not accessible")
    mem = bmp = old = None
    try:
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, tw, th) if mem else None
        if not mem or not bmp:
            raise CaptureError("not enough memory for the screenshot")
        old = gdi32.SelectObject(mem, bmp)
        gdi32.SetStretchBltMode(mem, HALFTONE)
        gdi32.SetBrushOrgEx(mem, 0, 0, None)
        if not gdi32.StretchBlt(mem, 0, 0, tw, th, hdc, x, y, w, h, SRCCOPY | CAPTUREBLT):
            raise CaptureError("StretchBlt failed (secure desktop or no access)")
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = tw
        bmi.bmiHeader.biHeight = -th  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = BI_RGB
        stride = (tw * 3 + 3) & ~3
        buf = ctypes.create_string_buffer(stride * th)
        # the bitmap must not be selected into a DC while GetDIBits reads it
        gdi32.SelectObject(mem, old)
        old = None
        lines = gdi32.GetDIBits(mem, bmp, 0, th, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if lines != th:
            raise CaptureError("GetDIBits failed")
        return bgr_rows_to_rgb(buf.raw, tw, th, stride), tw, th
    finally:
        if mem and old:
            gdi32.SelectObject(mem, old)
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)


def capture_screen(max_width: int = 1280, fmt: str = "jpeg", quality: int = 60,
                   max_bytes: int | None = None) -> dict[str, Any]:
    """Grab the virtual desktop.  Returns {data, format, width, height} or {error}.

    The result never exceeds ``max_bytes`` (default: what fits one IPC line):
    it is compressed harder and then taken smaller until it does.
    """
    if not _IS_WIN:
        return {"error": "screenshots are only available on Windows"}
    if max_bytes is None:
        max_bytes = image_budget(DEFAULT_LINE_LIMIT)
    try:
        user32, gdi32 = _gdi()
        with _PerMonitorDpi(user32):
            return fit_capture(lambda width: _grab(user32, gdi32, width), encode_image, int(max_bytes),
                               int(max_width), fmt, quality)
    except CaptureError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.warning("screenshot failed: %s", exc)
        return {"error": str(exc)}
