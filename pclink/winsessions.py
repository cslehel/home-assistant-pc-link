"""Windows logon session enumeration and control (Terminal Services API).

ASTER multiseat gives every "workplace" its own Windows session, so a PC can
have several *active* sessions at once.  Everything here works per session id.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Any, Optional

from .models import SessionInfo

log = logging.getLogger(__name__)

WTS_CURRENT_SERVER_HANDLE = 0
WTS_SESSION_INFO_EX = 25
_STATE_NAMES = {0: "active", 1: "connected", 2: "connected", 3: "other", 4: "disconnected", 5: "idle",
                6: "listen", 7: "reset", 8: "down", 9: "init"}

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


def _filetime_to_unix(value: int) -> Optional[float]:
    if not value:
        return None
    return (value - _FILETIME_EPOCH_DIFF) / 1e7


class WindowsSessions:
    """Session backend used by the service (requires pywin32)."""

    def __init__(self, lock_flag_inverted: bool = False):
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
        wts = self._wts
        out: list[SessionInfo] = []
        try:
            raw = wts.WTSEnumerateSessions(WTS_CURRENT_SERVER_HANDLE)
        except Exception as exc:  # noqa: BLE001
            log.error("WTSEnumerateSessions failed: %s", exc)
            return out
        for entry in raw:
            sid = int(entry["SessionId"])
            if sid == 0:
                continue  # session 0 hosts services only
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
                except Exception:  # noqa: BLE001
                    user = ""
            if not user:
                continue  # logon screen / no user
            state = _STATE_NAMES.get(int(entry["State"]), "other")
            out.append(
                SessionInfo(
                    session_id=sid, user=user.lower(), domain=domain,
                    station=str(entry.get("WinStationName") or (ex or {}).get("station") or ""),
                    state=state, locked=(ex or {}).get("locked"), idle_seconds=(ex or {}).get("idle_seconds"),
                    logon_time=(ex or {}).get("logon_time"),
                )
            )
        return out

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
