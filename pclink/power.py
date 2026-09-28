"""Power control (shutdown / reboot / sleep / hibernate) and Wake-on-LAN helpers."""
from __future__ import annotations

import ctypes
import logging
import re
import socket
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2})[:\-]?([0-9A-Fa-f]{2})[:\-]?([0-9A-Fa-f]{2})[:\-]?([0-9A-Fa-f]{2})[:\-]?([0-9A-Fa-f]{2})[:\-]?([0-9A-Fa-f]{2})$")


def magic_packet(mac: str) -> bytes:
    m = _MAC_RE.match(mac.strip())
    if not m:
        raise ValueError(f"invalid MAC address {mac!r}")
    raw = bytes(int(part, 16) for part in m.groups())
    return b"\xff" * 6 + raw * 16


def send_wol(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    """Send a Wake-on-LAN magic packet (lets HA wake *other* PCs through this one)."""
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
        sock.sendto(packet, (broadcast, 7))
    log.info("sent WOL packet to %s via %s", mac, broadcast)


def _enable_privilege(name: str = "SeShutdownPrivilege") -> None:
    if sys.platform != "win32":
        return
    try:
        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32security  # type: ignore

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY
        )
        luid = win32security.LookupPrivilegeValue(None, name)
        win32security.AdjustTokenPrivileges(token, False, [(luid, win32con.SE_PRIVILEGE_ENABLED)])
    except Exception as exc:  # noqa: BLE001
        log.debug("could not enable %s: %s", name, exc)


class PowerControl:
    def __init__(self, delay_seconds: int = 10):
        self.delay = max(0, int(delay_seconds))

    def _shutdown_exe(self, args: list[str]) -> None:
        if sys.platform != "win32":
            log.info("(dry-run) shutdown.exe %s", " ".join(args))
            return
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["shutdown.exe", *args], check=False, creationflags=creation, timeout=15)

    def shutdown(self, message: str = "Shutdown requested by Home Assistant", delay: Optional[int] = None) -> None:
        d = self.delay if delay is None else max(0, int(delay))
        log.warning("shutting down in %ss", d)
        self._shutdown_exe(["/s", "/f", "/t", str(d), "/c", message[:512]])

    def reboot(self, message: str = "Reboot requested by Home Assistant", delay: Optional[int] = None) -> None:
        d = self.delay if delay is None else max(0, int(delay))
        log.warning("rebooting in %ss", d)
        self._shutdown_exe(["/r", "/f", "/t", str(d), "/c", message[:512]])

    def cancel(self) -> None:
        self._shutdown_exe(["/a"])

    def sleep(self) -> bool:
        return self._suspend(hibernate=False)

    def hibernate(self) -> bool:
        return self._suspend(hibernate=True)

    def _suspend(self, hibernate: bool) -> bool:
        if sys.platform != "win32":
            log.info("(dry-run) %s", "hibernate" if hibernate else "sleep")
            return True
        _enable_privilege()
        try:
            # SetSuspendState(Hibernate, ForceCritical, DisableWakeEvent) - called directly
            # (not through rundll32, whose argument passing hibernates instead of sleeping).
            ok = ctypes.windll.powrprof.SetSuspendState(  # type: ignore[attr-defined]
                ctypes.c_bool(hibernate), ctypes.c_bool(True), ctypes.c_bool(False)
            )
            if not ok:
                log.error("SetSuspendState failed: %s", ctypes.GetLastError())  # type: ignore[attr-defined]
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            log.error("suspend failed: %s", exc)
            return False
