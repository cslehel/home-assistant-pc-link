"""Windows Update status (pending updates, reboot required) and triggered installs.

* **Reboot pending** is read from the registry (cheap, cached for a minute).
* **Pending updates** come from the Windows Update Agent COM API
  (``Microsoft.Update.Session``).  A search can take tens of seconds and may
  talk to Microsoft, so it runs on its own thread every few hours and never
  blocks the main loop.
* **Triggers** use ``UsoClient.exe`` (Windows 10/11): ``StartScan``,
  ``StartDownload``, ``StartInstall``.  Installing is only exposed when
  ``windows_update.allow_install`` is on.

Everything degrades to "unknown" off Windows or when the APIs are unavailable.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .config import WindowsUpdateConfig

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

_REBOOT_KEYS = [
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired", None, "windows update"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending", None, "component servicing"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update", "PostRebootReporting", "post-reboot reporting"),
    (r"SYSTEM\CurrentControlSet\Control\Session Manager", "PendingFileRenameOperations", "pending file renames"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Services\Pending", None, "update services"),
]


def reboot_pending() -> tuple[Optional[bool], list[str]]:
    """(pending, reasons); pending is None when it cannot be determined."""
    if not _IS_WIN:
        return None, []
    try:
        import winreg  # type: ignore
    except ImportError:
        return None, []
    reasons: list[str] = []
    for path, value, label in _REBOOT_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                if value is None:
                    reasons.append(label)
                else:
                    data, _ = winreg.QueryValueEx(key, value)
                    if data:
                        reasons.append(label)
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.debug("registry read %s failed: %s", path, exc)
    return bool(reasons), reasons


class WindowsUpdate:
    def __init__(self, cfg: WindowsUpdateConfig, on_change: Optional[Callable[[dict[str, Any]], None]] = None):
        self.cfg = cfg
        self._on_change = on_change
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: dict[str, Any] = {
            "pending_updates": None, "update_details": {"updates": [], "error": None, "status": "unknown"},
            "reboot_pending": None, "reboot_reasons": [], "last_update_check": None, "last_update_install": None,
        }
        self._reboot_checked = 0.0
        self._next_search = 0.0

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        if not self.cfg.enabled:
            return
        self._thread = threading.Thread(target=self._loop, name="winupdate", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- public ---------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._reboot_checked >= 60:
            pending, reasons = reboot_pending()
            with self._lock:
                changed = pending != self._state.get("reboot_pending")
                self._state["reboot_pending"] = pending
                self._state["reboot_reasons"] = reasons
            self._reboot_checked = now
            if changed and pending and self._on_change:
                self._emit({"event": "reboot_pending"})
        with self._lock:
            return dict(self._state)

    def check_now(self) -> None:
        """Ask for a fresh search (and a UsoClient scan) as soon as possible."""
        self._next_search = 0.0
        self._wake.set()
        self._uso("StartScan")

    def install(self) -> bool:
        if not self.cfg.allow_install:
            log.warning("update install requested but windows_update.allow_install is off")
            return False
        ok = self._uso("StartDownload") and self._uso("StartInstall")
        if ok:
            with self._lock:
                self._state["update_details"]["status"] = "installing"
        return ok

    # -- internals ----------------------------------------------------------------------------
    def _loop(self) -> None:
        self._next_search = time.monotonic() + 60  # let the service settle first
        while not self._stop.is_set():
            if time.monotonic() >= self._next_search:
                self._search()
                self._next_search = time.monotonic() + max(0.25, self.cfg.check_interval_hours) * 3600
            self._wake.wait(30)
            self._wake.clear()

    def _search(self) -> None:
        if not _IS_WIN:
            return
        with self._lock:
            self._state["update_details"]["status"] = "checking"
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore

            pythoncom.CoInitialize()
            try:
                session = win32com.client.Dispatch("Microsoft.Update.Session")
                searcher = session.CreateUpdateSearcher()
                result = searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
                updates = []
                for i in range(result.Updates.Count):
                    u = result.Updates.Item(i)
                    kbs = []
                    try:
                        kbs = [f"KB{k}" for k in u.KBArticleIDs]
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        size_mb = round(float(u.MaxDownloadSize) / 1024 / 1024, 1)
                    except Exception:  # noqa: BLE001
                        size_mb = None
                    updates.append({
                        "title": str(u.Title), "kb": kbs, "severity": str(getattr(u, "MsrcSeverity", "") or ""),
                        "downloaded": bool(getattr(u, "IsDownloaded", False)), "size_mb": size_mb,
                    })
                last_install = None
                try:
                    au = win32com.client.Dispatch("Microsoft.Update.AutoUpdate")
                    d = au.Results.LastInstallationSuccessDate
                    if d:
                        last_install = _to_iso(d)
                except Exception:  # noqa: BLE001
                    pass
                with self._lock:
                    previous = self._state.get("pending_updates")
                    self._state["pending_updates"] = len(updates)
                    self._state["update_details"] = {"updates": updates, "error": None, "status": "ok",
                                                     "checked": _now_iso()}
                    self._state["last_update_check"] = _now_iso()
                    if last_install:
                        self._state["last_update_install"] = last_install
                log.info("Windows Update: %d pending update(s)", len(updates))
                if updates and previous != len(updates):
                    self._emit({"event": "updates_available", "count": len(updates)})
            finally:
                pythoncom.CoUninitialize()
        except Exception as exc:  # noqa: BLE001
            log.warning("Windows Update search failed: %s", exc)
            with self._lock:
                self._state["update_details"] = {"updates": [], "error": str(exc), "status": "error"}
                self._state["last_update_check"] = _now_iso()

    def _uso(self, verb: str) -> bool:
        if not _IS_WIN:
            log.info("(dry-run) UsoClient %s", verb)
            return True
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "UsoClient.exe")
        try:
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run([exe, verb], check=False, timeout=60, creationflags=creation)
            log.info("UsoClient %s issued", verb)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("UsoClient %s failed: %s", verb, exc)
            return False

    def _emit(self, data: dict[str, Any]) -> None:
        if self._on_change:
            try:
                self._on_change(data)
            except Exception:  # noqa: BLE001
                log.debug("update change handler failed", exc_info=True)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _to_iso(value: Any) -> Optional[str]:
    try:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)
    except Exception:  # noqa: BLE001
        return None
