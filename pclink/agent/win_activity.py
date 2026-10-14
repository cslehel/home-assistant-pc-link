"""Read the current user's foreground window, idle time, lock state and audio.

All calls are cheap Win32 queries (no polling of every process, no hooks) so
the agent stays well under a rounding error of CPU.  Everything degrades
gracefully to empty values when a dependency or API is unavailable.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from dataclasses import fields
from typing import Optional

from ..models import ForegroundInfo

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

# Store / UWP apps draw into a frame window owned by this host process; the app itself
# owns a child window of the class below.
FRAME_HOST = "applicationframehost.exe"
CORE_WINDOW_CLASS = "Windows.UI.Core.CoreWindow"

_FG_HAS_CREATED = "created" in {f.name for f in fields(ForegroundInfo)}


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _typed_user32():
    """A private ``user32`` with prototypes, so handles and pointers are never truncated to 32 bits.

    A private ``WinDLL`` instance is used (not ``ctypes.windll.user32``) so the
    prototypes cannot clash with other code sharing the cached library object.
    """
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    u.GetLastInputInfo.restype = wintypes.BOOL
    u.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u.OpenInputDesktop.restype = wintypes.HANDLE
    u.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
    u.GetUserObjectInformationW.restype = wintypes.BOOL
    u.CloseDesktop.argtypes = [wintypes.HANDLE]
    u.CloseDesktop.restype = wintypes.BOOL
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetShellWindow.argtypes = []
    u.GetShellWindow.restype = wintypes.HWND
    u.GetDesktopWindow.argtypes = []
    u.GetDesktopWindow.restype = wintypes.HWND
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.MonitorFromWindow.restype = wintypes.HANDLE
    u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    u.GetMonitorInfoW.restype = wintypes.BOOL
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    u.keybd_event.restype = None
    u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowExW.restype = wintypes.HWND
    u.LockWorkStation.argtypes = []
    u.LockWorkStation.restype = wintypes.BOOL
    return u


def _window_title(user32, hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _core_window_pid(user32, hwnd, frame_pid: int) -> int:
    """The pid of the Store app shown in an ApplicationFrameHost window (0 when there is none).

    While the app is in front its core window is a child of the frame.  Right
    after a switch (and while the app is suspended) it is still a top-level
    window of its own with the frame's title; that is the second place looked.
    """
    def owner(window) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        return int(pid.value)

    child = None
    for _ in range(16):   # a frame has one or two such children; never loop on a broken window tree
        child = user32.FindWindowExW(hwnd, child, CORE_WINDOW_CLASS, None)
        if not child:
            break
        pid = owner(child)
        if pid and pid != int(frame_pid):
            return pid
    title = _window_title(user32, hwnd)
    if not title:
        return 0
    found = set()
    top = None
    for _ in range(64):
        top = user32.FindWindowExW(None, top, CORE_WINDOW_CLASS, title)
        if not top:
            break
        pid = owner(top)
        if pid and pid != int(frame_pid):
            found.add(pid)
    # two apps with the same title (two Settings-like windows): not known which one this frame shows
    return found.pop() if len(found) == 1 else 0


def rect_is_fullscreen(window: tuple[int, int, int, int], monitor: tuple[int, int, int, int]) -> bool:
    """True when a window rectangle is *exactly* its monitor (pure helper, unit-tested).

    A maximized window is not full-screen: Windows gives it a rectangle that
    overhangs the work area by the frame width on every side, which used to
    pass a "covers the monitor" test on a monitor without a taskbar.
    """
    return tuple(window) == tuple(monitor)


def secure_desktop_means_locked(desktop_name: Optional[str], session_locked: Optional[bool]) -> bool:
    """Is the session locked, given the input desktop and what Terminal Services says? (pure; unit-tested)

    ``desktop_name`` is None when the input desktop could not be opened.  The
    Winlogon desktop is also where a UAC prompt is shown: when Windows says
    the session is *not* locked, that is all it is.
    """
    name = (desktop_name or "").lower() if desktop_name is not None else None
    if name in ("default", ""):
        return False
    if name in (None, "winlogon") and session_locked is False:
        return False
    return True


def session_locked() -> Optional[bool]:
    """Windows' own lock flag of this session; None when it cannot be read."""
    if not _IS_WIN:
        return None
    try:
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        wts.WTSFreeMemory.restype = None
        buf = ctypes.c_void_p()
        size = wintypes.DWORD()
        # WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionInfoEx
        if not wts.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 25, ctypes.byref(buf), ctypes.byref(size)) or not buf.value:
            return None
        try:
            if size.value < 20:
                return None
            # WTSINFOEXW: DWORD Level; (8-byte aligned) WTSINFOEX_LEVEL1_W { SessionId, SessionState, SessionFlags, ... }
            level = ctypes.c_uint32.from_address(buf.value).value
            flags = ctypes.c_int32.from_address(buf.value + 16).value
        finally:
            wts.WTSFreeMemory(buf)
        if level != 1:
            return None
        return {0: True, 1: False}.get(flags)   # WTS_SESSIONSTATE_LOCK / _UNLOCK
    except Exception:  # noqa: BLE001
        return None


INJECTION_WINDOW_MS = 1000   # input seen this soon after the agent's own wake-up movement is that movement


def idle_after_injection(last_input_tick: int, now_tick: int, injected_tick: int, prior_idle: float,
                         window_ms: int = INJECTION_WINDOW_MS) -> Optional[float]:
    """Idle seconds as if the agent's injected mouse movement had not happened (pure; unit-tested).

    ``injected_tick`` is ``GetTickCount`` when the movement was injected and
    ``prior_idle`` the idle time just before.  As long as the last input is that
    movement (or older), the seat has been idle for ``prior_idle`` plus the time
    since.  None once real input came after it: from then on the plain reading
    is right.  Ticks wrap after 49.7 days, hence the modular arithmetic.
    """
    def signed(delta: int) -> int:
        return ((int(delta) + 2 ** 31) % 2 ** 32) - 2 ** 31

    since_injection = signed(last_input_tick - injected_tick)
    if since_injection > window_ms:
        return None
    return max(0.0, float(prior_idle) + max(0, signed(now_tick - injected_tick)) / 1000.0)


class Win32Activity:
    def __init__(self):
        self._injected: Optional[tuple[int, float]] = None   # (tick, idle before) of the last wake-up movement
        self._psutil = None
        # pid -> (process creation time, name, exe); the creation time detects pid reuse
        self._proc_cache: dict[int, tuple[float, str, str]] = {}
        self.last_hwnd: int = 0
        self.last_created: float = 0.0   # creation time of the last reported foreground process
        self.last_frame_pid: int = 0     # the ApplicationFrameHost pid when the foreground is a Store app
        if _IS_WIN:
            self._user32 = _typed_user32()
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32.GetTickCount.argtypes = []
            self._kernel32.GetTickCount.restype = wintypes.DWORD
            try:
                import psutil  # type: ignore

                self._psutil = psutil
            except Exception:  # noqa: BLE001
                log.debug("psutil unavailable in agent; process names limited")

    # -- idle ----------------------------------------------------------------
    def idle_seconds(self) -> float:
        if not _IS_WIN:
            return 0.0
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick = self._kernel32.GetTickCount()
        elapsed = (tick - info.dwTime) & 0xFFFFFFFF
        if self._injected is not None:
            adjusted = idle_after_injection(int(info.dwTime), int(tick), *self._injected)
            if adjusted is not None:
                return adjusted
            self._injected = None   # somebody touched the keyboard or mouse since
        return max(0.0, elapsed / 1000.0)

    def wake_display(self) -> bool:
        """Turn the display on without the injected movement counting as somebody at the seat.

        Waking is done with a mouse movement (``set_display_power``), which
        resets Windows' last-input time; until real input follows, the idle
        time is reported as if it had not happened.
        """
        if not _IS_WIN:
            return set_display_power(True)
        try:
            prior = self.idle_seconds()
            tick = int(self._kernel32.GetTickCount())
        except Exception:  # noqa: BLE001
            return set_display_power(True)
        done = set_display_power(True)
        if done:
            self._injected = (tick, prior)
        return done

    # -- lock state ----------------------------------------------------------
    def is_locked(self) -> bool:
        """A locked session has no "Default" input desktop we can open.

        Neither has a session showing a UAC prompt, so Windows' own lock flag
        is asked before a secure desktop is taken for a lock.
        """
        if not _IS_WIN:
            return False
        try:
            name: Optional[str] = None
            hdesk = self._user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
            if hdesk:
                name_buf = ctypes.create_unicode_buffer(256)
                needed = wintypes.DWORD()
                self._user32.GetUserObjectInformationW(hdesk, 2, name_buf, ctypes.sizeof(name_buf), ctypes.byref(needed))
                self._user32.CloseDesktop(hdesk)
                # "Default" desktop = interactive; "Winlogon"/"Screen-saver" = locked/secure
                name = name_buf.value
                if name.lower() in ("default", ""):
                    return False
            return secure_desktop_means_locked(name, session_locked())
        except Exception:  # noqa: BLE001
            return False

    # -- foreground window ---------------------------------------------------
    def foreground(self) -> Optional[ForegroundInfo]:
        if not _IS_WIN:
            return None
        hwnd = self._user32.GetForegroundWindow()
        self.last_hwnd = int(hwnd or 0)
        self.last_frame_pid = 0
        if not hwnd:
            self.last_created = 0.0
            return None
        title = _window_title(self._user32, hwnd)
        owner = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        pid = int(owner.value)
        name, exe, created = self._process_info(pid)
        if name.lower() == FRAME_HOST:
            # A Store app: every one of them would otherwise be "applicationframehost.exe",
            # which no rule can tell apart and nothing may close.
            try:
                app_pid = _core_window_pid(self._user32, hwnd, pid)
            except Exception:  # noqa: BLE001
                app_pid = 0
            if app_pid:
                app_name, app_exe, app_created = self._process_info(app_pid)
                if app_name:
                    self.last_frame_pid = pid
                    pid, name, exe, created = app_pid, app_name, app_exe, app_created
        self.last_created = created
        extra = {"created": created} if _FG_HAS_CREATED else {}
        return ForegroundInfo(pid=pid, name=name, exe=exe, title=title, fullscreen=self._is_fullscreen(hwnd), **extra)

    def _process(self, pid: int) -> tuple[str, str]:
        name, exe, _created = self._process_info(pid)
        return name, exe

    def _process_info(self, pid: int) -> tuple[str, str, float]:
        """(name, exe, creation time) of a process; empty / 0 when it cannot be read."""
        if pid <= 0 or self._psutil is None:
            return "", "", 0.0
        # Windows reuses pids quickly, so a cache entry is only valid for the process
        # that was created at the same moment.  Reading the creation time is one cheap
        # call per poll; the (slower) name / path lookups stay cached.
        try:
            p = self._psutil.Process(pid)
            created = float(p.create_time())
        except Exception:  # noqa: BLE001 - gone, or not accessible: never cache a failure
            self._proc_cache.pop(pid, None)
            return "", "", 0.0
        cached = self._proc_cache.get(pid)
        if cached is not None and cached[0] == created:
            return cached[1], cached[2], created
        try:
            name = p.name() or ""
        except Exception:  # noqa: BLE001
            name = ""
        try:
            exe = p.exe() or ""
        except Exception:  # noqa: BLE001 - AccessDenied for elevated apps
            exe = ""
        if not name:
            self._proc_cache.pop(pid, None)  # try again on the next poll
            return name, exe, created
        if len(self._proc_cache) > 512:
            self._proc_cache.clear()
        self._proc_cache[pid] = (created, name, exe)
        return name, exe, created

    def _is_fullscreen(self, hwnd) -> bool:
        try:
            user32 = self._user32
            fg_rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(fg_rect)):
                return False
            shell = user32.GetShellWindow()
            desktop = user32.GetDesktopWindow()
            if hwnd in (shell, desktop):
                return False
            monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            if not monitor:
                return False
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(mi)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                return False
            m = mi.rcMonitor
            return rect_is_fullscreen((fg_rect.left, fg_rect.top, fg_rect.right, fg_rect.bottom),
                                      (m.left, m.top, m.right, m.bottom))
        except Exception:  # noqa: BLE001
            return False


def lock_workstation() -> bool:
    if not _IS_WIN:
        return False
    try:
        return bool(_typed_user32().LockWorkStation())
    except Exception:  # noqa: BLE001
        return False


def set_display_power(on: bool) -> bool:
    """Turn the displays of this session off, or wake them.

    Off is the request Windows itself uses (``SC_MONITORPOWER``), posted so that a
    window that does not answer cannot hold the agent up.  The same request with
    "on" is ignored by current Windows versions, so waking is done the way a
    person does it: with (injected) mouse movement of one pixel and back.
    """
    if not _IS_WIN:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not on:
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW.restype = wintypes.BOOL
            # HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2 = power off
            return bool(user32.PostMessageW(wintypes.HWND(0xFFFF), 0x0112, 0xF170, 2))
        user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        user32.mouse_event.restype = None
        for dx in (1, -1):
            user32.mouse_event(0x0001, dx & 0xFFFFFFFF, 0, 0, None)  # MOUSEEVENTF_MOVE, relative
        return True
    except Exception:  # noqa: BLE001
        return False


# -- closing things (rules / focus mode / HA "close app") -----------------------------------

# The shell and the sign-in / security processes: the same list the service refuses
# (rules.NEVER_CLOSE; a test keeps the two together), plus PC Link's own programs.  The
# agent's interpreter (python.exe in a Python install) is protected by pid, not by name,
# so a rule can still close somebody's Python game.
_PROTECTED = {
    "explorer.exe", "winlogon.exe", "csrss.exe", "dwm.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe",
    "runtimebroker.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "applicationframehost.exe", "lockapp.exe", "textinputhost.exe",
    "systemsettings.exe", "consent.exe", "credentialuihost.exe", "securityhealthsystray.exe",
    "logonui.exe", "userinit.exe", "smss.exe", "wininit.exe", "services.exe", "lsass.exe", "fontdrvhost.exe",
    "pclink.exe", "pclink-agent.exe",
}
CREATED_TOLERANCE_SECONDS = 1.0


def protected_pids() -> set[int]:
    """Processes the agent never terminates whatever they are called: itself and what started it.

    The parent is the service (or, in a virtual environment, the launcher stub
    whose end would take the agent with it).  ``PCLINK_SERVICE_PID`` names the
    service when the launcher passes it.
    """
    pids = {os.getpid()}
    try:
        pids.add(os.getppid())
    except Exception:  # noqa: BLE001
        pass
    try:
        service = int(os.environ.get("PCLINK_SERVICE_PID", "0") or 0)
        if service > 0:
            pids.add(service)
    except ValueError:
        pass
    pids.discard(0)
    return pids


def created_matches(expected: float, actual: float, tolerance: float = CREATED_TOLERANCE_SECONDS) -> bool:
    """Is this still the process the service meant? (pure; unit-tested)

    ``expected`` is the creation time from the report the decision was based
    on; 0 means the service did not say, and only the name is checked.
    """
    if not expected or expected <= 0:
        return True
    if not actual or actual <= 0:
        return False
    return abs(float(actual) - float(expected)) <= tolerance


# Why nothing was terminated, as told to the service.  It reads a reply that is one of
# "tab_failed", "noop", "terminated 0" or starts with "refused" / "failed" as "not closed".
WHY_PROTECTED = "refused protected"        # the shell, a sign-in process, the agent or the service
WHY_REUSED = "refused pid reused"          # the pid now belongs to another process
WHY_DENIED = "failed access denied"        # Windows did not let this user end it (elevated / another user's)
WHY_NOOP = "noop"                          # nothing to do here (not Windows, or nothing was named)


def close_reply(count: int, why: str = "") -> str:
    """The ``result`` of a close reply (pure; unit-tested).

    A success is always "terminated N" with N of at least one, which the
    service can never mistake for a failure; every failure is one of the forms
    it knows ("terminated 0" when the process was simply not there any more).
    """
    if count > 0:
        return f"terminated {int(count)}"
    return why or "terminated 0"


def close_app(pid: int = 0, name: str = "", created: float = 0.0) -> str:
    """``close_process`` with the reply the service expects."""
    why: list[str] = []
    return close_reply(close_process(pid, name, created, why), why[0] if why else "")


def close_process(pid: int = 0, name: str = "", created: float = 0.0, why: Optional[list] = None) -> int:
    """Terminate ``pid`` (if it is still that process) or, without a pid, every process called ``name``.

    When a pid is given it is the *only* candidate: if that process is gone, is
    no longer called ``name`` or was created at another moment than ``created``
    (the pid was reused, or the report was stale) nothing is terminated -
    falling back to "everything called ``name``" would close the wrong
    application.  Protected processes, the agent itself and the process that
    started it are never terminated, whatever name the request carries.

    The agent runs as the session user, so it can only ever terminate that
    user's own processes - another seat is never affected.  Returns the count;
    when it is 0 and a list is passed as ``why``, the reason (``WHY_*``) is put in it.
    """
    def nothing(reason: str = "") -> int:
        if why is not None and reason:
            why.append(reason)
        return 0

    if not _IS_WIN:
        log.info("(dry-run) close process pid=%s name=%s", pid, name)
        return nothing(WHY_NOOP)
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        return nothing(WHY_NOOP)
    want = (name or "").strip().lower()
    if want in _PROTECTED:
        return nothing(WHY_PROTECTED)
    if pid <= 0 and not want:
        return nothing(WHY_NOOP)
    never = protected_pids()
    targets = []
    if pid > 0:
        if pid in never:
            return nothing(WHY_PROTECTED)
        try:
            p = psutil.Process(pid)
            actual = (p.name() or "").lower()
            actual_created = float(p.create_time())
        except psutil.Error:
            log.info("close: pid %s is gone or not accessible; nothing closed", pid)
            return nothing()
        if not actual or actual in _PROTECTED:
            log.info("close: pid %s is %s; refusing", pid, actual or "unknown")
            return nothing(WHY_PROTECTED)
        if want and actual != want:
            log.info("close: pid %s is now %s, not %s; nothing closed", pid, actual, want)
            return nothing(WHY_REUSED)
        if not created_matches(created, actual_created):
            log.info("close: pid %s was reused by another %s; nothing closed", pid, actual)
            return nothing(WHY_REUSED)
        targets.append(p)
    else:
        for p in psutil.process_iter(["name"]):
            try:
                if p.pid not in never and (p.info["name"] or "").lower() == want:
                    targets.append(p)
            except psutil.Error:
                continue
    count = denied = 0
    for p in targets:
        try:
            p.terminate()
            count += 1
        except psutil.AccessDenied:
            denied += 1
            continue  # another user's process (multiseat) or an elevated one - not ours to touch
        except psutil.Error:
            continue
    if not count and denied:
        nothing(WHY_DENIED)
    gone, alive = psutil.wait_procs(targets, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return count


def modifiers_block_close(shift: bool, alt: bool, win: bool) -> bool:
    """True when a held modifier would turn Ctrl+W into something else (pure helper, unit-tested).

    Ctrl+Shift+W closes the whole browser window (every tab); Alt and the
    Windows key produce unrelated shortcuts.
    """
    return bool(shift or alt or win)


def injection_blocked(own_elevated: Optional[bool], target_elevated: Optional[bool]) -> bool:
    """True when Windows would drop keystrokes sent to the target (pure helper, unit-tested).

    A program running as administrator does not accept input injected by one
    that is not ("UIPI"); the keystroke disappears without any error.
    """
    return bool(target_elevated) and not own_elevated


def process_elevated(pid: Optional[int] = None) -> Optional[bool]:
    """Does ``pid`` (default: this process) run elevated?  None when it cannot be told."""
    if not _IS_WIN:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        own = pid is None
        process = kernel32.GetCurrentProcess() if own else kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not process:
            return None
        token = wintypes.HANDLE()
        try:
            if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
                # the token of a process at a higher integrity level cannot be opened: that is the answer
                return True if (not own and ctypes.get_last_error() == 5) else None
            try:
                elevated = wintypes.DWORD()
                size = wintypes.DWORD()
                if not advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated),
                                                    ctypes.byref(size)):  # TokenElevation
                    return None
                return bool(elevated.value)
            finally:
                kernel32.CloseHandle(token)
        finally:
            if not own:
                kernel32.CloseHandle(process)
    except Exception:  # noqa: BLE001
        return None


def window_process_id(hwnd: int) -> int:
    """The id of the process that owns window ``hwnd`` (0 when it cannot be told)."""
    if not _IS_WIN or not hwnd:
        return 0
    try:
        user32 = _typed_user32()
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return int(owner.value)
    except Exception:  # noqa: BLE001
        return 0


def window_title(hwnd: int) -> str:
    """The title of window ``hwnd`` ("" when it has none or cannot be read)."""
    if not _IS_WIN or not hwnd:
        return ""
    try:
        return _window_title(_typed_user32(), hwnd)
    except Exception:  # noqa: BLE001
        return ""


def child_window(hwnd: int, class_name: str) -> int:
    """The first direct child window of ``hwnd`` with this window class (0: there is none)."""
    if not _IS_WIN or not hwnd:
        return 0
    try:
        return int(_typed_user32().FindWindowExW(hwnd, None, class_name, None) or 0)
    except Exception:  # noqa: BLE001
        return 0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    # vt, three reserved words, then the value (a pointer for VT_LPWSTR); padded to the real size
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


def _guid(text: str) -> _GUID:
    import uuid

    guid = _GUID()
    ctypes.memmove(ctypes.byref(guid), uuid.UUID(text).bytes_le, ctypes.sizeof(guid))
    return guid


_IID_IPROPERTYSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
_PKEY_APPUSERMODEL_ID = ("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", 5)
_VT_LPWSTR = 31


def window_app_user_model_id(hwnd: int) -> Optional[str]:
    """The AppUserModelID Windows groups window ``hwnd`` under on the taskbar, when it has its own.

    Chromium browsers give each installed web app window one of its own
    ("Chrome._crx_<app id>", "MSEdge._crx_<app id>"); an ordinary browser
    window usually has none.  Needs COM on the calling thread.  Read-only.
    """
    if not _IS_WIN or not hwnd:
        return None
    store = ctypes.c_void_p()
    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PROPVARIANT)]
        ole32.PropVariantClear.restype = ctypes.c_long
        iid = _guid(_IID_IPROPERTYSTORE)
        if shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(store)) < 0 or not store.value:
            return None
        vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        get_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY),
                                       ctypes.POINTER(_PROPVARIANT))(vtable[5])   # IPropertyStore::GetValue
        key = _PROPERTYKEY(_guid(_PKEY_APPUSERMODEL_ID[0]), _PKEY_APPUSERMODEL_ID[1])
        value = _PROPVARIANT()
        if get_value(store, ctypes.byref(key), ctypes.byref(value)) < 0:
            return None
        try:
            if value.vt != _VT_LPWSTR or not value.ptr:
                return None
            return ctypes.wstring_at(value.ptr)[:512] or None
        finally:
            ole32.PropVariantClear(ctypes.byref(value))
    except Exception as exc:  # noqa: BLE001
        log.debug("AppUserModelID of window %s not read: %s", hwnd, exc)
        return None
    finally:
        if store.value:
            try:
                vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(store)   # IUnknown::Release
            except Exception:  # noqa: BLE001
                pass


def close_tab(pid: int = 0, title: str = "") -> bool:
    """Send Ctrl+W to the foreground window.

    Only when that window still belongs to ``pid`` and - when the service passed
    the ``title`` it based its decision on - still shows that page; otherwise the
    user has already moved on and another tab would be closed.  Nothing is sent
    while Shift / Alt / Win is held, or to a browser running as administrator
    (Windows would silently drop it).  Returns False whenever nothing was sent;
    the service simply asks again on a later tick.
    """
    if not _IS_WIN:
        log.info("(dry-run) close tab pid=%s", pid)
        return False
    try:
        user32 = _typed_user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if pid and int(owner.value) != int(pid) and _core_window_pid(user32, hwnd, int(owner.value)) != int(pid):
            return False  # the user already switched away; do not close something else
        if title and _window_title(user32, hwnd) != title:
            log.info("close tab: the page in front changed; nothing closed")
            return False
        if injection_blocked(process_elevated(), process_elevated(int(owner.value))):
            log.info("close tab: the browser runs as administrator and does not accept the keystroke; nothing closed")
            return False
        VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x12, 0x5B, 0x5C

        def held(vk: int) -> bool:
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)

        if modifiers_block_close(held(VK_SHIFT), held(VK_MENU), held(VK_LWIN) or held(VK_RWIN)):
            log.info("close tab: a modifier key is held; nothing closed")
            return False
        VK_CONTROL, VK_W, KEYEVENTF_KEYUP = 0x11, 0x57, 0x0002
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_W, 0, 0, 0)
        user32.keybd_event(VK_W, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("close tab failed: %s", exc)
        return False
