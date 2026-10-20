"""Presence signals beyond keyboard/mouse idle, gathered inside the user session.

Everything here is a cheap Win32 query or a passive notification:

* **Game controllers** - XInput state (and legacy joysticks via winmm) so a
  child playing with a gamepad is not "idle".  Only a change counts:
  buttons pressed or released, a trigger or a stick moved past its dead
  zone.  A controller resting on the desk - or with a button held or stuck -
  produces nothing.
* **Display state** - ``RegisterPowerSettingNotification`` for
  ``GUID_SESSION_DISPLAY_STATE`` (per session - correct for multiseat) with
  ``GUID_CONSOLE_DISPLAY_STATE`` as a fallback: on / off / dimmed.
* **Windows' own user presence** - ``GUID_SESSION_USER_PRESENCE``.
* **Screensaver** - ``SPI_GETSCREENSAVERRUNNING``.
* **Full-screen D3D / presentation mode** - ``SHQueryUserNotificationState``.
* **Display-required requests** - ``CallNtPowerInformation(SystemExecutionState)``:
  set while a video player / browser video / game asks Windows to keep the
  screen on.

The service combines these in ``pclink/presence.py``.  Off Windows everything
returns "unknown" and the agent behaves exactly as before.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Optional

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

# -- power setting GUIDs ---------------------------------------------------------------
GUID_CONSOLE_DISPLAY_STATE = "{6FE69556-704A-47A0-8F24-C28D936FDA47}"
GUID_SESSION_DISPLAY_STATE = "{2B84C20E-AD23-4DDF-93DB-05FFBD7EFCA5}"
GUID_SESSION_USER_PRESENCE = "{3C0F4548-C03F-4C4D-B9F2-237EDE686376}"

WM_POWERBROADCAST = 0x0218
PBT_POWERSETTINGCHANGE = 0x8013
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
DEVICE_NOTIFY_WINDOW_HANDLE = 0

# SHQueryUserNotificationState
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7

# CallNtPowerInformation(SystemExecutionState)
ES_SYSTEM_REQUIRED = 0x1
ES_DISPLAY_REQUIRED = 0x2
ES_USER_PRESENT = 0x4
ES_AWAYMODE_REQUIRED = 0x40

XINPUT_DEADZONE = 8000       # XInput docs recommend ~7849 for the left stick
XINPUT_TRIGGER_THRESHOLD = 30
ERROR_DEVICE_NOT_CONNECTED = 1167


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, text: str) -> "_GUID":
        text = text.strip("{}")
        parts = text.split("-")
        g = cls()
        g.Data1 = int(parts[0], 16)
        g.Data2 = int(parts[1], 16)
        g.Data3 = int(parts[2], 16)
        tail = bytes.fromhex(parts[3] + parts[4])
        for i in range(8):
            g.Data4[i] = tail[i]
        return g

    def __str__(self) -> str:
        return "{%08X-%04X-%04X-%s-%s}" % (
            self.Data1, self.Data2, self.Data3, bytes(self.Data4[:2]).hex().upper(), bytes(self.Data4[2:]).hex().upper()
        )


class _POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", _GUID), ("DataLength", ctypes.c_uint32), ("Data", ctypes.c_ubyte * 4)]


# -- game controllers --------------------------------------------------------------------

class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_uint16), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_int16), ("sThumbLY", ctypes.c_int16), ("sThumbRX", ctypes.c_int16), ("sThumbRY", ctypes.c_int16)]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", _XINPUT_GAMEPAD)]


class _JOYINFOEX(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32), ("dwXpos", ctypes.c_uint32), ("dwYpos", ctypes.c_uint32),
                ("dwZpos", ctypes.c_uint32), ("dwRpos", ctypes.c_uint32), ("dwUpos", ctypes.c_uint32), ("dwVpos", ctypes.c_uint32),
                ("dwButtons", ctypes.c_uint32), ("dwButtonNumber", ctypes.c_uint32), ("dwPOV", ctypes.c_uint32),
                ("dwReserved1", ctypes.c_uint32), ("dwReserved2", ctypes.c_uint32)]


JOY_AXIS_DEADZONE = 4096     # of 0..65535 (~6 %): axis noise / stick drift below this is not input


def joystick_moved(reference: tuple, current: tuple, deadzone: int = JOY_AXIS_DEADZONE) -> bool:
    """True when a legacy joystick axis really moved (pure helper, unit-tested).

    ``reference`` is the position at the last accepted movement (not a fixed
    centre: throttles, pedals and wheels rest anywhere).  A resting stick that
    jitters or drifts stays inside the dead zone around it and never counts.
    """
    return any(abs(int(c) - int(r)) > deadzone for r, c in zip(reference, current))


def joystick_used(last: Optional[tuple], axes: tuple, pov: int, buttons: int) -> bool:
    """Did somebody touch a legacy joystick since ``last`` = (axes, POV, buttons)? (pure; unit-tested)

    Buttons count when they *change*.  Some devices report a button bit that
    is set for good (a switch on a throttle, a wheel's gear lever, odd
    peripherals that show up as a joystick); "pressed" alone kept such a seat
    active for ever.
    """
    if last is None:
        return False   # the first look only records the resting state
    return joystick_moved(last[0], axes) or last[1] != pov or last[2] != buttons


def machine_wide_counts(other_seats_active: bool, fullscreen: bool, audio_active: bool, busy: bool) -> bool:
    """May a signal that is not per session be counted for this one? (pure; unit-tested)

    The "display required" request and XInput are the same for every session
    of the PC.  With one seat that is this seat.  With several active seats
    (multiseat) the signal only counts where something else of this session
    agrees with it: a full-screen window in front, sound on this seat's
    output, or Windows reporting a full-screen / presentation state here.
    """
    if not other_seats_active:
        return True
    return bool(fullscreen or audio_active or busy)


def other_active_sessions() -> Optional[int]:
    """How many *other* sessions have somebody at the console right now; None when unknown."""
    if not _IS_WIN:
        return None
    try:
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class WTS_SESSION_INFOW(ctypes.Structure):
            _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR), ("State", ctypes.c_int)]

        wts.WTSEnumerateSessionsW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                              ctypes.POINTER(ctypes.POINTER(WTS_SESSION_INFOW)), ctypes.POINTER(wintypes.DWORD)]
        wts.WTSEnumerateSessionsW.restype = wintypes.BOOL
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        wts.WTSFreeMemory.restype = None
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        own = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(own)):
            return None
        info = ctypes.POINTER(WTS_SESSION_INFOW)()
        count = wintypes.DWORD()
        if not wts.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(info), ctypes.byref(count)):
            return None
        try:
            # WTSActive = 0; session 0 is the services session, never a seat
            return sum(1 for i in range(count.value)
                       if info[i].State == 0 and info[i].SessionId not in (0, own.value))
        finally:
            wts.WTSFreeMemory(info)
    except Exception:  # noqa: BLE001
        return None


def gamepad_active(buttons: int, lt: int, rt: int, thumbs: tuple[int, int, int, int]) -> bool:
    """True when the pad is being *used* (pure helper, unit-tested)."""
    if buttons:
        return True
    if lt > XINPUT_TRIGGER_THRESHOLD or rt > XINPUT_TRIGGER_THRESHOLD:
        return True
    return any(abs(v) > XINPUT_DEADZONE for v in thumbs)


def gamepad_used(last: Optional[tuple], packet: int, buttons: int, lt: int, rt: int,
                 thumbs: tuple[int, int, int, int]) -> bool:
    """Did somebody use an XInput pad since ``last``? (pure; unit-tested)

    ``last`` is (packet number, buttons, left trigger, right trigger, thumbs)
    at the last accepted input.  Like the joystick: only a *change* counts -
    the packet number moved on and buttons differ, a trigger moved past its
    threshold or a stick past the dead zone.  A button held down (or stuck,
    or a weight on a trigger) stops counting after the first look.
    """
    if last is None:
        return False   # the first look only records the resting state
    if int(packet) == int(last[0]):
        return False   # XInput says nothing changed at all
    if int(buttons) != int(last[1]):
        return True
    if abs(int(lt) - int(last[2])) > XINPUT_TRIGGER_THRESHOLD or abs(int(rt) - int(last[3])) > XINPUT_TRIGGER_THRESHOLD:
        return True
    return any(abs(int(c) - int(r)) > XINPUT_DEADZONE for r, c in zip(last[4], thumbs))


class ControllerActivity:
    """Tracks the last moment a game controller was actually used."""

    def __init__(self, enabled: bool = True):
        self.available = False
        self._xinput = None
        self._winmm = None
        self._last_input: Optional[float] = None
        self._last_joy: dict[int, tuple] = {}
        self._last_pad: dict[int, tuple] = {}   # pad -> its state at the last accepted input (see gamepad_used)
        self._connected: set[int] = set()
        self._next_probe = 0.0
        self.counts = True   # False while another seat may be the one using the controller (PresenceSignals)
        if not (_IS_WIN and enabled):
            return
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._xinput = ctypes.WinDLL(name)
                self._xinput.XInputGetState.argtypes = [ctypes.c_uint32, ctypes.POINTER(_XINPUT_STATE)]
                self._xinput.XInputGetState.restype = ctypes.c_uint32
                break
            except OSError:
                continue
        try:
            self._winmm = ctypes.WinDLL("winmm")
            self._winmm.joyGetPosEx.argtypes = [ctypes.c_uint32, ctypes.POINTER(_JOYINFOEX)]
            self._winmm.joyGetPosEx.restype = ctypes.c_uint32
        except OSError:
            self._winmm = None
        self.available = self._xinput is not None or self._winmm is not None

    @property
    def any_connected(self) -> bool:
        return bool(self._connected)

    def poll(self) -> None:
        if not self.available:
            return
        now = time.monotonic()
        probe = now >= self._next_probe  # look for newly connected pads only every few seconds
        if probe:
            self._next_probe = now + 5.0
        active = False
        if self._xinput is not None:
            state = _XINPUT_STATE()
            for i in range(4):
                if i not in self._connected and not probe:
                    continue
                rc = self._xinput.XInputGetState(i, ctypes.byref(state))
                if rc != 0:
                    self._connected.discard(i)
                    self._last_pad.pop(i, None)
                    continue
                self._connected.add(i)
                g = state.Gamepad
                packet = int(state.dwPacketNumber)
                thumbs = (g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY)
                last = self._last_pad.get(i)
                if last is None or gamepad_used(last, packet, g.wButtons, g.bLeftTrigger, g.bRightTrigger, thumbs):
                    self._last_pad[i] = (packet, int(g.wButtons), int(g.bLeftTrigger), int(g.bRightTrigger), tuple(int(v) for v in thumbs))
                    if last is not None:
                        active = True
                elif packet != last[0]:
                    # noise inside the dead zones: the reference state stays, the packet is seen
                    self._last_pad[i] = (packet,) + tuple(last[1:])
        if self._winmm is not None and (probe or self._last_joy):
            info = _JOYINFOEX()
            info.dwSize = ctypes.sizeof(info)
            info.dwFlags = 0xFF  # JOY_RETURNALL
            for i in range(4):
                if not probe and i not in self._last_joy:
                    continue
                if self._winmm.joyGetPosEx(i, ctypes.byref(info)) != 0:
                    self._last_joy.pop(i, None)
                    continue
                axes = (info.dwXpos, info.dwYpos, info.dwZpos, info.dwRpos)
                last = self._last_joy.get(i)   # (reference axes, POV, buttons) at the last accepted input
                if last is None or joystick_used(last, axes, info.dwPOV, info.dwButtons):
                    self._last_joy[i] = (axes, info.dwPOV, info.dwButtons)
                    if last is not None:
                        active = True
        if active and self.counts:
            self._last_input = now

    def idle_seconds(self) -> Optional[float]:
        if self._last_input is None:
            return None
        return max(0.0, time.monotonic() - self._last_input)


# -- display / user presence notifications --------------------------------------------------

class DisplayStateMonitor:
    """Hidden top-level window receiving WM_POWERBROADCAST power-setting changes."""

    def __init__(self):
        self.display_state: Optional[str] = None   # on | off | dimmed
        self.user_present: Optional[bool] = None
        self._hwnd = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._handles: list = []
        self._wndproc = None
        self.available = False

    def start(self) -> None:
        if not _IS_WIN:
            return
        self._thread = threading.Thread(target=self._run, name="power-notify", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._hwnd:
            try:
                user32 = ctypes.WinDLL("user32")
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user32.PostMessageW.restype = wintypes.BOOL
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        # Private library objects with full prototypes: window messages carry 64-bit
        # pointers in LPARAM, which the default (32-bit int) conversion cannot pass.
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None
        user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = LRESULT
        user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
        user32.UnregisterPowerSettingNotification.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE and lparam:
                    self._on_setting(ctypes.cast(lparam, ctypes.POINTER(_POWERBROADCAST_SETTING)).contents)
                    return 1
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:  # noqa: BLE001
                log.debug("power notification handling failed", exc_info=True)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        try:
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = "PCLinkPowerNotify"
            user32.RegisterClassW(ctypes.byref(wc))
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                               wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            self._hwnd = user32.CreateWindowExW(0, "PCLinkPowerNotify", "PCLink", 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                log.debug("power notification window could not be created")
                self._ready.set()
                return
            user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
            user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.POINTER(_GUID), wintypes.DWORD]
            for guid in (GUID_SESSION_DISPLAY_STATE, GUID_CONSOLE_DISPLAY_STATE, GUID_SESSION_USER_PRESENCE):
                g = _GUID.from_string(guid)
                h = user32.RegisterPowerSettingNotification(self._hwnd, ctypes.byref(g), DEVICE_NOTIFY_WINDOW_HANDLE)
                if h:
                    self._handles.append(h)
            self.available = bool(self._handles)
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001
            log.debug("power notification thread failed", exc_info=True)
            self._ready.set()
        finally:
            for h in self._handles:
                try:
                    user32.UnregisterPowerSettingNotification(h)
                except Exception:  # noqa: BLE001
                    pass

    def _on_setting(self, setting: "_POWERBROADCAST_SETTING") -> None:
        guid = str(setting.PowerSetting).upper()
        value = int.from_bytes(bytes(setting.Data[:4]), "little") if setting.DataLength >= 4 else None
        self.apply(guid, value)

    def apply(self, guid: str, value: Optional[int]) -> None:
        """Interpret one notification (separated out so it is unit-testable)."""
        guid = guid.upper()
        if guid in (GUID_SESSION_DISPLAY_STATE, GUID_CONSOLE_DISPLAY_STATE):
            # a per-session report always wins over the console-wide one
            if guid == GUID_CONSOLE_DISPLAY_STATE and getattr(self, "_session_seen", False):
                return
            if guid == GUID_SESSION_DISPLAY_STATE:
                self._session_seen = True
            self.display_state = {0: "off", 1: "on", 2: "dimmed"}.get(value if value is not None else -1)
        elif guid == GUID_SESSION_USER_PRESENCE:
            # POWER_USER_PRESENCE_TYPE: 0 = present, 2 = not present
            self.user_present = None if value is None else value == 0


# -- one-shot queries ------------------------------------------------------------------------

def screensaver_running() -> bool:
    if not _IS_WIN:
        return False
    try:
        running = wintypes.BOOL(0)
        if ctypes.windll.user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0):
            return bool(running.value)
    except Exception:  # noqa: BLE001
        pass
    return False


def notification_state() -> Optional[int]:
    """SHQueryUserNotificationState (QUNS_*); None when unavailable."""
    if not _IS_WIN:
        return None
    try:
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return int(state.value)
    except Exception:  # noqa: BLE001
        pass
    return None


def toast_suppressed(quns: Optional[int]) -> bool:
    """Would Windows keep a notification off the screen right now? (pure; unit-tested)

    Full-screen games and videos, presentation mode, "do not disturb" and
    a full-screen Store app: the toast goes straight to the notification
    centre, where nobody playing a game sees it.
    """
    return quns in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME, QUNS_APP)


def execution_state() -> Optional[int]:
    """System execution state flags (ES_*); None when unavailable."""
    if not _IS_WIN:
        return None
    try:
        out = ctypes.c_ulong(0)
        rc = ctypes.windll.powrprof.CallNtPowerInformation(16, None, 0, ctypes.byref(out), ctypes.sizeof(out))
        if rc == 0:
            return int(out.value)
    except Exception:  # noqa: BLE001
        pass
    return None


class PresenceSignals:
    """Everything the agent adds to a report beyond idle/lock/foreground."""

    SEATS_CHECK_SECONDS = 30.0

    def __init__(self, controller: bool = True):
        self.controller = ControllerActivity(controller)
        self.display = DisplayStateMonitor()
        self._other_seats = False
        self._seats_checked = 0.0
        # how many other seats are in use as the service counts them (it sees every ASTER workplace,
        # which a session of ASTER's does not); None until it said
        self.service_other_seats: Optional[int] = None
        self.last_quns: Optional[int] = None   # the last SHQueryUserNotificationState, for the notifier

    def start(self) -> None:
        self.display.start()

    def stop(self) -> None:
        self.display.stop()

    def _other_seats_active(self) -> bool:
        now = time.monotonic()
        if now - self._seats_checked >= self.SEATS_CHECK_SECONDS or not self._seats_checked:
            self._seats_checked = now
            self._other_seats = bool(other_active_sessions())
        return self._other_seats or bool(self.service_other_seats)

    def sample(self, input_idle_seconds: float, fullscreen: bool = False, audio_active: bool = False) -> dict[str, Any]:
        """One set of signals.  ``fullscreen`` / ``audio_active`` describe *this* session; they
        decide whether machine-wide signals may be counted here on a PC with several seats."""
        quns = notification_state()
        self.last_quns = quns
        busy = quns in (QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE, QUNS_BUSY)
        ours = machine_wide_counts(self._other_seats_active(), fullscreen, audio_active, busy)
        self.controller.counts = ours
        self.controller.poll()
        controller_idle = self.controller.idle_seconds()
        idle = input_idle_seconds
        if controller_idle is not None:
            idle = min(idle, controller_idle)
        es = execution_state() if ours else None
        return {
            "idle_seconds": idle,
            "input_idle_seconds": input_idle_seconds,
            "controller_idle_seconds": controller_idle,
            "display_state": self.display.display_state,
            "user_present": self.display.user_present,
            "screensaver": screensaver_running(),
            "fullscreen_d3d": quns == QUNS_RUNNING_D3D_FULL_SCREEN,
            "presentation_mode": quns == QUNS_PRESENTATION_MODE,
            "display_required": bool(es is not None and es & ES_DISPLAY_REQUIRED),
        }
