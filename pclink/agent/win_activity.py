"""Read the current user's foreground window, idle time, lock state and audio.

All calls are cheap Win32 queries (no polling of every process, no hooks) so
the agent stays well under a rounding error of CPU.  Everything degrades
gracefully to empty values when a dependency or API is unavailable.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional

from ..models import ForegroundInfo

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class Win32Activity:
    def __init__(self):
        self._psutil = None
        self._proc_cache: dict[int, tuple[str, str]] = {}
        if _IS_WIN:
            self._user32 = ctypes.windll.user32
            self._kernel32 = ctypes.windll.kernel32
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
        return max(0.0, elapsed / 1000.0)

    # -- lock state ----------------------------------------------------------
    def is_locked(self) -> bool:
        """A locked session has no open desktop input desktop we can open."""
        if not _IS_WIN:
            return False
        try:
            hdesk = self._user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
            if not hdesk:
                return True
            name_buf = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            ctypes.windll.user32.GetUserObjectInformationW(hdesk, 2, name_buf, ctypes.sizeof(name_buf), ctypes.byref(needed))
            self._user32.CloseDesktop(hdesk)
            # "Default" desktop = interactive; "Winlogon"/"Screen-saver" = locked/secure
            return name_buf.value.lower() not in ("default", "")
        except Exception:  # noqa: BLE001
            return False

    # -- foreground window ---------------------------------------------------
    def foreground(self) -> Optional[ForegroundInfo]:
        if not _IS_WIN:
            return None
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = self._user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name, exe = self._process(int(pid.value))
        return ForegroundInfo(pid=int(pid.value), name=name, exe=exe, title=title, fullscreen=self._is_fullscreen(hwnd))

    def _process(self, pid: int) -> tuple[str, str]:
        if pid <= 0:
            return "", ""
        if pid in self._proc_cache:
            return self._proc_cache[pid]
        name = exe = ""
        if self._psutil is not None:
            try:
                p = self._psutil.Process(pid)
                name = p.name() or ""
                try:
                    exe = p.exe() or ""
                except Exception:  # noqa: BLE001 - AccessDenied for elevated apps
                    exe = ""
            except Exception:  # noqa: BLE001
                name = exe = ""
        if len(self._proc_cache) > 512:
            self._proc_cache.clear()
        self._proc_cache[pid] = (name, exe)
        return name, exe

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

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(mi)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                return False
            m = mi.rcMonitor
            return (fg_rect.left <= m.left and fg_rect.top <= m.top
                    and fg_rect.right >= m.right and fg_rect.bottom >= m.bottom)
        except Exception:  # noqa: BLE001
            return False


class AudioActivity:
    """True when any session process is actively rendering audio.

    Uses the Core Audio ``IAudioMeterInformation`` peak value through comtypes.
    Optional: if comtypes/pycaw-style access is not available it always returns
    False, which only means "audio never keeps an idle session awake".
    """

    def __init__(self, enabled: bool = True):
        self.available = False
        self._meter = None
        if not (_IS_WIN and enabled):
            return
        try:
            self._init_meter()
            self.available = True
        except Exception as exc:  # noqa: BLE001
            log.debug("audio metering unavailable: %s", exc)

    def _init_meter(self):
        import comtypes  # type: ignore
        from comtypes import GUID  # type: ignore

        self._comtypes = comtypes
        self._CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        self._IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        self._IID_IAudioMeterInformation = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
        self._eRender = 0
        self._eConsole = 0

    def active(self, threshold: float = 0.0) -> bool:
        if not self.available:
            return False
        try:
            comtypes = self._comtypes
            comtypes.CoInitialize()
            try:
                enum = comtypes.CoCreateInstance(self._CLSID_MMDeviceEnumerator, interface=None,
                                                 clsctx=comtypes.CLSCTX_INPROC_SERVER)
                # Late-bound query: peak meter on the default render endpoint.
                dev = enum.GetDefaultAudioEndpoint(self._eRender, self._eConsole)  # type: ignore[attr-defined]
                meter = dev.Activate(self._IID_IAudioMeterInformation, comtypes.CLSCTX_INPROC_SERVER, None)
                peak = meter.GetPeakValue()  # type: ignore[attr-defined]
                return float(peak) > threshold
            finally:
                comtypes.CoUninitialize()
        except Exception:  # noqa: BLE001
            return False


def lock_workstation() -> bool:
    if not _IS_WIN:
        return False
    try:
        return bool(ctypes.windll.user32.LockWorkStation())
    except Exception:  # noqa: BLE001
        return False
