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
        self.last_hwnd: int = 0
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
        self.last_hwnd = int(hwnd or 0)
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
    """True when the session's default playback device is actively rendering audio.

    Uses Core Audio's ``IAudioMeterInformation`` peak meter through comtypes -- a
    single read-only COM call per poll, no per-process enumeration.  Optional: if
    comtypes is unavailable it always returns False, which only means that audio
    playback will not keep an otherwise idle session counted as active.

    The COM objects are created lazily on the thread that first calls ``active()``
    so they live in that thread's COM apartment.
    """

    def __init__(self, enabled: bool = True):
        self.available = _IS_WIN and enabled
        self._meter = None
        self._failures = 0
        if self.available:
            try:
                import comtypes  # noqa: F401  # fail fast if missing
            except Exception as exc:  # noqa: BLE001
                log.debug("audio metering unavailable (comtypes missing): %s", exc)
                self.available = False

    def _define_interfaces(self):
        from ctypes import POINTER, c_float, c_uint, c_wchar_p
        from ctypes.wintypes import DWORD

        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

        class IAudioMeterInformation(IUnknown):
            _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "pfPeak")),
                COMMETHOD([], HRESULT, "GetMeteringChannelCount", (["out"], POINTER(c_uint), "pnChannelCount")),
                COMMETHOD([], HRESULT, "GetChannelsPeakValues", (["in"], c_uint, "u32ChannelCount"),
                          (["in"], POINTER(c_float), "afPeakValues")),
                COMMETHOD([], HRESULT, "QueryHardwareSupport", (["out"], POINTER(DWORD), "pdwHardwareSupportMask")),
            )

        class IMMDevice(IUnknown):
            _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = (
                COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "dwClsCtx"),
                          (["in"], POINTER(DWORD), "pActivationParams"),
                          (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
                COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], DWORD, "stgmAccess"),
                          (["out"], POINTER(POINTER(IUnknown)), "ppProperties")),
                COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "ppstrId")),
                COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "pdwState")),
            )

        class IMMDeviceEnumerator(IUnknown):
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = (
                COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], DWORD, "dataFlow"), (["in"], DWORD, "dwStateMask"),
                          (["out"], POINTER(POINTER(IUnknown)), "ppDevices")),
                COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], DWORD, "dataFlow"), (["in"], DWORD, "role"),
                          (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
                COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "pwstrId"),
                          (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
                COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], POINTER(IUnknown), "pClient")),
                COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], POINTER(IUnknown), "pClient")),
            )

        self._IMMDeviceEnumerator = IMMDeviceEnumerator
        self._IAudioMeterInformation = IAudioMeterInformation
        self._CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

    def _create_meter(self):
        import comtypes

        if not hasattr(self, "_IMMDeviceEnumerator"):
            self._define_interfaces()
        enumerator = comtypes.CoCreateInstance(self._CLSID_MMDeviceEnumerator, interface=self._IMMDeviceEnumerator,
                                               clsctx=comtypes.CLSCTX_INPROC_SERVER)
        device = enumerator.GetDefaultAudioEndpoint(0, 0)  # eRender, eConsole
        unknown = device.Activate(self._IAudioMeterInformation._iid_, comtypes.CLSCTX_INPROC_SERVER, None)
        return unknown.QueryInterface(self._IAudioMeterInformation)

    def active(self, threshold: float = 0.01) -> bool:
        if not self.available:
            return False
        try:
            if self._meter is None:
                self._meter = self._create_meter()
            peak = float(self._meter.GetPeakValue())
            self._failures = 0
            return peak > threshold
        except Exception as exc:  # noqa: BLE001
            # default device changed / unplugged: rebuild next time, give up after many failures
            self._meter = None
            self._failures += 1
            if self._failures in (1, 20):
                log.debug("audio meter read failed (%d): %s", self._failures, exc)
            if self._failures > 200:
                self.available = False
            return False


def lock_workstation() -> bool:
    if not _IS_WIN:
        return False
    try:
        return bool(ctypes.windll.user32.LockWorkStation())
    except Exception:  # noqa: BLE001
        return False


# -- closing things (rules / focus mode / HA "close app") -----------------------------------

_PROTECTED = {"explorer.exe", "winlogon.exe", "csrss.exe", "dwm.exe", "pythonw.exe", "python.exe", "pclink.exe"}


def close_process(pid: int = 0, name: str = "") -> int:
    """Terminate ``pid`` (if its image name still matches) or every process called ``name``.

    The agent runs as the session user, so it can only ever terminate that
    user's own processes - another seat is never affected.  Returns the count.
    """
    if not _IS_WIN:
        log.info("(dry-run) close process pid=%s name=%s", pid, name)
        return 0
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        return 0
    want = (name or "").lower()
    if want in _PROTECTED:
        return 0
    targets = []
    if pid > 0:
        try:
            p = psutil.Process(pid)
            if not want or p.name().lower() == want:
                targets.append(p)
        except psutil.Error:
            pass
    if not targets and want:
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info["name"] or "").lower() == want:
                    targets.append(p)
            except psutil.Error:
                continue
    count = 0
    for p in targets:
        try:
            p.terminate()
            count += 1
        except psutil.AccessDenied:
            continue  # another user's process (multiseat) - not ours to touch
        except psutil.Error:
            continue
    gone, alive = psutil.wait_procs(targets, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return count


def close_tab(pid: int = 0) -> bool:
    """Send Ctrl+W to the foreground window (only if it belongs to ``pid``)."""
    if not _IS_WIN:
        log.info("(dry-run) close tab pid=%s", pid)
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        if pid:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) != int(pid):
                return False  # the user already switched away; do not close something else
        VK_CONTROL, VK_W, KEYEVENTF_KEYUP = 0x11, 0x57, 0x0002
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_W, 0, 0, 0)
        user32.keybd_event(VK_W, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("close tab failed: %s", exc)
        return False
