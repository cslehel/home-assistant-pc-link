"""Per-session agent entry point.

Launched by the service as ``pythonw.exe -m pclink.agent``.  Reads its
configuration from environment variables set by the launcher, connects to the
service over localhost, samples activity and presence cheaply and shows the
tray icon.  It also carries out a few *harmless* requests from the service
inside the user's desktop (close an app or tab, set the volume, take a
screenshot, show a balloon) - it never decides anything itself.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import fields
from typing import Any, Callable, Optional

from .. import __version__
from .. import ipc
from ..ipc import AgentClient
from ..logsetup import setup_logging
from ..models import AgentReport, MediaInfo
from .browser_url import BrowserUrlReader
from .capture import MAX_CAPTURE_WIDTH, capture_screen, image_budget
from .coreaudio import CoreAudio
from .media import MediaInfoReader
from .tray import RequestGuard, TrayIcon, request_label, time_request_options
from .win_activity import Win32Activity, close_app, close_tab, lock_workstation, set_display_power
from .win_presence import PresenceSignals, toast_suppressed

log = logging.getLogger(__name__)


MAX_MESSAGE_BOXES = 3   # message boxes open at once; further ones are dropped (and logged)
WATCHDOG_SECONDS = 60.0         # no completed sample for this long: the poll hangs, start over
WATCHDOG_CHECK_SECONDS = 5.0
WATCHDOG_EXIT_CODE = 3
SERVICE_CHECK_SECONDS = 10.0    # how often the service's process is looked at when it cannot be waited on
MAX_QUEUED_COMMANDS = 64        # requests from the service waiting for the command thread
CLOSE_BAD_REQUEST = "failed bad request"   # a close request the agent could not read (the service reads it as "not closed")
_REPORT_FIELDS = {f.name for f in fields(AgentReport)}

# requests that are carried out on the command thread, never on the thread reading the connection
_COMMANDS = {"notify", "lock", "run", "close", "volume", "mute", "display", "screenshot"}


def as_int(value: Any, default: int = 0) -> int:
    """A field of a request as an integer; ``default`` for anything that is not one (pure; unit-tested)."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return int(number)


def service_gone(exists: bool, created: Optional[float], first_created: Optional[float],
                 own_created: Optional[float]) -> bool:
    """Has the service that started this agent ended? (pure; unit-tested)

    ``created`` is the creation time of the process now holding the service's
    pid, ``first_created`` what it was when the agent started.  Windows reuses
    pids, so a process created later than this agent - or another one than at
    the start - is not the service.
    """
    if not exists:
        return True
    if created is None:
        return False
    if first_created is not None and abs(created - first_created) > 1.0:
        return True
    return own_created is not None and created > own_created + 1.0


def plain_signals(input_idle: float) -> dict[str, Any]:
    """The presence part of a report when the presence signals could not be read: keyboard / mouse only."""
    return {"idle_seconds": input_idle, "input_idle_seconds": input_idle, "controller_idle_seconds": None,
            "display_state": None, "user_present": None, "screensaver": False, "fullscreen_d3d": False,
            "presentation_mode": False, "display_required": False}


def _guarded(what: str, fn: Callable[[], Any], fallback: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        log.debug("%s could not be read", what, exc_info=True)
        return fallback


class ServiceWatch:
    """Calls ``on_gone`` once when the service process that launched the agent has ended.

    A left-over agent would otherwise keep reconnecting to whatever takes the
    port next.  The process is waited on (``SYNCHRONIZE``) where Windows allows
    it, and looked at every few seconds (with its creation time, against pid
    reuse) where it does not.
    """

    def __init__(self, pid: int, on_gone: Callable[[], None], period: float = SERVICE_CHECK_SECONDS):
        self.pid = int(pid)
        self.on_gone = on_gone
        self.period = period
        self._stop = threading.Event()
        self._first: Optional[float] = None
        self._own: Optional[float] = None

    def start(self) -> None:
        if self.pid <= 0:
            return
        threading.Thread(target=self._run, name="service-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _process(self) -> tuple[bool, Optional[float]]:
        """(the pid exists, its creation time or None when unknown)."""
        try:
            import psutil  # type: ignore
        except Exception:  # noqa: BLE001
            return True, None
        try:
            return True, float(psutil.Process(self.pid).create_time())
        except psutil.NoSuchProcess:
            return False, None
        except Exception:  # noqa: BLE001 - access denied and the like: it is there
            return bool(psutil.pid_exists(self.pid)), None

    def check(self) -> bool:
        exists, created = self._process()
        return service_gone(exists, created, self._first, self._own)

    @staticmethod
    def own_created() -> Optional[float]:
        try:
            import psutil  # type: ignore

            return float(psutil.Process(os.getpid()).create_time())
        except Exception:  # noqa: BLE001
            return None

    def _run(self) -> None:
        self._own = self.own_created()
        _exists, self._first = self._process()
        if self._wait_handle():
            return
        while not self._stop.is_set():
            if self.check():
                self._gone()
                return
            self._stop.wait(self.period)

    def _wait_handle(self) -> bool:
        """Wait for the process to end; False when it cannot be opened for that (then it is polled)."""
        if sys.platform != "win32" or self.check():
            return False
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x00100000, False, self.pid)   # SYNCHRONIZE
            if not handle:
                return False
            try:
                if self.check():   # the pid may have been reused just before it was opened
                    return False
                while not self._stop.is_set():
                    if kernel32.WaitForSingleObject(handle, 5000) == 0:   # WAIT_OBJECT_0: it ended
                        self._gone()
                        return True
            finally:
                kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            log.debug("cannot wait on the service process", exc_info=True)
            return False

    def _gone(self) -> None:
        log.warning("the service (pid %s) that started this agent has ended; exiting", self.pid)
        try:
            self.on_gone()
        except Exception:  # noqa: BLE001
            log.exception("service watch callback failed")

# how a notification reached the user (reported back to the service)
VIA_BALLOON = "balloon"   # a toast from the tray icon
VIA_BOX = "box"           # a message box on top of everything
VIA_QUIET = "quiet"       # Windows keeps toasts off the screen right now: it is in the notification centre only
VIA_NONE = "none"         # nothing could be shown


class Watchdog:
    """Notices a poll loop that stopped completing samples (pure logic; unit-tested).

    A call into another process (the media controls, a browser's UI Automation)
    can block for good.  The agent would then stay connected but report nothing,
    and every per-app rule would silently stop.  Leaving the process is the
    cure: the service starts a fresh agent.
    """

    def __init__(self, limit: float = WATCHDOG_SECONDS, period: float = WATCHDOG_CHECK_SECONDS):
        self.limit = float(limit)
        self.period = float(period)
        self._beat: Optional[float] = None
        self._checked: Optional[float] = None

    def beat(self, now: float) -> None:
        self._beat = now

    def expired(self, now: float) -> bool:
        checked, self._checked = self._checked, now
        if self.limit <= 0:
            return False
        if self._beat is None:
            self._beat = now
            return False
        if checked is not None and now - checked > self.period * 3:
            # the checker itself was not running (the PC slept): the poll gets a fresh start too
            self._beat = now
            return False
        return now - self._beat > self.limit


def notify_plan(level: str, tray_ok: bool, suppressed: bool) -> tuple[bool, bool]:
    """(send a balloon, open a message box) for one notification (pure; unit-tested).

    A balloon is the polite way.  It is not enough when Windows keeps toasts off
    the screen (a full-screen game, presentation mode, "do not disturb") and the
    message is a warning - "5 minutes left" must be seen *before* the logoff -
    or when there is no tray icon to send it from.
    """
    important = level in ("warning", "critical")
    return bool(tray_ok), bool(not tray_ok or (important and suppressed))


def notify_outcome(balloon_sent: bool, box_opened: bool, suppressed: bool) -> str:
    """How the notification reached the user (pure; unit-tested)."""
    if box_opened:
        return VIA_BOX
    if balloon_sent:
        return VIA_QUIET if suppressed else VIA_BALLOON
    return VIA_NONE


def message_box_style(level: str) -> int:
    """MessageBox flags: on top of everything, never taking the keyboard from a game."""
    MB_ICONWARNING, MB_ICONINFORMATION, MB_SYSTEMMODAL, MB_TOPMOST = 0x30, 0x40, 0x1000, 0x40000
    if level == "critical":
        return MB_ICONWARNING | MB_SYSTEMMODAL   # the last warning before a logoff
    if level == "warning":
        return MB_ICONWARNING | MB_TOPMOST
    return MB_ICONINFORMATION | MB_TOPMOST      # MB_SETFOREGROUND is deliberately absent


def child_environment(env: dict[str, str]) -> dict[str, str]:
    """The environment for programs the agent starts: its own without the PCLINK_* settings.

    (The agent's token and the service's port are of no use to a launched
    program, and it would hand them on to everything it starts.)
    """
    return {k: v for k, v in env.items() if not k.upper().startswith("PCLINK_")}


def _log_dir() -> str:
    """A per-user log folder.

    The agent runs as the session's user, who has no write access to
    ``%ProgramData%\\PCLink`` (SYSTEM / Administrators only), so its log lives
    under the user's own profile.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return os.path.join(base, "PCLink", "logs")
        import tempfile

        return os.path.join(tempfile.gettempdir(), "PCLink")
    return os.path.join(os.path.expanduser("~"), ".pclink", "logs")


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
        self.media_poll = float(settings.get("media_poll_seconds", 6.0))
        self.browsers = {b.lower() for b in settings.get("browsers", [])}
        self.url_method = settings.get("url_method", "uia")
        self.want_audio = bool(settings.get("audio", True))
        self.volume_control = bool(settings.get("volume_control", True))
        self._stop = threading.Event()
        self.activity = Win32Activity()
        self.audio = CoreAudio(self.want_audio or self.volume_control)
        self.presence = PresenceSignals(controller=bool(settings.get("controller_input", True)))
        self.media = MediaInfoReader(bool(settings.get("now_playing", True)), settings.get("media_apps", []), list(self.browsers))
        self.urls = BrowserUrlReader(self.url_method, settings.get("group_sites_by", "registrable")) if self.url_method != "off" else None
        self.tray = TrayIcon(tooltip=f"{settings.get('device_name', 'PC')} - Home Assistant PC Link",
                             on_open=self._show_status,
                             on_time_request=self._request_time) if settings.get("tray_icon", True) else None
        self._request_guard = RequestGuard()
        limit = float(settings.get("poll_watchdog_seconds", WATCHDOG_SECONDS))
        # never shorter than a few polls, whatever poll interval is configured (0 switches it off)
        self.watchdog = Watchdog(max(limit, self.poll * 5) if limit > 0 else 0.0)
        self._last_report = 0.0
        self._last_signature = None
        self._audio_active = False
        self._volume: Optional[int] = None
        self._muted: Optional[bool] = None
        self._last_audio_poll = 0.0
        self._media: Optional[MediaInfo] = None
        self._last_media_poll = 0.0
        self._status_text = "Connecting..."
        self._last_status: Optional[dict[str, Any]] = None   # the service's last "status", shown again after a reconnect
        self._tray_connected = False
        self._boxes_open = 0
        self._boxes_lock = threading.Lock()
        self._commands: Optional[queue.Queue] = None   # set up by run(); without it requests are handled at once
        self._shot_lock = threading.Lock()             # one screenshot at a time
        self.service_watch = ServiceWatch(as_int(os.environ.get("PCLINK_SERVICE_PID"), 0), self._stop.set)
        # the token is not sent: it signs the answers to the service's challenge (pclink/ipc.py)
        self.client = AgentClient(
            self.host, self.port,
            hello={"session_id": self.session_id, "user": self.user, "pid": os.getpid(),
                   "agent_pid": os.getpid(), "version": __version__},
            on_message=self._on_message, reconnect_seconds=5.0, token=self.token,
        )

    # -- lifecycle -----------------------------------------------------------
    def run(self) -> int:
        if self.session_id < 0 or not self.token:
            log.error("agent launched without session/token environment; refusing to run")
            return 2
        log.info("agent %s starting: session=%s user=%s pid=%s", __version__, self.session_id, self.user, os.getpid())
        if self.tray is not None:
            self.tray.start()
            self.tray.update("offline", detail="Connecting to service...")
        try:
            self.presence.start()
        except Exception:  # noqa: BLE001
            log.debug("presence signals unavailable", exc_info=True)
        self.service_watch.start()
        self._commands = queue.Queue(maxsize=MAX_QUEUED_COMMANDS)
        threading.Thread(target=self._command_loop, name="commands", daemon=True).start()
        self.client.start()
        threading.Thread(target=self._watch, name="watchdog", daemon=True).start()
        try:
            self._loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self.service_watch.stop()
            if self._commands is not None:
                try:
                    self._commands.put_nowait(None)   # wakes the command thread so it ends
                except queue.Full:
                    pass
            self.client.stop()
            self.presence.stop()
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
            self.watchdog.beat(time.monotonic())
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.5, self.poll - elapsed))

    def _watch(self) -> None:
        """Leave the process when the poll loop hangs; the service starts a new agent."""
        while not self._stop.wait(self.watchdog.period):
            if self.watchdog.expired(time.monotonic()):
                log.error("no activity sample completed for %d s (a call into another program hangs); "
                          "exiting so the service starts a fresh agent", int(self.watchdog.limit))
                logging.shutdown()
                os._exit(WATCHDOG_EXIT_CODE)   # the hanging thread cannot be stopped any other way

    # -- sampling ------------------------------------------------------------
    def _sample_and_report(self) -> None:
        now = time.monotonic()
        locked = self.activity.is_locked()
        input_idle = self.activity.idle_seconds()
        try:
            fg = None if locked else self.activity.foreground()
        except Exception:  # noqa: BLE001
            log.debug("foreground window could not be read", exc_info=True)
            fg = None
        url = domain = host = app_id = None
        site_unknown = False
        if fg is not None and self.urls is not None and fg.name and fg.name.lower() in self.browsers:
            try:
                site = self.urls.site(self.activity.last_hwnd, fg.title)
                url, domain, host = site.url, site.domain, site.host
                site_unknown, app_id = site.unknown, site.app_id
            except Exception:  # noqa: BLE001
                url = domain = host = app_id = None
                site_unknown = False
        if now - self._last_audio_poll >= self.audio_poll:
            self._last_audio_poll = now
            # each source on its own (a failing one keeps its last value): the rest of the report is sent
            if self.want_audio:
                self._audio_active = bool(_guarded("audio activity", self.audio.active, self._audio_active))
            if self.volume_control:
                self._volume = _guarded("volume", self.audio.volume, self._volume)
                self._muted = _guarded("mute state", self.audio.muted, self._muted)
        if now - self._last_media_poll >= self.media_poll:
            self._last_media_poll = now
            try:
                self._media = self.media.read(fg, self._audio_active)
            except Exception:  # noqa: BLE001
                self._media = None
        signals = _guarded("presence signals", lambda: self.presence.sample(
            input_idle, fullscreen=bool(fg is not None and fg.fullscreen), audio_active=self._audio_active), None)
        if not isinstance(signals, dict):
            signals = plain_signals(input_idle)
        extra = {k: v for k, v in (("host", host), ("site_unknown", site_unknown), ("app_id", app_id))
                 if k in _REPORT_FIELDS}
        report = AgentReport(
            session_id=self.session_id, ts=time.time(), idle_seconds=signals["idle_seconds"], locked=locked,
            foreground=fg, url=url, domain=domain, audio_active=self._audio_active,
            input_idle_seconds=signals["input_idle_seconds"], controller_idle_seconds=signals["controller_idle_seconds"],
            display_state=signals["display_state"], user_present=signals["user_present"],
            screensaver=signals["screensaver"], fullscreen_d3d=signals["fullscreen_d3d"],
            display_required=signals["display_required"], presentation_mode=signals["presentation_mode"],
            media=self._media, volume=self._volume, muted=self._muted, agent_version=__version__, **extra,
        )
        signature = (report.signature(), host)
        changed = signature != self._last_signature
        due = time.time() - self._last_report >= self.heartbeat
        if (changed or due) and self.client.connected.is_set():
            if self.client.send(self._report_message(report, host)):
                self._last_report = time.time()
                self._last_signature = signature
        self._update_tray_connection()

    def _report_message(self, report: AgentReport, host: Optional[str]) -> dict[str, Any]:
        """The report as sent: the model's fields plus what the service may not know of yet."""
        message = report.to_dict()
        if message.get("host") is None:
            message["host"] = host                    # full host name of the page; "domain" stays the grouped one
        message["agent_pid"] = os.getpid()            # lets the service recognise the agent without trusting a name
        foreground = message.get("foreground")
        if isinstance(foreground, dict) and not foreground.get("created"):
            foreground["created"] = self.activity.last_created   # tells a reused pid apart in close requests
        return message

    # -- tray ----------------------------------------------------------------
    def _update_tray_connection(self) -> None:
        if self.tray is None:
            return
        connected = self.client.connected.is_set()
        if connected == self._tray_connected:
            return
        self._tray_connected = connected
        if connected:
            self._show_connected()
        else:
            self.tray.set_time_requests([])   # nothing can be asked while the service is away
            self.tray.update("offline", detail="Reconnecting to service...")

    def _request_time(self, minutes: int) -> None:
        """The user picked an amount under "Ask for more time" (called on the tray's thread).

        The request goes to the service, which asks the parent; its answer comes
        back as an ordinary notification.  A second click within a few seconds
        (a double click, an impatient one) is not sent again.
        """
        minutes = int(minutes)
        offered = time_request_options((self._last_status or {}).get("time_requests"))
        if minutes not in offered:
            return   # the offer changed while the menu was open
        if not self._request_guard.allow(time.monotonic()):
            log.debug("time request ignored: one was sent a moment ago")
            return
        if self.client.send({"type": "time_request", "minutes": minutes}):
            log.info("asked for %d more minutes", minutes)
            return
        self._request_guard.undo()
        log.info("time request not sent: no connection to the service")
        if self.tray is not None:
            self.tray.notify("Home Assistant PC Link",
                             f"Could not ask for {request_label(minutes)}: not connected. Try again in a moment.", "info")

    def _show_connected(self) -> None:
        """The tray once the service is there: its last status, or plain "Connected" until one arrives.

        The service sends a status after every connect; this is what the icon
        shows until then, so it can never stay on "Connecting".
        """
        if self.tray is None:
            return
        if self._last_status is not None:
            self._apply_status(self._last_status)
        else:
            self.tray.update("ok", detail="Connected")

    def _apply_status(self, data: dict[str, Any]) -> None:
        self._last_status = data
        text = str(data.get("text", ""))
        self._status_text = text or self._status_text
        others = data.get("other_seats")
        if isinstance(others, int) and not isinstance(others, bool):
            self.presence.service_other_seats = max(0, others)
        remaining = data.get("remaining_minutes")
        if self.tray is None:
            return
        self.tray.set_time_requests(data.get("time_requests"))   # absent or empty: the menu entry is not shown
        if "paused" in text.lower() or "outside" in text.lower() or "bedtime" in text.lower() or (isinstance(remaining, (int, float)) and remaining <= 0):
            state = "blocked"
        elif data.get("focus"):
            state = "focus"
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
        """Called on the thread reading the connection: anything that can take a while
        (closing a program waits for it, a screenshot, a notification) is handed to the
        command thread, so a slow request never holds up the next one or a ping."""
        kind = msg.get("type")
        commands = getattr(self, "_commands", None)
        if kind in _COMMANDS and commands is not None:
            try:
                commands.put_nowait(msg)
            except queue.Full:
                log.warning("too many requests from the service waiting; %s dropped", kind)
                self._refuse(msg, "busy")
            return
        self._handle(msg)

    def _command_loop(self) -> None:
        commands = self._commands
        assert commands is not None
        while not self._stop.is_set():
            msg = commands.get()
            if msg is None:
                return
            try:
                self._handle(msg)
            except Exception:  # noqa: BLE001 - one bad request must not end the command thread
                log.exception("request %r from the service failed", msg.get("type"))
                self._refuse(msg, "error")

    def _refuse(self, msg: dict[str, Any], why: str) -> None:
        """The answer the service waits for, for a request that was not carried out."""
        kind = msg.get("type")
        if kind == "close":
            self.client.send({"type": "closed", "name": str(msg.get("name", "") or "")[:260],
                              "pid": as_int(msg.get("pid"), 0), "result": CLOSE_BAD_REQUEST if why == "error" else f"failed {why}",
                              "reason": msg.get("reason")})
        elif kind == "screenshot":
            self.client.send({"type": "screenshot", "id": str(msg.get("id", ""))[:64], "error": f"screenshot {why}"})

    def _handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "status":
            self._tray_connected = True
            self._apply_status(msg)
        elif kind == "notify":
            via = self._notify(msg.get("title", "Home Assistant"), msg.get("message", ""), msg.get("level", "info"))
            # Tells the service whether the user can have seen it, so that it can use its own
            # way (a session message) when the agent could not show anything.
            self.client.send({"type": "notified", "id": msg.get("id"), "shown": via in (VIA_BALLOON, VIA_BOX), "via": via})
        elif kind == "lock":
            log.info("service requested session lock")
            lock_workstation()
        elif kind == "run":
            self._run_command(msg.get("command", ""))
        elif kind == "close":
            self._close(msg)
        elif kind == "volume":
            self.audio.set_volume(max(0, min(100, as_int(msg.get("level", 50), 50))))
            self._last_audio_poll = 0.0
        elif kind == "mute":
            self.audio.set_mute(bool(msg.get("muted", True)))
            self._last_audio_poll = 0.0
        elif kind == "display":
            on = bool(msg.get("on", False))
            # waking injects a mouse movement; the activity reader keeps it out of the idle time
            wake = getattr(self.activity, "wake_display", None)
            done = wake() if (on and wake is not None) else set_display_power(on)
            log.info("service requested display %s: %s", "on" if on else "off", "done" if done else "failed")
        elif kind == "screenshot":
            self._start_screenshot(msg)
        elif kind == "hello_ok":
            self._tray_connected = True
            # a fresh connection: the next poll reports at once instead of after the heartbeat
            self._last_signature = None
            self._last_report = 0.0
            self._show_connected()
        elif kind == "exit":
            log.info("service asked agent to exit")
            self._stop.set()

    def _notify(self, title: str, message: str, level: str) -> str:
        """Show a notification; returns how it reached the user (``VIA_*``)."""
        title, message, level = str(title or ""), str(message or ""), str(level or "info")
        suppressed = toast_suppressed(self.presence.last_quns)
        want_balloon, want_box = notify_plan(level, bool(self.tray is not None and self.tray.can_notify), suppressed)
        balloon = False
        if want_balloon and self.tray is not None:
            try:
                balloon = bool(self.tray.notify(title, message, level))
            except Exception:  # noqa: BLE001
                balloon = False
        box = False
        if want_box or not balloon:
            # no tray, it cannot show a balloon right now (Explorer not running, pywin32 missing),
            # or Windows would keep a warning off the screen
            box = self._message_box(title, message, level)
        return notify_outcome(balloon, box, suppressed)

    def _message_box(self, title: str, message: str, level: str) -> bool:
        """Show a message box without ever blocking the caller; False when none could be opened.

        ``MessageBoxW`` is modal; this is called from the thread that receives
        the service's messages, which must stay free to handle a later lock /
        close / exit while a box is left open.  Each box gets its own
        short-lived daemon thread, and only a few may be open at once.
        """
        if sys.platform != "win32":
            log.debug("notify: %s - %s", title, message)
            return False
        with self._boxes_lock:
            if self._boxes_open >= MAX_MESSAGE_BOXES:
                log.info("notification dropped: %d message boxes are already open", self._boxes_open)
                return False
            self._boxes_open += 1
        try:
            threading.Thread(target=self._message_box_thread, args=(title, message, level),
                             name="message-box", daemon=True).start()
        except Exception:  # noqa: BLE001
            with self._boxes_lock:
                self._boxes_open -= 1
            log.debug("message box thread could not start", exc_info=True)
            return False
        return True

    def _message_box_thread(self, title: str, message: str, level: str) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            style = message_box_style(level)
            # A box nobody answers closes by itself, so that forgotten ones cannot use up the
            # few that may be open at once.
            timeout_ms = (300 if level == "critical" else 120) * 1000
            user32 = ctypes.WinDLL("user32")
            try:
                box = user32.MessageBoxTimeoutW
                box.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT, wintypes.WORD, wintypes.DWORD]
                box.restype = ctypes.c_int
                box(None, message, title, style, 0, timeout_ms)
            except AttributeError:
                user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
                user32.MessageBoxW.restype = ctypes.c_int
                user32.MessageBoxW(None, message, title, style)
        except Exception:  # noqa: BLE001
            log.debug("message box failed", exc_info=True)
        finally:
            with self._boxes_lock:
                self._boxes_open -= 1

    def _run_command(self, command: str) -> None:
        if not command:
            return
        log.info("running a command from the service")
        log.debug("command: %s", command[:120])
        try:
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(command, shell=True, creationflags=creation,  # noqa: S602 - operator-configured
                             env=child_environment(dict(os.environ)))
        except Exception as exc:  # noqa: BLE001
            log.error("command failed: %s", exc)

    def _close(self, msg: dict[str, Any]) -> None:
        """Close a browser tab or terminate a process, as decided by the service.

        Always answers: a request with a field that cannot be read gets
        ``CLOSE_BAD_REQUEST``, which the service reads as "not closed"."""
        name = str(msg.get("name", "") or "")
        pid = as_int(msg.get("pid", 0) or 0, -1)
        result = "noop"
        try:
            if pid < 0 or isinstance(msg.get("name"), (dict, list)):
                log.warning("close request with an unreadable pid or name; nothing closed")
                pid, result = max(pid, 0), CLOSE_BAD_REQUEST
            elif msg.get("tab"):
                # "title" is optional: when the service sends the title it decided on, the tab is
                # only closed while that page is still the one in front.
                result = "tab_closed" if close_tab(pid, str(msg.get("title") or "")) else "tab_failed"
                log.info("close tab in %s (pid %s): %s", name, pid, result)
            else:
                try:
                    created = float(msg.get("created") or 0.0)
                except (TypeError, ValueError, OverflowError):
                    created = 0.0
                # "terminated N" (N >= 1) on success; otherwise a form the service reads as "not
                # closed": "terminated 0", "refused ...", "failed ..." or "noop"
                result = close_app(pid, name, created)
                log.info("close %s (pid %s): %s", name or "?", pid or "-", result)
        except Exception:  # noqa: BLE001
            log.exception("close request failed")
            result = CLOSE_BAD_REQUEST
        self.client.send({"type": "closed", "name": name, "pid": pid, "result": result, "reason": msg.get("reason")})

    def _start_screenshot(self, msg: dict[str, Any]) -> None:
        """One screenshot at a time, on its own thread; a request meanwhile is answered "busy"."""
        if not self._shot_lock.acquire(blocking=False):
            log.info("screenshot %s refused: one is being taken already", str(msg.get("id", ""))[:64])
            self._refuse(msg, "busy: one is being taken already")
            return
        try:
            threading.Thread(target=self._screenshot_thread, args=(msg,), name="screenshot", daemon=True).start()
        except Exception:  # noqa: BLE001
            self._shot_lock.release()
            self._refuse(msg, "failed: no thread")

    def _screenshot_thread(self, msg: dict[str, Any]) -> None:
        try:
            self._screenshot(msg)
        finally:
            self._shot_lock.release()

    def _screenshot(self, msg: dict[str, Any]) -> None:
        req_id = str(msg.get("id", ""))
        try:
            # never larger than one message to the service may be: it is compressed harder and
            # taken smaller until it fits, or refused with a reason
            limit = image_budget(int(getattr(ipc, "MAX_LINE_LARGE", 8 * 1024 * 1024)))
            width = as_int(msg.get("max_width", 1280), 1280)
            width = MAX_CAPTURE_WIDTH if width <= 0 else min(width, MAX_CAPTURE_WIDTH)
            result = capture_screen(width, str(msg.get("format", "jpeg")),
                                    max(1, min(100, as_int(msg.get("quality", 60), 60))), max_bytes=limit)
        except Exception as exc:  # noqa: BLE001 - a bad parameter must still produce a reply
            log.warning("screenshot %s failed: %s", req_id, exc)
            result = {"error": f"screenshot failed: {exc}"}
        if "error" in result:
            log.warning("screenshot %s not taken: %s", req_id, result["error"])
            self.client.send({"type": "screenshot", "id": req_id, "error": result["error"]})
            return
        self.client.send({
            "type": "screenshot", "id": req_id, "format": result["format"], "width": result["width"],
            "height": result["height"], "data": base64.b64encode(result["data"]).decode("ascii"),
        })
        log.info("screenshot %s sent (%dx%d, %d bytes)", req_id, result["width"], result["height"], len(result["data"]))
        if msg.get("notify", True):
            self._notify("Home Assistant PC Link", "A parent took a screenshot of this screen.", "info")


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
    session = os.environ.get("PCLINK_SESSION", "x")
    logging.raiseExceptions = False  # pythonw has no stderr; a failing handler must stay silent
    try:
        setup_logging(_log_dir(), level, filename=f"agent-{session}.log", max_bytes=1_000_000, backups=2)
    except Exception:  # noqa: BLE001 - never let logging stop the agent
        try:
            logging.getLogger().addHandler(logging.NullHandler())
        except Exception:  # noqa: BLE001
            pass
    return Agent(settings).run()


if __name__ == "__main__":
    sys.exit(main())
