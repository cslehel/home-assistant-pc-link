"""Windows logon session enumeration and control (Terminal Services API).

Multiseat software gives every seat ("workplace") its own Windows session, so a
PC can have several sessions in use at once.  Everything here works per session id.

Seen from session 0, where the service runs, only the first seat is an active
session ("Console"); every further seat is listed as a *disconnected* session,
although somebody is sitting at it:

- ASTER names those sessions "Multiseat".
- Hydra Multi-Seat gives them no telling name, so while Hydra is on (its flag file
  or its running service) every disconnected session is taken for a seat.  A
  session left by "Switch user" looks the same from here and is taken for one too.

Such a session is taken for the active seat it is (:func:`multiseat_seat`).
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from ctypes import wintypes
from typing import Any, Callable, Optional

from .models import SessionInfo

log = logging.getLogger(__name__)

WTS_CURRENT_SERVER_HANDLE = 0
WTS_SESSION_INFO_EX = 25
_STATE_NAMES = {0: "active", 1: "connected", 2: "connected", 3: "other", 4: "disconnected", 5: "idle",
                6: "listen", 7: "reset", 8: "down", 9: "init"}
_NO_USER_STATES = {"listen", "reset", "down", "init"}   # never a logged-on user: not even asked about
ASTER_STATION = "multiseat"   # ASTER's station name for every workplace after the first
MULTISEAT_MODES = ("auto", "aster", "hydra", "off")
SEAT_NAMES = {"aster": "ASTER", "hydra": "Hydra Multi-Seat"}
HYDRA_SERVICE = "hydra-service"
HYDRA_FLAG = "hydra-armed.flag"      # in %SystemRoot% while Hydra's seats are on
HYDRA_CHECK_SECONDS = 60.0


def multiseat_seat(state: str, station: str, mode: str = "auto", hydra: bool = False) -> Optional[str]:
    """Whose seat is this session that Windows lists as disconnected? (pure; unit-tested)

    "aster", "hydra" or None.  Such a session is in use like any active one.  Its lock flag
    is not that of the seat either, so whether it is locked is left to its agent.  ``hydra``:
    Hydra Multi-Seat is on (``mode`` "hydra" takes every disconnected session for a seat
    without asking).  A remote desktop session is never a seat."""
    if state != "disconnected" or mode == "off":
        return None
    name = (station or "").strip().lower()
    if name == ASTER_STATION and mode in ("auto", "aster"):
        return "aster"
    if (mode == "hydra" or (mode == "auto" and hydra)) and not name.startswith("rdp"):
        return "hydra"
    return None


def aster_seat(state: str, station: str) -> bool:
    """Is this a workplace of ASTER that Windows lists as disconnected?"""
    return multiseat_seat(state, station, "aster") == "aster"


def _hydra_service_running() -> bool:
    try:
        import win32service  # type: ignore
        import win32serviceutil  # type: ignore

        return win32serviceutil.QueryServiceStatus(HYDRA_SERVICE)[1] == win32service.SERVICE_RUNNING
    except Exception:  # noqa: BLE001 - not installed, no pywin32, no access: not running as far as we know
        return False


class HydraDetector:
    """Is Hydra Multi-Seat on?  Its flag file, else its running service; asked at most once a minute."""

    def __init__(self, flag_path: Optional[str] = None, service_running: Callable[[], bool] = _hydra_service_running,
                 clock: Callable[[], float] = time.monotonic, interval: float = HYDRA_CHECK_SECONDS):
        self.flag_path = flag_path or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), HYDRA_FLAG)
        self._service_running = service_running
        self._clock = clock
        self._interval = interval
        self._checked: Optional[float] = None
        self._on = False

    def __call__(self) -> bool:
        now = self._clock()
        if self._checked is None or now - self._checked >= self._interval:
            self._checked = now
            try:
                on = os.path.exists(self.flag_path) or bool(self._service_running())
            except Exception:  # noqa: BLE001
                on = False
            if on != self._on:
                log.info("Hydra Multi-Seat is %s", "on: its disconnected sessions are seats" if on else "off")
            self._on = on
        return self._on


# WTS_SESSION_* event types delivered through SERVICE_CONTROL_SESSIONCHANGE
WTS_CONSOLE_CONNECT = 1
WTS_CONSOLE_DISCONNECT = 2
WTS_REMOTE_CONNECT = 3
WTS_REMOTE_DISCONNECT = 4
WTS_SESSION_LOGON = 5
WTS_SESSION_LOGOFF = 6
WTS_SESSION_LOCK = 7
WTS_SESSION_UNLOCK = 8
SESSION_EVENT_NAMES = {1: "console_connect", 2: "console_disconnect", 3: "remote_connect", 4: "remote_disconnect",
                       5: "logon", 6: "logoff", 7: "lock", 8: "unlock", 9: "remote_control"}

_FILETIME_EPOCH_DIFF = 116444736000000000


class WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", ctypes.c_long),
        ("WinStationName", wintypes.WCHAR * 33),
        ("UserName", wintypes.WCHAR * 21),
        ("DomainName", wintypes.WCHAR * 18),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class WTSINFOEX_LEVEL_W(ctypes.Union):
    _fields_ = [("WTSInfoExLevel1", WTSINFOEX_LEVEL1_W)]


class WTSINFOEXW(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", WTSINFOEX_LEVEL_W)]


class SessionQueryError(RuntimeError):
    """Windows could not list the sessions (or one of them) right now."""


ERROR_FILE_NOT_FOUND = 2
ERROR_CTX_WINSTATION_NOT_FOUND = 7022


def _session_gone(exc: Exception) -> bool:
    """Did the query fail because the session ended between listing and asking?"""
    return getattr(exc, "winerror", None) in (ERROR_FILE_NOT_FOUND, ERROR_CTX_WINSTATION_NOT_FOUND)


def _filetime_to_unix(value: int) -> Optional[float]:
    if not value:
        return None
    return (value - _FILETIME_EPOCH_DIFF) / 1e7


class WindowsSessions:
    """Session backend used by the service (requires pywin32)."""

    def __init__(self, lock_flag_inverted: bool = False, multiseat_mode: str = "auto"):
        if sys.platform != "win32":
            raise RuntimeError("WindowsSessions only works on Windows")
        import win32ts  # type: ignore

        self._wts = win32ts
        self._api = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self._api.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
        ]
        self._api.WTSQuerySessionInformationW.restype = wintypes.BOOL
        self._api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        self._api.WTSFreeMemory.restype = None
        self.lock_flag_inverted = lock_flag_inverted  # Windows 7 reports LOCK/UNLOCK swapped
        self._known: dict[int, SessionInfo] = {}   # the sessions of the last list, by id
        self._failed: set[int] = set()              # sessions that could not be queried (logged once)
        self.multiseat_mode = multiseat_mode        # multiseat.mode; the service sets it again on a reload
        self.hydra = HydraDetector()

    # -- multiseat ----------------------------------------------------------------
    def _hydra_on(self) -> bool:
        mode = getattr(self, "multiseat_mode", "auto")
        if mode == "hydra":
            return True
        detector = getattr(self, "hydra", None)
        return mode == "auto" and detector is not None and bool(detector())

    def multiseat(self) -> Optional[str]:
        """The multiseat software in use as far as the sessions show: "hydra", "aster" or None."""
        if getattr(self, "multiseat_mode", "auto") == "off":
            return None
        if self._hydra_on():
            return "hydra"
        seats = {s.seat for s in getattr(self, "_known", {}).values()}
        return "aster" if "aster" in seats else None

    # -- queries ----------------------------------------------------------------
    def _query_ex(self, session_id: int) -> Optional[dict[str, Any]]:
        buf = ctypes.c_void_p()
        size = wintypes.DWORD()
        ok = self._api.WTSQuerySessionInformationW(None, session_id, WTS_SESSION_INFO_EX, ctypes.byref(buf), ctypes.byref(size))
        if not ok or not buf.value:
            return None
        try:
            if size.value < ctypes.sizeof(WTSINFOEXW):
                return None
            info = ctypes.cast(buf, ctypes.POINTER(WTSINFOEXW)).contents
            if info.Level != 1:
                return None
            d = info.Data.WTSInfoExLevel1
            flags = d.SessionFlags
            if flags == 0:
                locked: Optional[bool] = True
            elif flags == 1:
                locked = False
            else:
                locked = None
            if locked is not None and self.lock_flag_inverted:
                locked = not locked
            idle = None
            if d.LastInputTime and d.CurrentTime:
                idle = max(0.0, (d.CurrentTime - d.LastInputTime) / 1e7)
            return {
                "user": d.UserName, "domain": d.DomainName, "station": d.WinStationName,
                "state": d.SessionState, "locked": locked, "idle_seconds": idle,
                "logon_time": _filetime_to_unix(d.LogonTime),
            }
        finally:
            self._api.WTSFreeMemory(buf)

    def list_sessions(self) -> list[SessionInfo]:
        """Every session with a user in it.

        Raises :class:`SessionQueryError` when Windows could not list them: an
        empty list would read as "everybody logged off" (agents dropped, logged-off
        events), so the caller keeps what it last knew instead.  A single session
        that cannot be queried keeps what was last known about it (and is left out
        when nothing was), the others are listed as usual."""
        wts = self._wts
        out: list[SessionInfo] = []
        try:
            raw = wts.WTSEnumerateSessions(WTS_CURRENT_SERVER_HANDLE)
        except Exception as exc:  # noqa: BLE001
            raise SessionQueryError(f"WTSEnumerateSessions failed: {exc}") from exc
        known: dict[int, SessionInfo] = getattr(self, "_known", {})
        failed: set[int] = getattr(self, "_failed", set())
        current: dict[int, SessionInfo] = {}
        mode = getattr(self, "multiseat_mode", "auto")
        hydra = self._hydra_on()
        for entry in raw:
            sid = int(entry["SessionId"])
            if sid == 0:
                continue  # session 0 hosts services only
            if _STATE_NAMES.get(int(entry.get("State", -1)), "other") in _NO_USER_STATES:
                continue  # a listener or a session being set up or torn down: nobody in it
            try:
                info = self._session(entry, sid, mode, hydra)
            except SessionQueryError as exc:
                # One session that cannot be asked about must not hide all the others; it
                # keeps what was last known about it (dropping it would read as a logoff).
                if sid not in failed:
                    failed.add(sid)
                    log.warning("%s; %s", exc, "keeping what was known about it" if sid in known
                                else "leaving it out for now")
                info = known.get(sid)
            else:
                failed.discard(sid)
            if info is not None:
                if info.seat and (sid not in known or known[sid].seat != info.seat):
                    log.info("session %s (%s): %s workplace listed as disconnected; taken as active",
                             sid, info.user, SEAT_NAMES.get(info.seat, info.seat))
                current[sid] = info
                out.append(info)
        self._known = current
        self._failed = failed & {int(e["SessionId"]) for e in raw}
        return out

    def _session(self, entry: Any, sid: int, mode: str = "auto", hydra: bool = False) -> Optional[SessionInfo]:
        """One listed session as a :class:`SessionInfo`; None when nobody is logged on to it.
        Raises :class:`SessionQueryError` when Windows would not tell."""
        wts = self._wts
        ex = None
        try:
            ex = self._query_ex(sid)
        except Exception as exc:  # noqa: BLE001
            log.debug("WTSINFOEX failed for session %s: %s", sid, exc)
        user = (ex or {}).get("user") or ""
        domain = (ex or {}).get("domain") or ""
        if not user:
            try:
                user = wts.WTSQuerySessionInformation(WTS_CURRENT_SERVER_HANDLE, sid, wts.WTSUserName) or ""
                domain = wts.WTSQuerySessionInformation(WTS_CURRENT_SERVER_HANDLE, sid, wts.WTSDomainName) or ""
            except Exception as exc:  # noqa: BLE001
                if ex is None and not _session_gone(exc):
                    # neither query answered: "nobody there" cannot be told from "could not ask"
                    raise SessionQueryError(f"session {sid} could not be queried: {exc}") from exc
                user = ""
        if not user:
            return None  # logon screen / no user
        state = _STATE_NAMES.get(int(entry["State"]), "other")
        station = str(entry.get("WinStationName") or (ex or {}).get("station") or "")
        locked = (ex or {}).get("locked")
        seat = multiseat_seat(state, station, mode, hydra)
        if seat:
            state, locked = "active", None
        return SessionInfo(
            session_id=sid, user=user.lower(), domain=domain, station=station,
            state=state, locked=locked, idle_seconds=(ex or {}).get("idle_seconds"),
            logon_time=(ex or {}).get("logon_time"), seat=seat,
        )

    # -- control -----------------------------------------------------------------
    def logoff(self, session_id: int, wait: bool = False) -> None:
        log.warning("logging off session %s", session_id)
        self._wts.WTSLogoffSession(WTS_CURRENT_SERVER_HANDLE, session_id, wait)

    def disconnect(self, session_id: int) -> None:
        log.info("disconnecting session %s", session_id)
        self._wts.WTSDisconnectSession(WTS_CURRENT_SERVER_HANDLE, session_id, False)

    def send_message(self, session_id: int, title: str, text: str, timeout: int = 30, critical: bool = False) -> None:
        style = 0x00000000 | (0x30 if critical else 0x40)  # MB_OK | MB_ICONWARNING / MB_ICONINFORMATION
        style |= 0x00040000  # MB_TOPMOST
        try:
            self._wts.WTSSendMessage(WTS_CURRENT_SERVER_HANDLE, session_id, title[:255], text[:1000], style, timeout, False)
        except Exception as exc:  # noqa: BLE001
            log.debug("WTSSendMessage failed for session %s: %s", session_id, exc)

    def user_token(self, session_id: int):
        return self._wts.WTSQueryUserToken(session_id)
