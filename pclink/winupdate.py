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

_SUBKEYS = "*subkeys*"   # marker: the key counts only when it has at least one subkey

# Browser updaters, antivirus and installers queue file renames all the time; on its own
# that is no reason to tell anybody to restart.  It is listed next to a real reason only.
WEAK_REBOOT_REASONS = {"pending file renames"}

_REBOOT_KEYS = [
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired", None, "windows update"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending", None, "component servicing"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update", "PostRebootReporting", "post-reboot reporting"),
    (r"SYSTEM\CurrentControlSet\Control\Session Manager", "PendingFileRenameOperations", "pending file renames"),
    # this key exists (empty) on healthy systems; only a subkey means a service registration is pending
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Services\Pending", _SUBKEYS, "update services"),
]


def reboot_verdict(reasons: list[str]) -> tuple[bool, list[str]]:
    """(pending, reasons to show) from everything found in the registry (pure; unit-tested)."""
    strong = [r for r in reasons if r not in WEAK_REBOOT_REASONS]
    return (True, list(reasons)) if strong else (False, [])


def reboot_pending() -> tuple[Optional[bool], list[str]]:
    """(pending, reasons); pending is None when it cannot be determined."""
    if not _IS_WIN:
        return None, []
    try:
        import winreg  # type: ignore
    except ImportError:
        return None, []
    return reboot_verdict(_reboot_reasons(winreg))


def _reboot_reasons(winreg) -> list[str]:
    reasons: list[str] = []
    for path, value, label in _REBOOT_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                if value is None:
                    reasons.append(label)
                elif value == _SUBKEYS:
                    if winreg.QueryInfoKey(key)[0] > 0:
                        reasons.append(label)
                else:
                    data, _ = winreg.QueryValueEx(key, value)
                    if data:
                        reasons.append(label)
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.debug("registry read %s failed: %s", path, exc)
    return reasons


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
        self._search_requested = threading.Event()   # set by check_now(); survives a search already running
        self._recheck_at: Optional[float] = None     # set by install(): when to search again
        self._installing = threading.Lock()          # held while UsoClient is being started

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
        # A flag rather than "next search = now": a search that is already running would
        # otherwise overwrite the request when it finishes.
        self._search_requested.set()
        self._wake.set()
        # UsoClient can take a while; never make the caller (the main loop) wait for it.
        threading.Thread(target=self._uso, args=("StartScan",), name="winupdate-scan", daemon=True).start()

    INSTALL_RECHECK_SECONDS = 900.0   # search again this long after an install was started

    def install(self) -> bool:
        """Start downloading and installing; returns at once.

        True means the request was accepted, not that anything was installed:
        UsoClient can take a minute to answer, and the caller is the main loop.
        The status only becomes "installing" once UsoClient really started both
        steps (see ``_install``).
        """
        if not self.cfg.allow_install:
            log.warning("update install requested but windows_update.allow_install is off")
            return False
        if _IS_WIN and not os.path.isfile(self._uso_path()):
            log.error("update install requested but UsoClient.exe was not found")
            return False
        if self._installing.locked():
            log.info("update install already being started")
            return True
        threading.Thread(target=self._install, name="winupdate-install", daemon=True).start()
        return True

    def _install(self) -> bool:
        if not self._installing.acquire(blocking=False):
            return False
        try:
            ok = self._uso("StartDownload") and self._uso("StartInstall")
            with self._lock:
                details = dict(self._state["update_details"])
                if ok:
                    details["status"] = "installing"
                else:
                    details["error"] = "the update install could not be started"
                self._state["update_details"] = details
            if ok:
                # "installing" is only known to end by searching again; do that soon instead of
                # leaving the status until the next regular check hours later.
                self._recheck_at = time.monotonic() + self.INSTALL_RECHECK_SECONDS
                self._wake.set()
            return ok
        finally:
            self._installing.release()

    # -- internals ----------------------------------------------------------------------------
    def _loop(self) -> None:
        self._next_search = time.monotonic() + 60  # let the service settle first
        while not self._stop.is_set():
            now = time.monotonic()
            recheck = self._recheck_at is not None and now >= self._recheck_at
            if self._search_requested.is_set() or recheck or now >= self._next_search:
                self._search_requested.clear()   # cleared *before* searching: a request made meanwhile is kept
                if recheck:
                    self._recheck_at = None
                self._search()
                self._next_search = time.monotonic() + max(0.25, self.cfg.check_interval_hours) * 3600
            self._wake.wait(30)
            self._wake.clear()

    def _search(self) -> None:
        if not _IS_WIN:
            return
        with self._lock:
            # a new dict: the old one may be in a snapshot another thread is serialising
            self._state["update_details"] = dict(self._state["update_details"], status="checking")
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore

            pythoncom.CoInitialize()
            try:
                # All COM objects live inside _query_updates(), so they are released when it
                # returns - before CoUninitialize() below, not after it.  A failed search is
                # turned into text here for the same reason: the exception's traceback keeps
                # that frame (and its COM objects) alive until it is dropped.
                failure = None
                try:
                    updates, last_install = self._query_updates(win32com.client)
                except Exception as exc:  # noqa: BLE001
                    failure = str(exc) or type(exc).__name__
                if failure is not None:
                    raise RuntimeError(failure)
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

    @staticmethod
    def _query_updates(client) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Run the search and copy everything needed into plain Python values."""
        session = client.Dispatch("Microsoft.Update.Session")
        searcher = session.CreateUpdateSearcher()
        result = searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
        updates: list[dict[str, Any]] = []
        found = result.Updates
        for i in range(found.Count):
            u = found.Item(i)
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
            au = client.Dispatch("Microsoft.Update.AutoUpdate")
            d = au.Results.LastInstallationSuccessDate
            if d:
                last_install = _to_iso(d)
        except Exception:  # noqa: BLE001
            pass
        return updates, last_install

    @staticmethod
    def _uso_path() -> str:
        return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "UsoClient.exe")

    def _uso(self, verb: str) -> bool:
        if not _IS_WIN:
            log.info("(dry-run) UsoClient %s", verb)
            return True
        try:
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            done = subprocess.run([self._uso_path(), verb], check=False, timeout=60, creationflags=creation,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            log.error("UsoClient %s failed: %s", verb, exc)
            return False
        if done.returncode != 0:
            log.error("UsoClient %s ended with exit code %s", verb, done.returncode)
            return False
        log.info("UsoClient %s issued", verb)
        return True

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
