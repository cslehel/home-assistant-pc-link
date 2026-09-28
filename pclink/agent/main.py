"""Per-session agent entry point.

Launched by the service as ``pythonw.exe -m pclink.agent``.  Reads its
configuration from environment variables set by the launcher, connects to the
service over localhost, samples activity cheaply and shows the tray icon.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from ..ipc import AgentClient
from ..logsetup import setup_logging
from ..models import AgentReport
from .browser_url import BrowserUrlReader
from .tray import TrayIcon
from .win_activity import AudioActivity, Win32Activity, lock_workstation

log = logging.getLogger(__name__)


def _program_data() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "PCLink")
    return os.path.join(os.path.expanduser("~"), ".pclink")


class Agent:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.session_id = int(os.environ.get("PCLINK_SESSION", "-1"))
        self.user = os.environ.get("PCLINK_USER", "")
        self.token = os.environ.get("PCLINK_AGENT_TOKEN", "")
        self.host = os.environ.get("PCLINK_IPC_HOST", "127.0.0.1")
        self.port = int(os.environ.get("PCLINK_IPC_PORT", "47391"))
        self.poll = float(settings.get("poll_seconds", 2.0))
        self.heartbeat = float(settings.get("heartbeat_seconds", 15.0))
        self.audio_poll = float(settings.get("audio_poll_seconds", 6.0))
        self.browsers = {b.lower() for b in settings.get("browsers", [])}
        self.url_method = settings.get("url_method", "uia")
        self.want_audio = bool(settings.get("audio", True))
        self._stop = threading.Event()
        self.activity = Win32Activity()
        self.audio = AudioActivity(self.want_audio) if self.want_audio else None
        self.urls = BrowserUrlReader(self.url_method, settings.get("group_sites_by", "registrable")) if self.url_method != "off" else None
        self.tray = TrayIcon(tooltip=f"{settings.get('device_name', 'PC')} - Home Assistant PC Link",
                             on_open=self._show_status) if settings.get("tray_icon", True) else None
        self._last_report = 0.0
        self._last_signature = None
        self._audio_active = False
        self._last_audio_poll = 0.0
        self._status_text = "Connecting..."
        self.client = AgentClient(
            self.host, self.port,
            hello={"session_id": self.session_id, "user": self.user, "token": self.token, "pid": os.getpid()},
            on_message=self._on_message, reconnect_seconds=5.0,
        )

    # -- lifecycle -----------------------------------------------------------
    def run(self) -> int:
        if self.session_id < 0 or not self.token:
            log.error("agent launched without session/token environment; refusing to run")
            return 2
        log.info("agent starting: session=%s user=%s pid=%s", self.session_id, self.user, os.getpid())
        if self.tray is not None:
            self.tray.start()
            self.tray.update("offline", detail="Connecting to service...")
        self.client.start()
        try:
            self._loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.client.stop()
            if self.tray is not None:
                self.tray.stop()
        return 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.client.rejected.is_set():
                log.warning("service rejected this agent; exiting so it can relaunch cleanly")
                return
            started = time.monotonic()
            try:
                self._sample_and_report()
            except Exception:  # noqa: BLE001
                log.exception("sample failed")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.5, self.poll - elapsed))

    # -- sampling ------------------------------------------------------------
    def _sample_and_report(self) -> None:
        now = time.monotonic()
        locked = self.activity.is_locked()
        idle = self.activity.idle_seconds()
        fg = None if locked else self.activity.foreground()
        url = domain = None
        if fg is not None and self.urls is not None and fg.name and fg.name.lower() in self.browsers:
            try:
                url, domain = self.urls.url_and_domain(fg.pid and self._fg_hwnd(), fg.title)
            except Exception:  # noqa: BLE001
                url = domain = None
        if self.audio is not None and now - self._last_audio_poll >= self.audio_poll:
            self._audio_active = self.audio.active()
            self._last_audio_poll = now
        report = AgentReport(
            session_id=self.session_id, ts=time.time(), idle_seconds=idle, locked=locked,
            foreground=fg, url=url, domain=domain, audio_active=self._audio_active,
        )
        signature = report.signature()
        changed = signature != self._last_signature
        due = time.time() - self._last_report >= self.heartbeat
        if (changed or due) and self.client.connected.is_set():
            if self.client.send(report.to_dict()):
                self._last_report = time.time()
                self._last_signature = signature
        self._update_tray_connection()

    def _fg_hwnd(self) -> int:
        try:
            import ctypes

            return int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:  # noqa: BLE001
            return 0

    # -- tray ----------------------------------------------------------------
    def _update_tray_connection(self) -> None:
        if self.tray is None:
            return
        if not self.client.connected.is_set():
            self.tray.update("offline", detail="Reconnecting to service...")

    def _apply_status(self, data: dict[str, Any]) -> None:
        text = str(data.get("text", ""))
        self._status_text = text or self._status_text
        remaining = data.get("remaining_minutes")
        if self.tray is None:
            return
        if "paused" in text.lower() or "outside" in text.lower() or (isinstance(remaining, (int, float)) and remaining <= 0):
            state = "blocked"
        elif isinstance(remaining, (int, float)) and remaining <= 15:
            state = "warn"
        else:
            state = "ok"
        self.tray.update(state, detail=text)

    def _show_status(self) -> None:
        if self.tray is not None:
            self.tray.notify("Home Assistant PC Link", self._status_text or "No time limit", "info")

    # -- messages from the service -------------------------------------------
    def _on_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "status":
            self._apply_status(msg)
        elif kind == "notify":
            self._notify(msg.get("title", "Home Assistant"), msg.get("message", ""), msg.get("level", "info"))
        elif kind == "lock":
            log.info("service requested session lock")
            lock_workstation()
        elif kind == "run":
            self._run_command(msg.get("command", ""))
        elif kind == "hello_ok":
            if self.tray is not None:
                self.tray.update("ok", detail="Connected")
        elif kind == "exit":
            log.info("service asked agent to exit")
            self._stop.set()

    def _notify(self, title: str, message: str, level: str) -> None:
        if self.tray is not None:
            self.tray.notify(title, message, level)
        else:
            self._message_box(title, message, level)

    def _message_box(self, title: str, message: str, level: str) -> None:
        if sys.platform != "win32":
            log.info("notify: %s - %s", title, message)
            return
        try:
            import ctypes

            style = 0x40 | 0x1000  # MB_ICONINFORMATION | MB_SYSTEMMODAL
            if level == "critical":
                style = 0x30 | 0x1000
            ctypes.windll.user32.MessageBoxW(0, message, title, style)
        except Exception:  # noqa: BLE001
            pass

    def _run_command(self, command: str) -> None:
        if not command:
            return
        log.info("running command from service: %s", command[:120])
        try:
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(command, shell=True, creationflags=creation)  # noqa: S602 - operator-configured
        except Exception as exc:  # noqa: BLE001
            log.error("command failed: %s", exc)


def _load_settings() -> dict[str, Any]:
    raw = os.environ.get("PCLINK_AGENT_CONFIG")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("bad PCLINK_AGENT_CONFIG; using defaults")
    return {}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pclink agent")
    parser.add_argument("--log-level", default=None)
    args, _ = parser.parse_known_args(argv or [])
    settings = _load_settings()
    level = args.log_level or settings.get("log_level", "INFO")
    log_dir = os.path.join(_program_data(), "logs")
    session = os.environ.get("PCLINK_SESSION", "x")
    setup_logging(log_dir, level, filename=f"agent-{session}.log", max_bytes=1_000_000, backups=2)
    return Agent(settings).run()


if __name__ == "__main__":
    sys.exit(main())
