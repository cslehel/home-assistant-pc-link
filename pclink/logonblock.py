"""Prevent a local Windows account from logging on.

Two mechanisms are supported:

* ``logon_hours``      - sets the account's permitted logon hours to *none*
                         (the same mechanism as ``net user <name> /times:``).
                         Windows shows "Your account has time restrictions" at
                         the logon screen.  Existing sessions are not affected,
                         which is why the service logs the user off itself.
* ``disable_account``  - sets the account-disabled flag.  Heavier handed but
                         works even where logon hours are not honoured.
* ``logoff_only``      - do nothing at the account level; the service simply
                         logs the user off right after they log on.

Only local (SAM) accounts can be changed; domain accounts are reported and
left alone.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)

UF_ACCOUNTDISABLE = 0x0002
_HOURS_ALL = bytes([0xFF] * 21)
_HOURS_NONE = bytes([0x00] * 21)


class NullBlocker:
    method = "logoff_only"

    def block(self, user: str) -> bool:
        return True

    def unblock(self, user: str) -> bool:
        return True

    def is_blocked(self, user: str) -> Optional[bool]:
        return None


class LogonBlocker:
    def __init__(self, method: str = "logon_hours"):
        if method not in ("logon_hours", "disable_account"):
            raise ValueError(f"unsupported block method {method!r}")
        if sys.platform != "win32":
            raise RuntimeError("LogonBlocker only works on Windows")
        import win32net  # type: ignore

        self._net = win32net
        self.method = method

    # -- public --------------------------------------------------------------
    def block(self, user: str) -> bool:
        return self._set(user, blocked=True)

    def unblock(self, user: str) -> bool:
        return self._set(user, blocked=False)

    def is_blocked(self, user: str) -> Optional[bool]:
        try:
            if self.method == "disable_account":
                info = self._net.NetUserGetInfo(None, user, 1)
                return bool(int(info.get("flags", 0)) & UF_ACCOUNTDISABLE)
            info = self._net.NetUserGetInfo(None, user, 2)
            hours = _as_bytes(info.get("logon_hours"))
            if hours is None:
                return None
            return not any(hours)
        except Exception as exc:  # noqa: BLE001
            log.debug("is_blocked(%s) failed: %s", user, exc)
            return None

    # -- internals -----------------------------------------------------------
    def _set(self, user: str, blocked: bool) -> bool:
        try:
            if self.method == "disable_account":
                return self._set_disabled(user, blocked)
            return self._set_hours(user, blocked)
        except Exception as exc:  # noqa: BLE001
            log.error("could not %s user %s: %s", "block" if blocked else "unblock", user, exc)
            return False

    def _set_disabled(self, user: str, disabled: bool) -> bool:
        info = self._net.NetUserGetInfo(None, user, 1)
        flags = int(info.get("flags", 0))
        new_flags = flags | UF_ACCOUNTDISABLE if disabled else flags & ~UF_ACCOUNTDISABLE
        if new_flags != flags:
            self._net.NetUserSetInfo(None, user, 1008, {"flags": new_flags})
            log.info("account %s %s", user, "disabled" if disabled else "enabled")
        return True

    def _set_hours(self, user: str, blocked: bool) -> bool:
        """Set the permitted logon hours to none (blocked) or all.

        Written through ``net user <name> /times:`` (blank = never, ``all`` =
        always).  pywin32's NetUserSetInfo does not support the logon-hours
        level (1020), so net.exe is the supported way; its arguments are the
        same on every Windows language.  Reading still uses NetUserGetInfo.
        """
        current = self.is_blocked(user)
        if current is not None and current == blocked:
            return True
        net_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "net.exe")
        arg = "/times:" if blocked else "/times:all"
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run([net_exe, "user", user, arg], capture_output=True, text=True, timeout=20,
                                    creationflags=creation)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("could not run net.exe for %s: %s", user, exc)
            return False
        if result.returncode != 0:
            detail = " ".join((result.stdout + " " + result.stderr).split())
            log.error("could not %s %s: net.exe exit code %s: %s", "block" if blocked else "unblock", user,
                      result.returncode, detail)
            return False
        log.info("logon hours for %s set to %s", user, "none (blocked)" if blocked else "all")
        return True


def _as_bytes(value) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("latin-1", "replace")
    try:
        return bytes(value)
    except Exception:  # noqa: BLE001
        return None


def make_blocker(method: str):
    if method == "logoff_only" or sys.platform != "win32":
        if method != "logoff_only":
            log.warning("logon blocking (%s) unavailable on this platform; using logoff_only", method)
        return NullBlocker()
    return LogonBlocker(method)
