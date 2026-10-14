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
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

MAX_QUEUED_BALLOONS = 10
BALLOON_GAP_SECONDS = 4.0   # a balloon sent while another is up replaces it; leave each one this long


ID_REQUEST_BASE = 1100      # menu ids of the "Ask for more time" entries: base + position
MAX_REQUEST_OPTIONS = 6
REQUEST_GAP_SECONDS = 5.0   # a second click this soon after a request is not sent again


def time_request_options(value) -> list[int]:
    """The amounts the service offers, cleaned up (pure; unit-tested).

    Whole minutes from 1 to 240, each once, in the order given, six at most;
    anything else in the message is ignored.  An empty list means "not offered".
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        if not -1e6 < item < 1e6 or item != int(item):   # also turns away NaN and infinity
            continue
        minutes = int(item)
        if 1 <= minutes <= 240 and minutes not in out:
            out.append(minutes)
        if len(out) >= MAX_REQUEST_OPTIONS:
            break
    return out


def request_label(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def request_menu_items(options: list[int]) -> list[tuple[int, str]]:
    """(menu id, label) of every "Ask for more time" entry (pure; unit-tested)."""
    return [(ID_REQUEST_BASE + i, request_label(m)) for i, m in enumerate(options)]


def request_minutes(ident: int, options: list[int]) -> Optional[int]:
    """The amount a clicked menu id stands for; None when it is not one of the entries shown."""
    index = ident - ID_REQUEST_BASE
    return options[index] if 0 <= index < len(options) else None


class RequestGuard:
    """Lets one request through and swallows a second click right after it (pure; unit-tested)."""

    def __init__(self, gap: float = REQUEST_GAP_SECONDS):
        self.gap = gap
        self._last: Optional[float] = None

    def allow(self, now: float) -> bool:
        if self._last is not None and 0 <= now - self._last < self.gap:
            return False
        self._last = now
        return True

    def undo(self) -> None:
        """The request could not be sent after all: the next click may try again at once."""
        self._last = None


def balloon_wait(now: float, last_shown: float, gap: float = BALLOON_GAP_SECONDS) -> float:
    """Seconds until the next queued balloon may be shown; 0 = now (pure; unit-tested)."""
    if now < last_shown:   # the clock cannot go back, but never wait for ever
        return 0.0
    return max(0.0, gap - (now - last_shown))

# tray state -> (tooltip prefix, colour for the generated icon)
STATE_COLORS = {
    "ok": (0x2E, 0xA0, 0x43),         # green - connected, time remaining
    "warn": (0xE6, 0xA2, 0x17),       # amber - little time left
    "blocked": (0xC0, 0x39, 0x2B),    # red   - out of time / paused / bedtime
    "focus": (0x25, 0x63, 0xEB),      # blue  - focus / homework mode
    "offline": (0x8A, 0x8A, 0x8A),    # grey  - not connected to service
}


class TrayIcon:
    def __init__(self, tooltip: str = "Home Assistant PC Link", on_quit: Optional[Callable[[], None]] = None,
                 on_open: Optional[Callable[[], None]] = None, allow_quit: bool = False,
                 on_time_request: Optional[Callable[[int], None]] = None):
        self.tooltip = tooltip
        self._on_quit = on_quit
        self._on_open = on_open
        self._on_time_request = on_time_request
        self._request_options: list[int] = []   # minutes offered under "Ask for more time"; empty = no such entry
        self._allow_quit = allow_quit
        self._state = "offline"
        self._detail = "Starting..."
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._enabled = _IS_WIN
        self._ready = threading.Event()
        self._icons: dict = {}
        self._lock = threading.Lock()
        # Balloons wait here for the tray thread.  One slot was not enough: a time warning
        # and "a screenshot was taken" arriving together showed only the second.
        self._balloons: deque = deque(maxlen=MAX_QUEUED_BALLOONS)
        self._balloon_shown_at = 0.0
        self._icon_added = False   # False until Shell_NotifyIcon(NIM_ADD) succeeded (Explorer may not be up yet)

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

    def set_time_requests(self, options) -> None:
        """What the "Ask for more time" menu offers (from the service's status); nothing = no menu entry."""
        cleaned = time_request_options(options)
        with self._lock:
            self._request_options = cleaned

    @property
    def can_notify(self) -> bool:
        """True when the icon is really in the notification area, so a balloon can be shown."""
        return bool(self._enabled and self._hwnd is not None and self._icon_added)

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        """Show a balloon notification from the tray.

        Returns False when the tray cannot show it (pywin32 missing, no window,
        or the icon is not in the notification area because Explorer is not
        running yet) so the caller can fall back to a message box.
        """
        if not self.can_notify:
            return False
        with self._lock:
            self._balloons.append((title, message, level))
        try:
            import win32gui

            win32gui.PostMessage(self._hwnd, self._WM_BALLOON, 0, 0)
        except Exception:  # noqa: BLE001
            return False
        return True

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
        self.ID_OPEN, self.ID_STATUS, self.ID_QUIT = 1023, 1024, 1025

        message_map = {
            self._WM_TRAY: self._on_tray,
            win32con.WM_TIMER: self._on_timer,
            self._WM_UPDATE: lambda h, m, w, l: self._refresh(),
            self._WM_BALLOON: lambda h, m, w, l: self._drain_balloons(),
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_CLOSE: lambda h, m, w, l: win32gui.DestroyWindow(h),
        }
        try:
            # Explorer broadcasts this when the taskbar is (re)created: at logon after the
            # agent started, and after every Explorer restart.  The icon must be re-added.
            self._WM_TASKBARCREATED = win32gui.RegisterWindowMessage("TaskbarCreated")
            message_map[self._WM_TASKBARCREATED] = self._on_taskbar_created
        except Exception as exc:  # noqa: BLE001
            log.debug("TaskbarCreated registration failed: %s", exc)
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
        self._start_retry_timer()
        self._ready.set()
        win32gui.PumpMessages()

    _RETRY_TIMER_ID = 1
    _RETRY_TIMER_MS = 10_000
    _BALLOON_TIMER_ID = 2

    def _set_timer(self, ident: int, milliseconds: int) -> bool:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
            user32.SetTimer.restype = ctypes.c_size_t
            return bool(user32.SetTimer(self._hwnd, ident, int(milliseconds), None))
        except Exception as exc:  # noqa: BLE001
            log.debug("tray timer unavailable: %s", exc)
            return False

    def _kill_timer(self, ident: int) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
            user32.KillTimer.restype = wintypes.BOOL
            user32.KillTimer(self._hwnd, ident)
        except Exception:  # noqa: BLE001
            pass

    def _start_retry_timer(self) -> None:
        """A slow timer that re-adds the icon while it is missing (belt and braces for TaskbarCreated)."""
        self._set_timer(self._RETRY_TIMER_ID, self._RETRY_TIMER_MS)

    def _on_timer(self, hwnd, msg, wparam, lparam):
        if wparam == self._BALLOON_TIMER_ID:
            self._kill_timer(self._BALLOON_TIMER_ID)
            self._drain_balloons()
            return 0
        if not self._icon_added:
            self._add_icon()
        return 0

    def _drain_balloons(self) -> None:
        """Show the next waiting balloon, or come back when the one on screen had its time."""
        wait = balloon_wait(time.monotonic(), self._balloon_shown_at)
        with self._lock:
            if not self._balloons:
                return
            balloon = self._balloons.popleft() if wait <= 0 else None
            more = bool(self._balloons)
        if balloon is not None:
            self._balloon_shown_at = time.monotonic()
            self._show_balloon(balloon)
            wait = BALLOON_GAP_SECONDS
        if (balloon is None or more) and not self._set_timer(self._BALLOON_TIMER_ID, int(wait * 1000) + 50):
            # no timer: better one after the other at once than not at all
            with self._lock:
                rest = list(self._balloons)
                self._balloons.clear()
            for item in rest:
                self._show_balloon(item)

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        self._icon_added = False
        self._add_icon()
        return 0

    def _icon_handle(self, state: str):
        if state in self._icons:
            return self._icons[state]
        try:
            handle = self._make_icon(STATE_COLORS.get(state, STATE_COLORS["offline"]))
        except Exception as exc:  # noqa: BLE001
            log.debug("custom tray icon failed (%s); using stock icon", exc)
            import win32con
            import win32gui

            handle = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        self._icons[state] = handle
        return handle

    def _make_icon(self, rgb):
        """Draw a small filled circle as the tray icon (pure GDI, no .ico file)."""
        import win32con
        import win32gui

        size = 16
        r, g, b = rgb
        colour = win32api_rgb(r, g, b)
        screen = win32gui.GetDC(0)
        colour_dc = mask_dc = None
        colour_bmp = mask_bmp = None
        try:
            # colour plane ------------------------------------------------------
            colour_dc = win32gui.CreateCompatibleDC(screen)
            colour_bmp = win32gui.CreateCompatibleBitmap(screen, size, size)
            old_bmp = win32gui.SelectObject(colour_dc, colour_bmp)
            black = win32gui.GetStockObject(win32con.BLACK_BRUSH)
            win32gui.FillRect(colour_dc, (0, 0, size, size), black)
            brush = win32gui.CreateSolidBrush(colour)
            pen = win32gui.CreatePen(win32con.PS_SOLID, 1, colour)
            old_brush = win32gui.SelectObject(colour_dc, brush)
            old_pen = win32gui.SelectObject(colour_dc, pen)
            win32gui.Ellipse(colour_dc, 1, 1, size - 1, size - 1)
            win32gui.SelectObject(colour_dc, old_brush)
            win32gui.SelectObject(colour_dc, old_pen)
            win32gui.DeleteObject(brush)
            win32gui.DeleteObject(pen)
            win32gui.SelectObject(colour_dc, old_bmp)
            # mask plane: 1 = transparent, 0 = opaque --------------------------
            mask_dc = win32gui.CreateCompatibleDC(screen)
            mask_bmp = win32gui.CreateBitmap(size, size, 1, 1, None)
            old_mask = win32gui.SelectObject(mask_dc, mask_bmp)
            white = win32gui.GetStockObject(win32con.WHITE_BRUSH)
            win32gui.FillRect(mask_dc, (0, 0, size, size), white)
            black_pen = win32gui.GetStockObject(win32con.BLACK_PEN)
            old_b2 = win32gui.SelectObject(mask_dc, black)
            old_p2 = win32gui.SelectObject(mask_dc, black_pen)
            win32gui.Ellipse(mask_dc, 1, 1, size - 1, size - 1)
            win32gui.SelectObject(mask_dc, old_b2)
            win32gui.SelectObject(mask_dc, old_p2)
            win32gui.SelectObject(mask_dc, old_mask)
            # ICONINFO: (fIcon, xHotspot, yHotspot, hbmMask, hbmColor)
            return win32gui.CreateIconIndirect((True, 0, 0, mask_bmp, colour_bmp))
        finally:
            for dc in (colour_dc, mask_dc):
                if dc:
                    win32gui.DeleteDC(dc)
            for bmp in (colour_bmp, mask_bmp):
                if bmp:
                    win32gui.DeleteObject(bmp)
            win32gui.ReleaseDC(0, screen)

    def _add_icon(self) -> None:
        import win32gui

        with self._lock:
            state, tip = self._state, self._tooltip_text()
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self._hwnd, 0, flags, self._WM_TRAY, self._icon_handle(state), tip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
            if not self._icon_added:
                log.info("tray icon added")
            self._icon_added = True
        except Exception as exc:  # noqa: BLE001
            # Explorer's taskbar does not exist yet (early in logon) or the icon is
            # already there; a successful MODIFY tells the two apart.
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
                self._icon_added = True
            except Exception:  # noqa: BLE001
                self._icon_added = False
                log.debug("Shell_NotifyIcon ADD failed (will retry): %s", exc)

    def _refresh(self) -> None:
        import win32gui

        if not self._icon_added:
            self._add_icon()
            return
        with self._lock:
            state, tip = self._state, self._tooltip_text()
        flags = win32gui.NIF_ICON | win32gui.NIF_TIP
        nid = (self._hwnd, 0, flags, self._WM_TRAY, self._icon_handle(state), tip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:  # noqa: BLE001
            self._icon_added = False  # Explorer went away; the timer / TaskbarCreated re-adds it

    def _show_balloon(self, balloon: tuple) -> None:
        import win32gui

        with self._lock:
            state = self._state
        title, message, level = balloon
        info_flags = {"critical": win32gui.NIIF_ERROR, "warning": win32gui.NIIF_WARNING}.get(level, win32gui.NIIF_INFO)
        nid = (self._hwnd, 0, win32gui.NIF_INFO, self._WM_TRAY, self._icon_handle(state),
               self._tooltip_text(), message[:255], 10, title[:63], info_flags)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:  # noqa: BLE001
            self._icon_added = False
            log.debug("balloon could not be shown; tray icon is gone")

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
            options = list(self._request_options)
        self._menu_options = options   # what the ids of this menu mean, whatever arrives while it is open
        win32gui.AppendMenu(menu, self._win32con.MF_STRING | self._win32con.MF_GRAYED, self.ID_STATUS, detail[:80] or "PC Link")
        win32gui.AppendMenu(menu, self._win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, self._win32con.MF_STRING, self.ID_OPEN, "Time remaining")
        if options and self._on_time_request:
            # only offered when the service says a request can be made (a limited user, requests enabled)
            sub = win32gui.CreatePopupMenu()
            for ident, label in request_menu_items(options):
                win32gui.AppendMenu(sub, self._win32con.MF_STRING, ident, label)
            win32gui.AppendMenu(menu, self._win32con.MF_POPUP, sub, "Ask for more time")
        if self._allow_quit:
            win32gui.AppendMenu(menu, self._win32con.MF_STRING, self.ID_QUIT, "Exit")
        try:
            pos = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self._hwnd)
            win32gui.TrackPopupMenu(menu, self._win32con.TPM_LEFTALIGN, pos[0], pos[1], 0, self._hwnd, None)
            win32gui.PostMessage(self._hwnd, self._win32con.WM_NULL, 0, 0)
        finally:
            try:
                win32gui.DestroyMenu(menu)  # the WM_COMMAND is already posted; the menu itself is not needed
            except Exception:  # noqa: BLE001
                pass

    def _on_command(self, hwnd, msg, wparam, lparam):
        ident = wparam & 0xFFFF
        minutes = request_minutes(ident, getattr(self, "_menu_options", []))
        if minutes is not None:
            if self._on_time_request:
                try:
                    self._on_time_request(minutes)
                except Exception:  # noqa: BLE001 - never let a failed request end the tray's message loop
                    log.debug("time request failed", exc_info=True)
        elif ident in (self.ID_OPEN, self.ID_STATUS) and self._on_open:
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
