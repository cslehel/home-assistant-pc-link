"""A native Windows system-tray icon showing the service state.

Implemented directly on Shell_NotifyIcon through pywin32 so the agent needs no
extra GUI dependency (fewer moving parts, smaller antivirus surface).  The icon
runs its own message loop on a dedicated thread; the agent updates the tooltip
and balloon text through thread-safe calls.  On non-Windows or when pywin32 is
missing it becomes a no-op so the rest of the agent still runs.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

# tray state -> (tooltip prefix, colour for the generated icon)
STATE_COLORS = {
    "ok": (0x2E, 0xA0, 0x43),         # green - connected, time remaining
    "warn": (0xE6, 0xA2, 0x17),       # amber - little time left
    "blocked": (0xC0, 0x39, 0x2B),    # red   - out of time / paused
    "offline": (0x8A, 0x8A, 0x8A),    # grey  - not connected to service
}


class TrayIcon:
    def __init__(self, tooltip: str = "Home Assistant PC Link", on_quit: Optional[Callable[[], None]] = None,
                 on_open: Optional[Callable[[], None]] = None, allow_quit: bool = False):
        self.tooltip = tooltip
        self._on_quit = on_quit
        self._on_open = on_open
        self._allow_quit = allow_quit
        self._state = "offline"
        self._detail = "Starting..."
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._enabled = _IS_WIN
        self._ready = threading.Event()
        self._icons: dict = {}
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if not self._enabled:
            self._ready.set()
            return
        try:
            import win32gui  # noqa: F401  # fail fast if pywin32 missing
        except Exception as exc:  # noqa: BLE001
            log.info("tray icon disabled (pywin32 missing): %s", exc)
            self._enabled = False
            self._ready.set()
            return
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if not self._enabled or self._hwnd is None:
            return
        try:
            import win32gui

            win32gui.PostMessage(self._hwnd, self._WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            pass

    def update(self, state: str, tooltip: Optional[str] = None, detail: Optional[str] = None) -> None:
        with self._lock:
            self._state = state if state in STATE_COLORS else "offline"
            if tooltip:
                self.tooltip = tooltip
            if detail is not None:
                self._detail = detail
        if self._enabled and self._hwnd is not None:
            try:
                import win32gui

                win32gui.PostMessage(self._hwnd, self._WM_UPDATE, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def notify(self, title: str, message: str, level: str = "info") -> None:
        """Show a balloon notification from the tray."""
        if not self._enabled or self._hwnd is None:
            return
        with self._lock:
            self._balloon = (title, message, level)
        try:
            import win32gui

            win32gui.PostMessage(self._hwnd, self._WM_BALLOON, 0, 0)
        except Exception:  # noqa: BLE001
            pass

    # -- message loop --------------------------------------------------------
    def _run(self) -> None:
        import win32api
        import win32con
        import win32gui

        self._WM_TRAY = win32con.WM_USER + 20
        self._WM_UPDATE = win32con.WM_USER + 21
        self._WM_BALLOON = win32con.WM_USER + 22
        self._WM_CLOSE = win32con.WM_CLOSE
        self._win32gui = win32gui
        self._win32con = win32con
        self._balloon: Optional[tuple] = None
        self.ID_OPEN, self.ID_STATUS, self.ID_QUIT = 1023, 1024, 1025

        message_map = {
            self._WM_TRAY: self._on_tray,
            self._WM_UPDATE: lambda h, m, w, l: self._refresh(),
            self._WM_BALLOON: lambda h, m, w, l: self._show_balloon(),
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_CLOSE: lambda h, m, w, l: win32gui.DestroyWindow(h),
        }
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = "PCLinkTrayWindow"
        wc.lpfnWndProc = message_map
        try:
            class_atom = win32gui.RegisterClass(wc)
        except win32gui.error:
            class_atom = wc.lpszClassName
        self._hwnd = win32gui.CreateWindow(class_atom, "PCLink", win32con.WS_OVERLAPPED, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        win32gui.UpdateWindow(self._hwnd)
        self._add_icon()
        self._ready.set()
        win32gui.PumpMessages()

    def _icon_handle(self, state: str):
        if state in self._icons:
            return self._icons[state]
        handle = self._make_icon(STATE_COLORS.get(state, STATE_COLORS["offline"]))
        self._icons[state] = handle
        return handle

    def _make_icon(self, rgb):
        """Draw a small filled circle as the tray icon (no external .ico needed)."""
        import win32con
        import win32gui
        import win32ui

        width = height = 16
        r, g, b = rgb
        colour = win32api_rgb(r, g, b)
        hdc = win32gui.GetDC(0)
        dc = win32ui.CreateDCFromHandle(hdc)
        mem_dc = dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(dc, width, height)
        mem_dc.SelectObject(bmp)
        mem_dc.FillSolidRect((0, 0, width, height), win32api_rgb(0, 0, 0))
        brush = win32gui.CreateSolidBrush(colour)
        pen = win32gui.CreatePen(win32con.PS_SOLID, 1, colour)
        old_brush = win32gui.SelectObject(mem_dc.GetSafeHdc(), brush)
        old_pen = win32gui.SelectObject(mem_dc.GetSafeHdc(), pen)
        win32gui.Ellipse(mem_dc.GetSafeHdc(), 1, 1, width - 1, height - 1)
        win32gui.SelectObject(mem_dc.GetSafeHdc(), old_brush)
        win32gui.SelectObject(mem_dc.GetSafeHdc(), old_pen)
        win32gui.DeleteObject(brush)
        win32gui.DeleteObject(pen)
        # build the HICON via a mask
        info = self._make_icon_info(bmp, width, height)
        hicon = win32gui.CreateIconIndirect(info)
        win32gui.ReleaseDC(0, hdc)
        mem_dc.DeleteDC()
        dc.DeleteDC()
        return hicon

    def _make_icon_info(self, colour_bmp, width, height):
        import win32gui
        import win32ui

        mask = win32ui.CreateBitmap()
        mask.CreateCompatibleBitmap(win32ui.CreateDCFromHandle(win32gui.GetDC(0)), width, height)
        return (True, 0, 0, mask.GetHandle(), colour_bmp.GetHandle())

    def _add_icon(self) -> None:
        import win32gui

        with self._lock:
            state, tip = self._state, self._tooltip_text()
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self._hwnd, 0, flags, self._WM_TRAY, self._icon_handle(state), tip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except Exception as exc:  # noqa: BLE001
            log.debug("Shell_NotifyIcon ADD failed: %s", exc)

    def _refresh(self) -> None:
        import win32gui

        with self._lock:
            state, tip = self._state, self._tooltip_text()
        flags = win32gui.NIF_ICON | win32gui.NIF_TIP
        nid = (self._hwnd, 0, flags, self._WM_TRAY, self._icon_handle(state), tip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:  # noqa: BLE001
            pass

    def _show_balloon(self) -> None:
        import win32gui

        with self._lock:
            balloon = self._balloon
            state = self._state
        if not balloon:
            return
        title, message, level = balloon
        info_flags = {"critical": win32gui.NIIF_ERROR, "warning": win32gui.NIIF_WARNING}.get(level, win32gui.NIIF_INFO)
        nid = (self._hwnd, 0, win32gui.NIF_INFO, self._WM_TRAY, self._icon_handle(state),
               self._tooltip_text(), message[:255], 10, title[:63], info_flags)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:  # noqa: BLE001
            pass

    def _tooltip_text(self) -> str:
        return f"{self.tooltip}\n{self._detail}"[:127]

    def _on_tray(self, hwnd, msg, wparam, lparam):
        import win32con
        import win32gui

        if lparam in (win32con.WM_LBUTTONDBLCLK, win32con.WM_LBUTTONUP):
            if self._on_open:
                self._on_open()
            else:
                self._popup_menu()
        elif lparam == win32con.WM_RBUTTONUP:
            self._popup_menu()
        return True

    def _popup_menu(self):
        import win32gui

        menu = win32gui.CreatePopupMenu()
        with self._lock:
            detail = self._detail
        win32gui.AppendMenu(menu, self._win32con.MF_STRING | self._win32con.MF_GRAYED, self.ID_STATUS, detail[:80] or "PC Link")
        win32gui.AppendMenu(menu, self._win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, self._win32con.MF_STRING, self.ID_OPEN, "Time remaining")
        if self._allow_quit:
            win32gui.AppendMenu(menu, self._win32con.MF_STRING, self.ID_QUIT, "Exit")
        pos = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self._hwnd)
        win32gui.TrackPopupMenu(menu, self._win32con.TPM_LEFTALIGN, pos[0], pos[1], 0, self._hwnd, None)
        win32gui.PostMessage(self._hwnd, self._win32con.WM_NULL, 0, 0)

    def _on_command(self, hwnd, msg, wparam, lparam):
        ident = wparam & 0xFFFF
        if ident in (self.ID_OPEN, self.ID_STATUS) and self._on_open:
            self._on_open()
        elif ident == self.ID_QUIT and self._on_quit:
            self._on_quit()
        return True

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        import win32gui

        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._hwnd, 0))
        except Exception:  # noqa: BLE001
            pass
        win32gui.PostQuitMessage(0)
        return True


def win32api_rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)
