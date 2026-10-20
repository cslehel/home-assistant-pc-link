"""The orchestrator: main loop of the service.

Every tick it enumerates sessions, merges agent reports, works out who is
really present at each seat, lets the TimeKeeper account time and decide on
actions, executes them, attributes activity (gaming / browsing / sites),
applies per-app rules and focus mode, reminds about breaks, publishes state
to Home Assistant (and the local API), emits device-trigger events and keeps
the per-session agents alive.  All Windows specific backends are injected so
the loop can be exercised with fakes on any platform.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import queue
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from . import __version__
from .activity import ActivityClassifier
from .config import AppLauncher, AppRule, Config, ConfigError, CustomCommand, UserPolicy, check_rule_ids, parse_rule
from .entities import PC_TRIGGERS, TAMPER_REASONS, USER_TRIGGERS, Entity, EntityFactory, SlugRegistry, catalog_hash
from .fleet import FleetTracker
from .logonblock import valid_account_name
from .models import Activity, AgentReport, SessionInfo
from .pin import ParentPin
from .power import PowerControl, send_wol
from .presence import Presence, evaluate as evaluate_presence, user_state as presence_user_state
from .rules import RuleDecision, RuleEngine, closable, rule_loosens
from .store import Store
from .timekeeper import Action, TimeKeeper, TimeStatus
from .util import NO_LIMIT, domain_from_url, human_minutes, local_iana_zone, make_clock, slugify, time_zone
from .wellbeing import BreakReminder
from .winsessions import SESSION_EVENT_NAMES, WTS_SESSION_LOGOFF

log = logging.getLogger(__name__)

PRESENCE_ORDER = {"active": 0, "passive": 1, "idle": 2, "away": 3, "none": 4}
CONFIG_POLL_SECONDS = 10.0
NEVER = float("-inf")                 # "has not happened yet" for the monotonic timers below
MAX_QUEUED_COMMANDS = 1000            # commands and agent replies waiting for the main loop
AGENT_MESSAGE_BURST = 20              # non-report messages one agent may send ...
AGENT_MESSAGE_WINDOW = 10.0           # ... within this many seconds; the rest is dropped
MAX_NAMES_PER_DAY = 300               # distinct programs / websites recorded per user and day; more go to "other"
OTHER_NAME = "other"
MAX_STORED_NAME = 100
NOTIFY_ACK_SECONDS = 6.0              # an agent that does not acknowledge a warning this long: the service shows it
LOCK_CONFIRM_SECONDS = 10.0          # the agent gets this long to lock before a repeated request disconnects instead
UNKNOWN_SITE_CONFIRM_SECONDS = 5.0    # a web app's site unknown this long (same window) before focus mode acts on it
CLOSE_ESCALATE_SECONDS = 30.0        # a window that is still there this long after being closed gets the next, harder step
AGENT_EXIT_WINDOW = 600.0             # an agent that exits more than AGENT_EXIT_LIMIT times within this is being killed
AGENT_EXIT_LIMIT = 2
TAMPER_REPEAT_SECONDS = 600.0         # the same reason is announced again at most this often
CLOCK_JUMP_SECONDS = 60.0             # wall clock and monotonic clock disagree by more than this: the clock was set
CLOCK_CHECK_MAX_GAP = 120.0           # ... between two ticks at most this far apart (so sleep is not flagged)
COMPONENT_RETRY_SECONDS = 60.0        # a part that could not start (agent port, API) is tried again this often
DEGRADED_AFTER_FAILURES = 3           # ticks in a row with a failed step before the service reports "degraded"
MAX_FOCUS_MINUTES = 1440
SETTING_TIME_REQUEST = "time_request"             # the request a child made that nobody answered yet
SETTING_TIME_REQUEST_LAST = "time_request_last"   # when the child last asked (for the cooldown)
SETTING_TIME_REQUEST_COUNT = "time_request_count"  # ...:<day>: how often today
SETTING_WEEKLY_SUMMARY = "weekly_summary"         # the summary of the last complete week
SETTING_TAMPER = "tamper"            # last tampering sign per user: {"reason": ..., "ts": ...}
SETTING_UTC_OFFSET = "utc_offset"     # the PC's time zone as last seen: {"offset": seconds, "zone": name}
SETTING_PINNED_ZONE = "time_zone_pinned"   # the IANA zone the clock keeps to without time_limits.time_zone
SETTING_SAVED_HOURS = "logon_hours_saved"   # logon hours an account had before we blocked it (hex)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(value: Any, limit: int = 120) -> str:
    """Text from an agent as it may go into the log or the event list: one line, cut short."""
    return _CONTROL_RE.sub(" ", str(value))[:limit]


MAX_PUBLISHED_URL = 300               # what Home Assistant gets of the address in front ...
MAX_PUBLISHED_TITLE = 160             # ... and of the window title


def _public_url(url: Any) -> Optional[str]:
    """The address of the page in front as it is published to Home Assistant (and kept in
    its history): scheme, host and path - no query string or fragment (search terms,
    session ids, tokens), no user name or password - and cut short.  Rules and site
    categories still see the full address; this is only what leaves the PC."""
    if not url:
        return None
    text = str(url)
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(text)
        host = parts.hostname
        if parts.scheme and host:
            port = f":{parts.port}" if parts.port else ""
            text = f"{parts.scheme}://{host}{port}{parts.path}"
        else:
            text = re.split(r"[?#]", text, maxsplit=1)[0]
    except ValueError:   # a malformed port, an odd IPv6 literal: keep only what is before any query
        text = re.split(r"[?#]", text, maxsplit=1)[0]
    return _clean(text, MAX_PUBLISHED_URL) or None


def _iso(ts: Any, tz=None) -> Optional[str]:
    """A timestamp as ISO text, or None when it is no usable moment (a stored value may be anything)."""
    try:
        return datetime.fromtimestamp(float(ts), tz).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def system_time_zone() -> tuple[Optional[float], str]:
    """The PC's current UTC offset in seconds and the name of its time zone.

    Asked from Windows on every call: the C runtime (and with it ``time`` /
    ``datetime``) keeps the zone the process started with."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class SYSTEMTIME(ctypes.Structure):
                _fields_ = [(n, wintypes.WORD) for n in ("y", "m", "dow", "d", "h", "mi", "s", "ms")]

            class DYNAMIC_TIME_ZONE_INFORMATION(ctypes.Structure):  # noqa: N801 - Windows name
                _fields_ = [("Bias", wintypes.LONG), ("StandardName", wintypes.WCHAR * 32), ("StandardDate", SYSTEMTIME),
                            ("StandardBias", wintypes.LONG), ("DaylightName", wintypes.WCHAR * 32),
                            ("DaylightDate", SYSTEMTIME), ("DaylightBias", wintypes.LONG),
                            ("TimeZoneKeyName", wintypes.WCHAR * 128), ("DynamicDaylightTimeDisabled", wintypes.BOOLEAN)]

            info = DYNAMIC_TIME_ZONE_INFORMATION()
            kind = ctypes.windll.kernel32.GetDynamicTimeZoneInformation(ctypes.byref(info))  # type: ignore[attr-defined]
            if kind != 0xFFFFFFFF:
                bias = info.Bias + (info.DaylightBias if kind == 2 else info.StandardBias if kind == 1 else 0)
                return -bias * 60.0, info.TimeZoneKeyName or info.StandardName
        except Exception:  # noqa: BLE001 - fall back to what Python knows
            log.debug("could not ask Windows for the time zone", exc_info=True)
    offset = datetime.now().astimezone().utcoffset()
    return (offset.total_seconds() if offset is not None else None), "/".join(time.tzname)
SETTING_BLOCKED_ITEMS = "blocked_items"   # apps / websites blocked from Home Assistant (per user)
MAX_BLOCKED_ITEMS = 200
SETTING_CUSTOM_RULES = "custom_rules"     # rules made or changed from Home Assistant (per user): slug -> definition
MAX_CUSTOM_RULES = 50
# what a rule definition may contain (the keys of a rule in config.yaml)
RULE_FIELDS = ("apps", "sites", "categories", "daily_limit_minutes", "weekly_limit_minutes", "allowed_hours",
               "blocked", "warn_at_minutes", "action", "message", "icon")
PRESENCE_KINDS = ("active", "passive", "idle", "away", "locked")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class _AgentSlot:
    handle: Any = None
    token: Optional[str] = None
    next_launch: float = NEVER        # monotonic
    backoff: float = 15.0
    connected: bool = False
    user: str = ""
    launched_at: float = NEVER        # monotonic; with last_report what the watchdog measures silence from
    last_report: float = NEVER
    exits: list = field(default_factory=list)   # monotonic times the agent exited without being asked to
    expected_exit: bool = False       # we told it to exit (new settings): the next exit is not suspicious
    was_active: bool = False          # the seat was active at the last look


@dataclass
class _UserView:
    user: str
    activity: Activity = field(default_factory=Activity)
    last_activity_tick: Optional[float] = None
    # transition tracking for events
    logged_in: Optional[bool] = None
    presence: Optional[str] = None
    focus_active: Optional[bool] = None
    time_up_sent: Optional[tuple] = None     # (action, reason) already announced for this out-of-time spell
    # per seat, the window we are trying to close there: {"key", "step", "since", "failed", ...}
    closings: dict[int, dict[str, Any]] = field(default_factory=dict)
    tamper: Optional[dict[str, Any]] = None  # {"reason", "ts", "detail"}: last sign of tampering, until cleared
    tamper_announced: dict = field(default_factory=dict)   # reason -> monotonic time of the last event
    screenshot: Optional[dict[str, Any]] = None
    screenshot_data: Optional[bytes] = None     # last image, served by GET /api/users/<user>/screenshot
    last_refusal: Optional[dict[str, Any]] = None
    best_session: Optional[int] = None

    @property
    def closing(self) -> Optional[dict[str, Any]]:
        """The window being closed at the user's main seat (or at any, when not there)."""
        if not self.closings:
            return None
        return self.closings.get(self.best_session) or next(iter(self.closings.values()))


class Orchestrator:
    def __init__(self, cfg: Config, *, store: Optional[Store] = None, sessions=None, blocker=None, launcher=None,
                 power: Optional[PowerControl] = None, telemetry=None, hwsensors=None,
                 agent_server=None, clock: Optional[Callable[[], datetime]] = None, winupdate=None, devices=None,
                 api=None, updater=None):
        self.cfg = cfg
        self.store = store or Store(cfg.storage.path)
        self.timekeeper = TimeKeeper(cfg.time_limits, self.store, cfg.build_default_policy)
        self.classifier = ActivityClassifier(cfg.activity)
        self.rules = RuleEngine(self.store, cfg.time_limits.week_start_index)
        self.breaks = BreakReminder()
        self.pin = ParentPin(cfg.time_limits, on_event=self._on_pin_event, store=self.store)
        self.sessions = sessions
        self.blocker = blocker
        self.launcher = launcher
        self.power = power or PowerControl(cfg.power.shutdown_delay_seconds)
        self.telemetry = telemetry
        self.hwsensors = hwsensors
        self.agent_server = agent_server
        self.winupdate = winupdate
        self.updater = updater
        self.devices = devices
        self.api = api
        # an injected clock (tests) is kept as it is; ours follows time_limits.time_zone - or,
        # without one, the zone the service first ran in (see _pin_zone)
        self._own_clock = clock is None
        self._pinned_zone: Optional[str] = None
        self.clock = clock or make_clock(cfg.time_limits.time_zone or self._pin_zone())
        # the time keeper's "now" for grants is ours (a lambda: reload_config may swap self.clock)
        self.timekeeper.clock = lambda: self.clock()
        self.entities = EntityFactory(cfg)   # the entity catalog of the local API
        self.fleet = FleetTracker(cfg.fleet, cfg.device.id, own_name=cfg.device.name) \
            if cfg.fleet.enabled and cfg.api.enabled else None
        self.mdns = None  # optional mDNS advertiser for the local API (set by build_orchestrator)
        if self.fleet is not None:
            self.timekeeper.external_usage = self.fleet.external_usage
        self._wire_shared_extras(cfg)
        # runtime state -----------------------------------------------------
        self._reports: dict[int, AgentReport] = {}
        self._presence: dict[int, Presence] = {}
        self._agents: dict[int, _AgentSlot] = {}
        self._users: dict[str, _UserView] = {}
        self._block_applied: dict[str, bool] = {}
        self._block_asserted_at: float = NEVER
        self._pending_unblocks: set[str] = set()   # users no longer managed whose unblock failed: tried again
        self._unblock_tries: dict[str, int] = {}
        self._commands: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=MAX_QUEUED_COMMANDS)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        # Two clocks: the wall clock for what is shown and stored, the monotonic one for every
        # "has it been N seconds" question, so setting the PC's clock neither stalls nor rushes
        # anything.  Both are attributes so the tests can drive them.
        self._wall: Callable[[], float] = time.time
        self._mono: Callable[[], float] = time.monotonic
        # session id -> (window, monotonic time) since which its site has been unknown (see _unknown_site_pending)
        self._unknown_sites: dict[int, tuple[tuple, float, float]] = {}   # (window, since, last seen)
        self._zone_probe: Callable[[], tuple[Optional[float], str]] = system_time_zone
        self._last_pc_snapshot: Optional[dict[str, Any]] = None
        self._last_pc_snapshot_at = NEVER
        self._last_flush = time.monotonic()
        self._last_purge: Optional[date] = None
        self._last_sessions: list[SessionInfo] = []
        self._started_at = time.time()
        self._stats = {"ticks": 0, "actions": 0, "events": 0, "tick_failures": 0, "dropped_messages": 0}
        self._failing_steps: dict[str, str] = {}       # step -> error, for the steps that failed in the last tick
        self._step_logged: dict[str, float] = {}       # step -> monotonic time its traceback was last logged
        self._failed_ticks_in_a_row = 0
        self._last_good_tick: Optional[float] = None   # wall time of the last tick in which every step worked
        self._degraded = False
        self._down: dict[str, tuple[Any, float, str]] = {}   # part that could not start -> (object, next try, error)
        self._tick_now: Optional[datetime] = None      # set while a tick runs: TimeStatus is worked out once per user
        self._tick_status: dict[str, TimeStatus] = {}
        self._tick_payloads: dict[str, dict[str, Any]] = {}
        self._last_clocks: Optional[tuple[float, float]] = None   # (wall, monotonic) of the previous tick
        self._last_zone: Optional[tuple[float, str, float]] = None   # (UTC offset, zone name, wall time seen)
        self._zone_checked = NEVER
        self._pc_tamper: list[tuple[str, str]] = []     # (reason, detail) waiting for a managed user to pin it on
        self._pending_notes: dict[str, dict[str, Any]] = {}   # notifications an agent has not acknowledged yet
        self._summary_checked: dict[str, date] = {}    # user -> the week whose summary is known to exist
        self.config_problem: Optional[str] = None      # set by the service when it had to start from the last good config
        self._config_problem_announced = False
        self._agents_checked = NEVER
        self._recent_events: deque = deque(maxlen=500)   # what was announced lately, for clients that reconnect
        self._agent_status_sent: dict[int, str] = {}   # session -> tray text its agent last got
        self._agent_messages: dict[int, list[float]] = {}     # session -> arrival times of non-report messages
        self._agent_messages_lock = threading.Lock()          # ... written by the IPC thread too
        self._clock_jumped = False                            # the clock check flagged a jump in this tick
        self._names_seen: dict[tuple[str, str, str], set[str]] = {}   # (user, day, apps|sites) -> names recorded
        self._config_mtime: Optional[float] = self._stat_config()
        self._config_checked = NEVER
        self._restart_needed: list[str] = []
        self._pending_screenshots: dict[str, dict[str, Any]] = {}
        self._report_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._policy_cache: dict[str, tuple[Any, tuple[str, ...], UserPolicy]] = {}
        self._custom_rules_cache: dict[str, dict[str, AppRule]] = {}
        self._rules_policy_cache: dict[str, tuple[UserPolicy, dict[str, AppRule], UserPolicy]] = {}
        self._presence_cache: dict[str, tuple[float, date, dict[str, float]]] = {}
        self._lock_requested: dict[int, float] = {}   # session -> monotonic time its agent was asked to lock
        self._pin_notice: Optional[dict[str, Any]] = None
        self._last_api_push = NEVER
        self._started_event_sent = False
        for policy in cfg.time_limits.users:
            self._ensure_user(policy.key)
        self._attach_blocker_memory()

    def _pin_zone(self) -> Optional[str]:
        """The time zone the service keeps to when ``time_limits.time_zone`` is not set.

        Without it "now" would be the PC's local time, and a child who changes the time
        zone (a standard user may) and restarts moves the day boundary, bedtime and the
        allowed hours.  So the IANA zone the service first ran in is remembered in the
        database and used from then on, whatever Windows is set to later; a deliberate
        change (moving, travelling) is made by setting ``time_limits.time_zone``.  When
        the zone cannot be told at all, local time it is, as before."""
        stored = self.store.get_setting(Store.GLOBAL, SETTING_PINNED_ZONE)
        if isinstance(stored, str) and stored:
            try:
                time_zone(stored)
                self._pinned_zone = stored
                log.info("time zone: %s (pinned when the service first ran; set time_limits.time_zone "
                         "to change it)", stored)
                return stored
            except ValueError:
                log.warning("the pinned time zone %r is not known; pinning the current one", _clean(stored, 60))
        zone = local_iana_zone()
        if zone is None:
            log.warning("the PC's time zone cannot be told; using local time (set time_limits.time_zone "
                        "so that changing the PC's time zone cannot move bedtime or the day boundary)")
            return None
        try:
            self.store.set_setting(Store.GLOBAL, SETTING_PINNED_ZONE, zone)
        except Exception:  # noqa: BLE001 - used for this run all the same, pinned again on the next
            log.warning("could not store the pinned time zone", exc_info=True)
        log.info("time zone pinned to %s (the PC's zone now); set time_limits.time_zone to change it", zone)
        self._pinned_zone = zone
        return zone

    def _wire_shared_extras(self, cfg: Config) -> None:
        """With a shared budget, bonus minutes and bedtime extensions granted on the other PCs
        count here too (``FleetTracker.external_extras``)."""
        shared = self.fleet is not None and cfg.time_limits.shared_budget
        self.timekeeper.external_extras = self.fleet.external_extras if shared else None

    @property
    def slugs(self):
        """The one allocator of user ids, shared with the entity factory (see ``SlugRegistry``)."""
        return self.entities.slugs

    def _attach_blocker_memory(self) -> None:
        """Let the login blocker keep, across restarts, the logon hours an account had before a block."""
        blocker = self.blocker
        if blocker is None or not hasattr(blocker, "on_remember"):
            return
        remembered = {}
        for user in self.timekeeper.managed_users():
            value = self.store.get_setting(user, SETTING_SAVED_HOURS)
            try:
                if value:
                    remembered[user] = bytes.fromhex(str(value))
            except ValueError:
                pass
        blocker.remembered = remembered

        def remember(user: str, hours: Optional[bytes]) -> None:
            if hours is None:
                self.store.delete_setting(user, SETTING_SAVED_HOURS)
            else:
                self.store.set_setting(user, SETTING_SAVED_HOURS, bytes(hours).hex())

        blocker.on_remember = remember

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def run(self) -> None:
        log.info("pclink %s starting (device %s)", __version__, self.cfg.device.id)
        self._start_components()
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("tick failed")
                elapsed = time.monotonic() - started
                if elapsed > 2.0:
                    log.debug("slow tick: %.2fs", elapsed)
                self._wake.wait(max(0.5, self.cfg.time_limits.tick_seconds - elapsed))
                self._wake.clear()
        finally:
            self._shutdown_components()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _start_components(self) -> None:
        # The agent port first, before anything slow (the purge, hardware sensors loading
        # LibreHardwareMonitor, Windows Update, the API, mDNS): until it is bound another
        # local process could take the port and wait for our agents to talk to it.
        self._start_part("agent_server", self.agent_server, first=True)
        try:
            self.store.purge(self.cfg.storage.retention_days)
        except Exception:  # noqa: BLE001
            log.exception("store purge failed")
        for name, comp in (("hardware sensors", self.hwsensors), ("windows update", self.winupdate),
                           ("self update", self.updater)):
            if comp is not None:
                try:
                    comp.start()
                except Exception:  # noqa: BLE001
                    log.exception("%s failed to start", name)
        self._start_part("api", self.api, first=True)
        if self.api is not None and self.mdns is not None:
            try:
                self.mdns.start()
            except Exception:  # noqa: BLE001
                log.warning("mDNS advertising failed to start", exc_info=True)

    _PART_NAMES = {"agent_server": "agent IPC server", "api": "local API"}

    def _start_part(self, name: str, part: Any, first: bool = False) -> bool:
        """Start the agent server or the API.  One that cannot start (its port is taken,
        say) is set aside - the rest of the service runs without it - and tried again every
        minute; until it runs the service reports itself as degraded."""
        if part is None:
            return False
        try:
            part.start()
        except Exception as exc:  # noqa: BLE001
            if first or name not in self._down:
                log.error("%s failed to start (%s); running without it and trying again every %d s",
                          self._PART_NAMES[name], exc, int(COMPONENT_RETRY_SECONDS), exc_info=first)
            self._down[name] = (part, self._mono() + COMPONENT_RETRY_SECONDS, _clean(exc, 200))
            setattr(self, name, None)
            return False
        if name in self._down:
            log.warning("%s started after all", self._PART_NAMES[name])
            del self._down[name]
        setattr(self, name, part)
        if name == "api" and not first and self.mdns is not None:
            try:
                self.mdns.start()
            except Exception:  # noqa: BLE001
                log.warning("mDNS advertising failed to start", exc_info=True)
        return True

    def _retry_parts(self, mono: float) -> None:
        for name, (part, due, _error) in list(self._down.items()):
            if mono >= due:
                self._start_part(name, part)

    def _shutdown_components(self) -> None:
        log.info("shutting down")
        try:
            self.store.flush()
        except Exception:  # noqa: BLE001
            log.exception("final flush failed")
        if self.agent_server is not None:
            try:
                self.agent_server.broadcast({"type": "exit"})
                time.sleep(0.2)
                self.agent_server.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping the agent server failed")
        if self.launcher is not None:
            for slot in self._agents.values():
                if slot.handle is not None:
                    self.launcher.release(slot.handle)
        if self.cfg.time_limits.unblock_on_stop and self.blocker is not None:
            for user in self.timekeeper.managed_users():
                try:
                    self.blocker.unblock(user)
                except Exception:  # noqa: BLE001
                    log.exception("unblock on stop failed for %s", user)
        for comp in (self.hwsensors, self.winupdate, self.updater, self.mdns, self.api, self.telemetry, self.devices):
            if comp is not None and not hasattr(comp, "stop"):
                continue
            if comp is not None:
                try:
                    comp.stop()
                except Exception:  # noqa: BLE001
                    log.debug("component stop failed", exc_info=True)
        self.store.close()

    # ------------------------------------------------------------------
    # external events (other threads)
    # ------------------------------------------------------------------
    def on_session_change(self, event_type: int, session_id: int) -> None:
        name = SESSION_EVENT_NAMES.get(event_type, str(event_type))
        log.info("session event: %s (session %s)", name, session_id)
        if event_type == WTS_SESSION_LOGOFF:
            self._reports.pop(session_id, None)
        self._wake.set()

    def on_agent_report(self, session_id: int, report: AgentReport) -> None:
        """A report from the agent of ``session_id`` (IPC thread).

        The agent runs as the monitored user: a report that did not pass
        ``AgentReport.from_dict`` is dropped and noted as a sign of tampering,
        and a report is dated by when it arrived, never by what it says."""
        if report.invalid:
            if self._agent_may_send(session_id):
                self._queue(("_bad_report", (session_id, report.invalid)))
            return
        report.received = self._mono()
        self._reports[session_id] = report
        slot = self._agents.get(session_id)
        if slot:
            slot.last_report = report.received

    def on_agent_message(self, session_id: int, message: dict[str, Any]) -> None:
        """Non-report messages from an agent (screenshot results, close results); handled on
        the main loop.  Only the kinds we ask for are taken, and only so many per agent:
        the rest is dropped here, before it costs the main loop anything."""
        kind = message.get("type") if isinstance(message, dict) else None
        if kind not in ("screenshot", "closed", "notified", "time_request", "oversized"):
            return
        if kind == "notified" and str(message.get("id")) not in self._pending_notes:
            return
        if kind == "screenshot" and not any(p["session"] == session_id for p in list(self._pending_screenshots.values())):
            log.warning("unsolicited screenshot from session %s ignored", session_id)
            return
        if self._agent_may_send(session_id) and self._queue(("_agent_message", (session_id, message))):
            self._wake.set()

    def _agent_may_send(self, session_id: int) -> bool:
        """At most AGENT_MESSAGE_BURST messages per agent within AGENT_MESSAGE_WINDOW seconds.

        Runs on the IPC thread while the main loop may be pruning the same table: both hold
        ``_agent_messages_lock`` (not the tick's lock - an agent must not wait for a tick)."""
        now = self._mono()
        with self._agent_messages_lock:
            recent = [t for t in self._agent_messages.get(session_id, []) if now - t < AGENT_MESSAGE_WINDOW]
            allowed = len(recent) < AGENT_MESSAGE_BURST
            if allowed:
                recent.append(now)
            else:
                self._stats["dropped_messages"] += 1
            self._agent_messages[session_id] = recent
        return allowed

    def _queue(self, item: tuple[str, Any]) -> bool:
        """Hand something to the main loop; dropped (and counted) when the loop is that far behind."""
        try:
            self._commands.put_nowait(item)
            return True
        except queue.Full:
            self._stats["dropped_messages"] += 1
            return False

    def validate_agent_token(self, session_id: int, token: str) -> bool:
        slot = self._agents.get(session_id)
        return bool(slot and slot.token and secrets.compare_digest(slot.token, token))

    def on_agent_connected(self, session_id: int, user: str, pid: int) -> None:
        slot = self._agents.get(session_id)
        if slot:
            slot.connected = True
            slot.last_report = self._mono()
            slot.backoff = self.cfg.agent.relaunch_backoff_seconds
        # a fresh agent knows nothing: the tray status is sent again on the next tick
        self._agent_status_sent.pop(session_id, None)
        self._wake.set()

    def on_agent_disconnected(self, session_id: int) -> None:
        slot = self._agents.get(session_id)
        if slot:
            slot.connected = False
        self._reports.pop(session_id, None)
        self._agent_status_sent.pop(session_id, None)

    def _perform_resync(self) -> None:
        """Send Home Assistant everything again: a fresh PC payload on the next API push."""
        self._last_pc_snapshot = None
        self._last_api_push = NEVER

    def _on_pin_event(self, kind: str) -> None:
        self._emit_pc_event(kind)

    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------
    def tick(self) -> None:
        with self._lock:
            self._tick()

    def _step(self, name: str, fn: Callable[..., Any], *args: Any, default: Any = None) -> Any:
        """Run one step of the tick.  A step that raises is noted and logged, and the tick goes
        on: counting time and enforcing limits must not depend on, say, publishing working."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            self._failing_steps[name] = f"{type(exc).__name__}: {_clean(exc, 200)}"
            mono = self._mono()
            if mono - self._step_logged.get(name, NEVER) >= 60.0:   # not a traceback every five seconds
                self._step_logged[name] = mono
                log.exception("tick step %s failed", name)
            return default

    def _tick(self) -> None:
        now = self.clock()
        now_ts = self._wall()
        mono = self._mono()
        self._stats["ticks"] += 1
        self._failing_steps = {}
        try:
            self._run_steps(now, now_ts, mono)
        finally:
            self._tick_now = None
            self._tick_status = {}
            self._tick_payloads = {}
            # whatever happened above, what was counted goes to disk
            if time.monotonic() - self._last_flush >= self.cfg.storage.flush_seconds:
                self._last_flush = time.monotonic()
                self._step("flush", self.store.flush)
            if self._last_purge != now.date():
                self._last_purge = now.date()
                # A date reached by setting the clock (a jump flagged by the clock check in this
                # tick) is no reason to delete anything: set a year ahead, the purge would take
                # every real day with it.  That date counts as done; the next one reached by
                # time passing purges as usual.
                if not self._clock_jumped:
                    self._step("purge", self.store.purge, self.cfg.storage.retention_days, now.date())
                else:
                    log.warning("the clock was set: no purge of old data for %s", now.date().isoformat())
            self._clock_jumped = False
            self._finish_tick(now_ts)

    def _run_steps(self, now: datetime, now_ts: float, mono: float) -> None:
        step = self._step
        step("start parts", self._retry_parts, mono)
        step("clock check", self._watch_clocks, now, now_ts, mono)
        step("config reload", self._check_config_reload, now_ts, mono)
        step("commands", self._drain_commands)
        sessions = step("sessions", self._collect_sessions, mono)
        if sessions is None:   # could not even merge: go on with what was known a moment ago
            sessions = list(self._last_sessions)
        self._last_sessions = sessions
        step("agents", self._manage_agents, sessions, mono)
        by_user: dict[str, list[SessionInfo]] = {}
        for s in sessions:
            by_user.setdefault(s.user, []).append(s)
        for user in by_user:
            self._ensure_user(user)
        step("tamper", self._apply_pc_tamper, by_user)
        actions = step("time limits", self.timekeeper.tick, now, by_user, default=[])
        for action in actions:
            self._stats["actions"] += 1
            step(f"action {action.kind}", self._execute, action, by_user)
        self._tick_now = now   # from here on a user's TimeStatus and payload are worked out once
        step("activity", self._track_activity, now, by_user)
        step("rules", self._enforce_rules, now, by_user)
        step("breaks", self._wellbeing, now, by_user)
        step("transitions", self._transitions, now, by_user)
        step("login blocks", self._reassert_blocks, now, mono)
        step("tray status", self._push_agent_status, now, by_user)
        step("notifications", self._check_notes, mono)
        if not self._started_event_sent and self.api is not None:
            self._started_event_sent = True
            step("started event", self._emit_pc_event, "started")
        if self.config_problem and not self._config_problem_announced and self.api is not None:
            self._config_problem_announced = True
            step("config event", lambda: self._emit_pc_event("config_invalid", reason=_clean(self.config_problem, 300)))
        step("api push", self._api_push, now, by_user, mono)

    def _finish_tick(self, now_ts: float) -> None:
        """Keep the health figures: failures in a row make the service "degraded", which Home
        Assistant shows (and can alert on) instead of it going unnoticed in a log file."""
        if self._failing_steps:
            self._stats["tick_failures"] += 1
            self._failed_ticks_in_a_row += 1
        else:
            self._failed_ticks_in_a_row = 0
            self._last_good_tick = now_ts
        degraded = self._failed_ticks_in_a_row >= DEGRADED_AFTER_FAILURES or bool(self._down) or bool(self.config_problem)
        if degraded != self._degraded:
            self._degraded = degraded
            self._last_api_push = NEVER
            if degraded:
                reason = ", ".join(sorted(self._failing_steps) + sorted(self._PART_NAMES[n] for n in self._down))
                log.error("service degraded: %s", reason)
                self.store.log_event(None, "degraded", reason[:300])
                self._step("degraded event", lambda: self._emit_pc_event("degraded", reason=reason[:300]))
            else:
                log.warning("service is working normally again")
                self.store.log_event(None, "recovered", "")

    def health(self) -> dict[str, Any]:
        return {
            "degraded": self._degraded or bool(self._down) or bool(self.config_problem),
            # config.yaml could not be read at start: the service runs on the last one that worked
            "config_problem": _clean(self.config_problem, 300) if self.config_problem else None,
            "tick_failures": self._stats["tick_failures"],
            "failed_ticks_in_a_row": self._failed_ticks_in_a_row,
            "failing_steps": dict(self._failing_steps),
            "not_started": {self._PART_NAMES[name]: error for name, (_part, _due, error) in self._down.items()},
            "last_good_tick": _iso(self._last_good_tick) if self._last_good_tick else None,
            "dropped_messages": self._stats["dropped_messages"],
        }

    def _status(self, user: str, now: datetime) -> TimeStatus:
        """The user's TimeStatus; inside a tick it is worked out once and shared by every step."""
        if self._tick_now is not now:
            return self.timekeeper.status(user, now)
        st = self._tick_status.get(user)
        if st is None:
            st = self._tick_status[user] = self.timekeeper.status(user, now)
        return st

    # -- tampering ------------------------------------------------------------
    def _flag_tamper(self, user: str, reason: str, detail: str = "") -> None:
        """Note a sign that ``user`` worked around PC Link.  It stays on the user (and in the
        database) until a parent clears it; the event is not repeated for every forged report."""
        if not self.timekeeper.is_managed(user):
            return
        view = self._ensure_user(user)
        mono = self._mono()
        changed = view.tamper is None or view.tamper.get("reason") != reason
        announce = mono - view.tamper_announced.get(reason, NEVER) >= TAMPER_REPEAT_SECONDS
        if not (changed or announce):
            return
        detail = _clean(detail, 200)
        view.tamper = {"reason": reason, "ts": self._wall(), "detail": detail}
        try:
            self.store.set_setting(user, SETTING_TAMPER, view.tamper)
        except Exception:  # noqa: BLE001 - it is still shown and announced
            log.debug("could not store the tamper flag of %s", user, exc_info=True)
        if announce:
            view.tamper_announced[reason] = mono
            log.warning("possible tampering by %s: %s %s", user, reason, detail)
            self.store.log_event(user, "tamper", f"{reason}: {detail}" if detail else reason)
            self._emit_user_event(user, "tamper", reason=reason, detail=detail or None)

    def _clear_tamper(self, user: Optional[str], pin: Any = None) -> dict[str, Any]:
        users = [user] if user else [u for u, v in self._users.items() if v.tamper]
        if any(self._users.get(u) is not None and self._users[u].tamper for u in users) \
                and (ref := self._gate("disable", "clear_tamper", user, pin)):
            return ref
        for name in users:
            view = self._users.get(name)
            if view is None:
                continue
            view.tamper = None
            view.tamper_announced = {}
            self.store.delete_setting(name, SETTING_TAMPER)
            self.store.log_event(name, "tamper_cleared", "")
        return {"ok": True, "cleared": users}

    def _watch_clocks(self, now: datetime, now_ts: float, mono: float) -> None:
        """Notice the PC's clock being set and its time zone being changed.

        The clock: wall time and monotonic time must advance together between two ticks
        that were close to each other (a long hole is sleep, not somebody setting the clock).
        The zone: asked from Windows and remembered across restarts, because a changed zone
        only moves the day boundary of a service that has no ``time_limits.time_zone`` after
        it restarts.  A change of offset at a daylight-saving switch is no tampering."""
        last, self._last_clocks = self._last_clocks, (now_ts, mono)
        if last is not None:
            mono_gap, wall_gap = mono - last[1], now_ts - last[0]
            if 0 <= mono_gap < CLOCK_CHECK_MAX_GAP and abs(wall_gap - mono_gap) > CLOCK_JUMP_SECONDS:
                self._clock_jumped = True   # (no purge on this tick, see _tick)
                self._pc_tamper.append(("clock_changed", f"clock moved by {wall_gap - mono_gap:+.0f} s"))
        if mono - self._zone_checked < 30.0:
            return
        self._zone_checked = mono
        offset, zone = self._zone_probe()
        if offset is None:
            return
        previous = self._last_zone
        if previous is None:
            stored = self.store.get_setting(Store.GLOBAL, SETTING_UTC_OFFSET)
            if isinstance(stored, dict) and isinstance(stored.get("offset"), (int, float)):
                previous = (float(stored["offset"]), str(stored.get("zone") or ""), float(stored.get("ts") or now_ts))
        self._last_zone = (offset, zone, now_ts)
        if previous is not None and previous[0] == offset and previous[1] == zone:
            return
        self.store.set_setting(Store.GLOBAL, SETTING_UTC_OFFSET, {"offset": offset, "zone": zone, "ts": now_ts})
        if previous is None or previous[0] == offset:
            return
        if previous[1] == zone or self._configured_zone_shift(previous[2], now_ts) == offset - previous[0]:
            return   # the same zone switched to or from daylight-saving time
        self._pc_tamper.append(("time_zone_changed", f"UTC{previous[0] / 3600:+g} ({previous[1]}) -> UTC{offset / 3600:+g} ({zone})"))

    def _configured_zone_shift(self, then_ts: float, now_ts: float) -> Optional[float]:
        """By how many seconds the UTC offset of ``time_limits.time_zone`` (or the pinned zone)
        moved between two moments."""
        name = self.cfg.time_limits.time_zone or self._pinned_zone
        if not name:
            return None
        try:
            from .util import time_zone

            tz = time_zone(name)
            return (datetime.fromtimestamp(now_ts, tz).utcoffset() - datetime.fromtimestamp(then_ts, tz).utcoffset()).total_seconds()
        except Exception:  # noqa: BLE001
            return None

    def _apply_pc_tamper(self, by_user: dict[str, list[SessionInfo]]) -> None:
        """Signs that concern the whole PC go to every managed user who is logged in.  A changed
        time zone is kept until somebody is (it is usually changed right before a restart); a
        clock that was set while nobody was logged in is the time service, not a child."""
        if not self._pc_tamper:
            return
        users = [u for u in by_user if self.timekeeper.is_managed(u)]
        if not users:
            self._pc_tamper = [p for p in self._pc_tamper if p[0] == "time_zone_changed"][-2:]
            return
        pending, self._pc_tamper = self._pc_tamper, []
        for reason, detail in pending:
            for user in users:
                self._flag_tamper(user, reason, detail)

    # -- sessions -------------------------------------------------------------
    def _collect_sessions(self, mono: float) -> list[SessionInfo]:
        if self.sessions is None:
            return []
        try:
            sessions = self.sessions.list_sessions()
        except Exception as exc:  # noqa: BLE001
            # Windows could not say.  "Nobody is logged in" would be the wrong guess (agents
            # dropped, logged-off events, time not counted): keep the sessions known so far.
            self._failing_steps["sessions"] = f"{type(exc).__name__}: {_clean(exc, 200)}"
            if mono - self._step_logged.get("sessions", NEVER) >= 60.0:
                self._step_logged["sessions"] = mono
                log.error("session enumeration failed (%s); keeping the last known sessions", exc)
            sessions = [replace(s, report=None) for s in self._last_sessions]
        stale_after = max(self.cfg.agent.heartbeat_seconds * 3, self.cfg.agent.poll_seconds * 5, 20.0)
        present = set()
        for s in sessions:
            present.add(s.session_id)
            report = self._reports.get(s.session_id)
            if report is not None and mono - report.received <= stale_after:
                s.report = report
                if s.locked is None:
                    s.locked = report.locked
                s.idle_seconds = report.idle_seconds  # GetLastInputInfo is per session and precise
            presence = evaluate_presence(s, self.cfg.activity)
            s.presence = presence.state
            s.presence_reason = presence.reason
            self._presence[s.session_id] = presence
        for sid in list(self._reports):
            if sid not in present:
                self._reports.pop(sid, None)
        for sid in list(self._presence):
            if sid not in present:
                self._presence.pop(sid, None)
        for per_session in (self._agent_status_sent, self._lock_requested):
            for sid in [sid for sid in per_session if sid not in present]:
                per_session.pop(sid, None)
        with self._agent_messages_lock:   # the IPC thread adds to this one (_agent_may_send)
            for sid in [sid for sid in self._agent_messages if sid not in present]:
                self._agent_messages.pop(sid, None)
        return sessions

    def _manage_agents(self, sessions: list[SessionInfo], mono: float) -> None:
        """Keep one agent running per seat: start it, start it again when it exits, and
        replace one that is still there but no longer says anything.

        A slot lives as long as its session: an agent keeps running through a disconnect
        (fast user switching) and is only started for an active seat.  When the session
        ends the agent is ended with it rather than left behind."""
        if self.launcher is None or self.agent_server is None or not self.cfg.agent.enabled:
            return
        present = {s.session_id: s for s in sessions if s.logged_in}
        active = {sid: s for sid, s in present.items() if s.state in ("active", "connected")}
        watchdog = float(getattr(self.cfg.agent, "watchdog_seconds", 90.0) or 0.0)
        # after a hole between ticks (the PC slept) or while a seat was disconnected, silence means nothing
        slept, self._agents_checked = mono - self._agents_checked > 30.0, mono
        for sid, slot in self._agents.items():
            if slept or (sid in active) != slot.was_active:
                slot.last_report = max(slot.last_report, mono)
            slot.was_active = sid in active
        for sid, slot in list(self._agents.items()):
            session = present.get(sid)
            if session is None or session.user != slot.user:
                # the session ended (or its number went to somebody else): take the agent along
                if slot.handle is not None:
                    self._end_agent(slot)
                del self._agents[sid]
                continue
            if slot.handle is None:
                continue
            if not self.launcher.alive(slot.handle):
                self.launcher.release(slot.handle)
                self._agent_gone(sid, slot, mono)
                if sid in active and not slot.expected_exit:
                    log.info("agent for session %s exited; will relaunch", sid)
                    slot.exits = [t for t in slot.exits if mono - t < AGENT_EXIT_WINDOW] + [mono]
                    if len(slot.exits) > AGENT_EXIT_LIMIT:
                        self._flag_tamper(slot.user, "agent_killed",
                                          f"the agent in session {sid} exited {len(slot.exits)} times in "
                                          f"{int(AGENT_EXIT_WINDOW // 60)} minutes")
                slot.expected_exit = False
            elif sid in active and watchdog > 0 and mono - max(slot.launched_at, slot.last_report) > watchdog:
                # alive but silent (hung, or suspended by its user): a new one takes its place
                log.warning("agent for session %s (%s) has been silent for %d s; restarting it",
                            sid, slot.user, int(mono - max(slot.launched_at, slot.last_report)))
                self._end_agent(slot)
                self._agent_gone(sid, slot, mono)
                self._flag_tamper(slot.user, "agent_silent", f"the agent in session {sid} stopped reporting")
        for sid, session in active.items():
            slot = self._agents.get(sid)
            if slot is None:
                slot = _AgentSlot(backoff=self.cfg.agent.relaunch_backoff_seconds, user=session.user)
                self._agents[sid] = slot
            if slot.handle is not None or mono < slot.next_launch:
                continue
            slot.token = secrets.token_urlsafe(24)
            slot.handle = self.launcher.launch(sid, slot.token, session.user)
            slot.launched_at = slot.last_report = mono
            slot.next_launch = mono + slot.backoff
            if slot.handle is None:
                slot.backoff = min(slot.backoff * 2, self.cfg.agent.relaunch_backoff_max_seconds)

    def _end_agent(self, slot: _AgentSlot) -> None:
        """Terminate an agent we started (and let go of its handle)."""
        handle, slot.handle = slot.handle, None
        try:
            getattr(self.launcher, "terminate", self.launcher.release)(handle)
        except Exception:  # noqa: BLE001
            log.debug("could not end the agent of session %s", getattr(handle, "session_id", "?"), exc_info=True)

    def _agent_gone(self, sid: int, slot: _AgentSlot, mono: float) -> None:
        slot.handle = None
        slot.connected = False
        slot.token = None   # the old token dies with the agent: nothing can connect in its name
        slot.next_launch = mono + slot.backoff
        slot.backoff = min(slot.backoff * 2, self.cfg.agent.relaunch_backoff_max_seconds)

    def _ensure_user(self, user: str) -> _UserView:
        view = self._users.get(user)
        if view is None:
            view = _UserView(user=user)
            self._users[user] = view
            policy = self.cfg.user_policy(user)
            self.slugs.assign(user, policy.slug if policy is not None else None)
            stored = self.store.get_setting(user, SETTING_TAMPER)
            if isinstance(stored, dict) and stored.get("reason") in TAMPER_REASONS:
                view.tamper = {"reason": stored["reason"], "ts": stored.get("ts"), "detail": str(stored.get("detail") or "")}
        return view

    def _agent_sessions(self, user: str, only_active: bool = True) -> list[int]:
        return [s.session_id for s in self._last_sessions
                if s.user == user and (s.state == "active" if only_active else s.logged_in)]

    # -- actions ----------------------------------------------------------------
    def _execute(self, action: Action, by_user: dict[str, list[SessionInfo]]) -> None:
        sessions = by_user.get(action.user, [])
        targets = action.sessions or [s.session_id for s in sessions if s.logged_in]
        log.info("action %s for %s (sessions %s) %s", action.kind, action.user, targets, action.title)
        if action.kind == "warn":
            self.notify_sessions(targets, action.title, action.message, action.level)
            self._emit_user_event(action.user, "grace_started" if action.level == "critical" else "warning",
                                  minutes_left=action.minutes_left, reason=action.reason)
            if action.reason == "bedtime" and action.level == "critical":
                self._emit_user_event(action.user, "bedtime")
        elif action.kind == "logoff":
            self.notify_sessions(targets, action.title, action.message, "critical")
            for sid in targets:
                self._logoff_session(sid)
            self._time_up(action, "logoff")
        elif action.kind == "lock":
            for sid in targets:
                self._lock_session(sid)
            self._time_up(action, "lock")
        elif action.kind == "shutdown":
            # anybody else who is logged in - also in a disconnected session (switch user), where
            # unsaved work would be lost just the same - turns the shutdown into a logoff
            others = [s for u, ss in by_user.items() if u != action.user for s in ss if s.logged_in]
            if others:
                log.warning("shutdown requested for %s but other users are logged in (%s); logging off instead",
                            action.user, sorted({s.user for s in others}))
                for sid in targets:
                    self._logoff_session(sid)
            else:
                self.power.shutdown(message="Computer time is over.", delay=5)
            self._time_up(action, "shutdown")
        elif action.kind == "block":
            if self._apply_block(action.user, True):
                self._emit_user_event(action.user, "login_blocked", reason=action.reason)
        elif action.kind == "unblock":
            if self._apply_block(action.user, False):
                self._emit_user_event(action.user, "login_unblocked")
            elif not self.timekeeper.is_managed(action.user):
                self._pending_unblocks.add(action.user)   # nothing else would try again for this user

    def _time_up(self, action: Action, kind: str) -> None:
        """Announce "time is up" once per spell.  The enforcement itself is repeated for as long
        as the user stays logged in (a locked session is), the event and its trigger are not;
        ``_transitions`` re-arms it when the user has time again or is gone."""
        view = self._ensure_user(action.user)
        key = (kind, action.reason)
        if view.time_up_sent == key:
            return
        view.time_up_sent = key
        self.store.log_event(action.user, "time_up", f"{kind} ({action.reason})")   # counted by the weekly summary
        self._emit_user_event(action.user, "time_up", action=kind, reason=action.reason)

    def _logoff_session(self, session_id: int) -> None:
        if self.sessions is None:
            return
        try:
            self.sessions.logoff(session_id, False)
            self.store.log_event(None, "logoff_session", str(session_id))
        except Exception as exc:  # noqa: BLE001
            log.error("logoff of session %s failed: %s", session_id, exc)
        self._reports.pop(session_id, None)

    def _lock_session(self, session_id: int) -> None:
        # The agent locks the workstation politely; if the session is still unlocked when we
        # are asked again a while after, it did not (hung or tampered with): disconnect instead.
        # A second request right behind the first (a button pressed twice) is the same request:
        # the session list is from the last tick and cannot show the lock yet.
        mono = self._mono()
        since = mono - self._lock_requested.get(session_id, NEVER)
        if 0 <= since < LOCK_CONFIRM_SECONDS:
            return
        still_unlocked = any(s.session_id == session_id and s.state == "active" and not s.locked for s in self._last_sessions)
        retry = still_unlocked and since < 300
        if not retry and self.agent_server is not None and self.agent_server.send(session_id, {"type": "lock"}):
            self._lock_requested[session_id] = mono
            return
        self._lock_requested.pop(session_id, None)
        if self.sessions is not None:
            try:
                self.sessions.disconnect(session_id)
            except Exception as exc:  # noqa: BLE001
                log.error("lock (disconnect) of session %s failed: %s", session_id, exc)

    def notify_sessions(self, session_ids: list[int], title: str, message: str, level: str = "info") -> None:
        """Show a message in these sessions: through the agent (a balloon), or - when there is
        none, or it answers that Windows kept the balloon off the screen, or it does not answer
        a warning at all - in a message box from the service (``WTSSendMessage``)."""
        for sid in session_ids:
            delivered = False
            if self.agent_server is not None:
                note_id = secrets.token_hex(4)
                delivered = self.agent_server.send(sid, {"type": "notify", "id": note_id, "title": title,
                                                         "message": message, "level": level})
                if delivered:
                    if len(self._pending_notes) > 200:   # nobody answers: the oldest go
                        for old in list(self._pending_notes)[:100]:
                            del self._pending_notes[old]
                    self._pending_notes[note_id] = {"session": sid, "title": title, "message": message, "level": level,
                                                    "at": self._mono()}
            if not delivered and self.sessions is not None:
                self.sessions.send_message(sid, title, message, timeout=60, critical=level == "critical")

    def _notified(self, session_id: int, message: dict[str, Any]) -> None:
        """The agent's answer to a notification: how it reached the user."""
        note = self._pending_notes.get(str(message.get("id")))
        if note is None or note["session"] != session_id:
            return
        del self._pending_notes[str(message.get("id"))]
        via = message.get("via")
        if via == "none" or (via == "quiet" and note["level"] != "info"):
            self._notify_fallback(note)

    def _notify_fallback(self, note: dict[str, Any]) -> None:
        if self.sessions is not None:
            self.sessions.send_message(note["session"], note["title"], note["message"], timeout=60,
                                       critical=note["level"] == "critical")

    def _check_notes(self, mono: float) -> None:
        """A warning the agent did not acknowledge within a few seconds (hung, suspended, or an
        older agent that never answers) is shown by the service itself; anything else is dropped."""
        for note_id, note in list(self._pending_notes.items()):
            if mono - note["at"] < NOTIFY_ACK_SECONDS:
                continue
            del self._pending_notes[note_id]
            if note["level"] in ("warning", "critical"):
                self._notify_fallback(note)

    def _send_agent(self, session_ids: list[int], message: dict[str, Any]) -> int:
        if self.agent_server is None:
            return 0
        return sum(1 for sid in session_ids if self.agent_server.send(sid, message))

    def _apply_block(self, user: str, blocked: bool) -> bool:
        """Block or unblock the login of ``user``; True when the account is now in that state.
        The event log gets a line when the state changed, not for every time it is confirmed."""
        if self.blocker is None:
            return True
        try:
            ok = bool(self.blocker.block(user) if blocked else self.blocker.unblock(user))
        except Exception:  # noqa: BLE001
            log.exception("block change for %s failed", user)
            ok = False
        if ok:
            if self._block_applied.get(user) != blocked:
                self.store.log_event(user, "login_block" if blocked else "login_unblock", self.cfg.time_limits.block_method)
            self._block_applied[user] = blocked
            self._pending_unblocks.discard(user)
        return ok

    def _reassert_blocks(self, now: datetime, mono: float) -> None:
        if self.blocker is None or mono - self._block_asserted_at < self.cfg.time_limits.reassert_block_seconds:
            return
        self._block_asserted_at = mono
        for user in self.timekeeper.managed_users():
            desired = self._status(user, now).block_desired
            try:
                actual = self.blocker.is_blocked(user)
            except Exception:  # noqa: BLE001
                actual = None
            # "actual is None": the account cannot be read (or the method has nothing to read):
            # then what we last applied is all there is to go by
            if (actual is not None and actual != desired) or self._block_applied.get(user) != desired:
                self._apply_block(user, desired)
        for user in list(self._pending_unblocks):   # no longer managed, and the unblock failed so far
            self._unblock_tries[user] = self._unblock_tries.get(user, 0) + 1
            if self.timekeeper.is_managed(user) or self._unblock_tries[user] > 12:
                self._pending_unblocks.discard(user)   # managed again, or the account is gone for good
            elif self._apply_block(user, False):
                self._emit_user_event(user, "login_unblocked")
            if user not in self._pending_unblocks:
                self._unblock_tries.pop(user, None)

    # -- activity ---------------------------------------------------------------
    def _pick_session(self, sessions: list[SessionInfo]) -> Optional[SessionInfo]:
        candidates = [s for s in sessions if s.state == "active"]
        if not candidates:
            return None
        candidates.sort(key=lambda s: (bool(s.locked), s.report is None, PRESENCE_ORDER.get(s.presence or "none", 9),
                                       s.idle_seconds if s.idle_seconds is not None else 1e9))
        return candidates[0]

    def _track_activity(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        for user, view in list(self._users.items()):
            # one user at a time: what goes wrong for one seat must not cost the others their figures
            self._step(f"activity of {user}", self._track_user, user, view, now, by_user.get(user, []))

    def _track_user(self, user: str, view: _UserView, now: datetime, sessions: list[SessionInfo]) -> None:
        today = now.date()
        now_ts = now.timestamp()
        best = self._pick_session(sessions)
        view.best_session = best.session_id if best else None
        activity = self._session_activity(best)
        view.activity = activity
        last = view.last_activity_tick
        view.last_activity_tick = now_ts if sessions else None
        if not sessions or last is None:
            return
        if not self.cfg.activity.track_unmanaged_users and not self.timekeeper.is_managed(user):
            return
        dt = max(0.0, now_ts - last)
        if dt > 120.0:  # the PC slept or the clock jumped: that time was not spent in the app
            dt = float(self.cfg.time_limits.tick_seconds)
        if dt > 0 and best is not None:
            state = "locked" if best.locked else (best.presence or "active")
            if state in PRESENCE_KINDS:
                self.store.add_hourly(user, now, "presence", state, dt)
        if dt <= 0 or not activity.is_active:
            return
        app_label = (activity.app or "").rsplit(".exe", 1)[0] or activity.app or "unknown"
        app_label = self._recorded_name(user, today, "apps", app_label)
        if activity.category == "gaming":
            self.store.add_usage(user, today, gaming=dt)
            self.store.add_app(user, today, f"game:{app_label}", dt)
            self.store.add_hourly(user, now, "game", app_label, dt)
        else:
            self.store.add_app(user, today, f"app:{app_label}", dt)
            self.store.add_hourly(user, now, "app", app_label, dt)
        if activity.domain:
            domain = self._recorded_name(user, today, "sites", activity.domain)
            self.store.add_site(user, today, domain, dt)
            self.store.add_hourly(user, now, "site", domain, dt)
        if activity.domain or activity.category == "browsing":
            if activity.category != "gaming":
                self.store.add_usage(user, today, browsing=dt)
        policy = self._effective_policy(user, self.timekeeper.policy_for(user))
        if policy is not None and policy.rules:
            self.rules.account(user, policy, today, activity, dt)

    def _session_activity(self, session: Optional[SessionInfo]) -> Activity:
        """What is going on at one seat, from its agent's report."""
        if session is None:
            return Activity(category="none")
        if session.locked:
            return Activity(category="locked")
        if session.presence == "away":
            return Activity(category="away")
        activity = self.classifier.classify(session.report, locked=session.locked, presence=session.presence)
        host = session.report.host if session.report is not None else None
        if host and not activity.url and activity.is_active and self.classifier.is_browser(activity.app or ""):
            # the agent could not read the address bar's text but knows the host (url_method
            # other than uia): rules and site categories match on the full host all the same
            url = "http://" + host
            activity = replace(activity, url=url, domain=activity.domain or host,
                               category=self.classifier.site_category(activity.domain or host, url))
        return activity

    def _recorded_name(self, user: str, today: date, kind: str, name: str) -> str:
        """The name a program or website is recorded under.  The names come from the agent, so
        there is a limit to how many different ones a user gets per day; beyond it time is
        booked on "other" instead of filling the database with made-up names."""
        name = _clean(name, MAX_STORED_NAME).strip() or "unknown"
        key = (user, today.isoformat(), kind)
        seen = self._names_seen.get(key)
        if seen is None:
            for old in [k for k in self._names_seen if k[0] == user and k[2] == kind]:
                del self._names_seen[old]
            if kind == "apps":
                seen = {a.split(":", 1)[-1] for a, _s in self.store.top_apps(user, today, 10 ** 6)}
            else:
                seen = {d for d, _s in self.store.top_sites(user, today, 10 ** 6)}
            self._names_seen[key] = seen
        if name in seen:
            return name
        if len(seen) >= MAX_NAMES_PER_DAY:
            return OTHER_NAME
        seen.add(name)
        return name

    # -- per-app rules & focus mode ----------------------------------------------------
    def _enforce_rules(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        for user, sessions in by_user.items():
            self._step(f"rules of {user}", self._enforce_user, user, now, sessions)

    def _enforce_user(self, user: str, now: datetime, sessions: list[SessionInfo]) -> None:
        """Rules and focus mode for every seat of ``user`` an agent reports on - with two
        sessions of the same user (multiseat) the second is enforced as well, not only the
        "best" one.  The rule time itself is counted once per user (``_track_user``)."""
        view = self._users[user]
        previous, view.closings = view.closings, {}   # put back below while the same window is still there
        policy = self._effective_policy(user, self.timekeeper.policy_for(user))
        if policy is None or policy.unlimited:
            return
        best = self._pick_session(sessions)
        seats = [s for s in sessions if s.state == "active" and s.report is not None]
        if not seats:
            return
        seats.sort(key=lambda s: s is not best)   # the main seat first: it gets the notifications that are due
        st = self._status(user, now)
        focus_active = st.focus_active
        if not (policy.rules or focus_active):
            return
        extra_allow = self.timekeeper.focus_extra_allow(user) if focus_active else None
        for seat in seats:
            activity = view.activity if seat is best else self._session_activity(seat)
            # Our own agent in front (its tray menu) is never closed or warned about.  It is told by
            # the process id we started - not by its name, and not by a pid the agent claims.
            fg = seat.report.foreground
            slot = self._agents.get(seat.session_id)
            agent_pid = getattr(slot.handle, "pid", None) if slot is not None and slot.handle is not None else None
            decisions = self.rules.tick(
                user, policy, now, activity, focus_active=focus_active, focus_extra_allow=extra_allow,
                enforcing=st.enforcing, agent_foreground=bool(fg is not None and agent_pid and fg.pid == agent_pid),
                unknown_site_pending=self._unknown_site_pending(seat.session_id, fg, activity, focus_active),
            )
            if seat.session_id in previous:
                view.closings[seat.session_id] = previous[seat.session_id]
            closed = False
            for decision in decisions:
                try:
                    closed = self._apply_decision(user, seat, decision, closed, activity) or closed
                except Exception:  # noqa: BLE001
                    log.exception("rule decision %s for %s failed", decision.slug, user)
            if not closed:
                view.closings.pop(seat.session_id, None)

    def _unknown_site_pending(self, session_id: int, fg: Any, activity: Activity, focus_active: bool = True) -> bool:
        """Has the web app window in front had an unknown site only for a moment?

        The agent's first read of a web app can fail or come late; focus mode waits
        UNKNOWN_SITE_CONFIRM_SECONDS for the same window (process, and app id when there
        is one - not the title, which a page can keep changing) before closing it.  A
        read with a site in between ends the wait; a window that stays unknown is closed."""
        if not focus_active or not activity.site_unknown or fg is None:
            self._unknown_sites.pop(session_id, None)   # the wait starts afresh with focus mode
            return False
        window = (fg.pid, (fg.name or "").lower(), activity.app_id)
        mono = self._mono()
        seen = self._unknown_sites.get(session_id)
        # a record not looked at for a while (focus was off, no rules ran) is not continuous
        stale = seen is not None and not 0 <= mono - seen[2] <= UNKNOWN_SITE_CONFIRM_SECONDS + 2 * self.cfg.time_limits.tick_seconds
        if seen is None or seen[0] != window or stale:
            self._unknown_sites[session_id] = (window, mono, mono)
            return True
        self._unknown_sites[session_id] = (window, seen[1], mono)
        return mono - seen[1] < UNKNOWN_SITE_CONFIRM_SECONDS

    def _apply_decision(self, user: str, session: SessionInfo, decision: RuleDecision, closed: bool = False,
                        activity: Optional[Activity] = None) -> bool:
        """Carry out one decision of the rule engine; True when it asked for a window to be closed."""
        fg = session.report.foreground if session.report else None
        if decision.notify:
            self.notify_sessions([session.session_id], decision.title, decision.message, decision.level)
            if decision.minutes_left == 0 or decision.source == "focus":
                self._emit_user_event(user, "app_limit_reached", rule=decision.slug, source=decision.source,
                                      app=decision.app, action=decision.kind)
        if closed or decision.kind != "close" or fg is None or not fg.name or not closable(fg.name):
            return False
        self._close_window(user, session, fg, decision, activity)
        return True

    def _close_window(self, user: str, session: SessionInfo, fg: Any, decision: RuleDecision,
                      activity: Optional[Activity] = None) -> None:
        """Ask the agent to close what is in front - and keep at it.

        The request is repeated on every tick for as long as the window stays, but the
        ``app_closed`` event and the log line are written once per window.  When it is still
        there after CLOSE_ESCALATE_SECONDS, or the agent says it could not do it, the next
        step follows: the whole program instead of the tab, then locking the seat
        (``app_close_failed``) - a window that cannot be closed must not stay usable."""
        view = self._users[user]
        mono = self._mono()
        activity = activity or view.activity
        key = (session.session_id, fg.name.lower(), fg.pid, activity.domain if decision.close_tab else None)
        closing = view.closings.get(session.session_id)
        if closing is None or closing["key"] != key:
            closing = view.closings[session.session_id] = {
                "key": key, "step": "tab" if decision.close_tab else "process", "since": mono,
                "failed": 0, "announced": False, "session": session.session_id}
        elif closing["step"] != "lock" and (mono - closing["since"] >= CLOSE_ESCALATE_SECONDS or closing["failed"] >= 2):
            closing.update(step="process" if closing["step"] == "tab" else "lock", since=mono, failed=0, announced=False)
        if closing["step"] == "lock":
            if not closing["announced"]:
                closing["announced"] = True
                log.warning("%s could not be closed for %s; locking session %s", fg.name, user, session.session_id)
                self.store.log_event(user, "app_close_failed", f"{fg.name} rule={decision.slug}")
                self._emit_user_event(user, "app_close_failed", rule=decision.slug, app=fg.name)
            self._lock_session(session.session_id)
            return
        tab = closing["step"] == "tab"
        message = {"type": "close", "pid": fg.pid, "name": fg.name, "title": fg.title, "tab": tab, "reason": decision.title}
        if fg.created:
            message["created"] = fg.created   # the agent only touches the process that was reported, not a reused pid
        if not self._send_agent([session.session_id], message):
            log.info("cannot close %s for %s: no agent in session %s", fg.name, user, session.session_id)
            return
        if not closing["announced"]:
            closing["announced"] = True
            self.store.log_event(user, "app_close", f"{fg.name} ({'tab' if tab else 'process'}) rule={decision.slug}")
            self._emit_user_event(user, "app_closed", rule=decision.slug, app=fg.name, tab=tab)

    def _close_result(self, session_id: int, message: dict[str, Any]) -> None:
        """The agent's answer to a close request: a failure brings the next step forward."""
        result = _clean(message.get("result"), 40)
        log.info("agent in session %s closed %s (%s)", session_id, _clean(message.get("name"), 80), result)
        # "error": the agent could not even read the request (malformed) - nothing was closed,
        # which is a failure like any other and brings the next step forward
        failed = result in ("tab_failed", "noop", "refused", "terminated 0", "error") \
            or result.startswith(("refused", "failed", "error"))
        pid = _int_or_none(message.get("pid"))
        if not failed:
            return
        for view in self._users.values():
            # only the answer to the request a rule made (same seat, same process) counts; an
            # "error" may come without the pid (it could not be read): then the seat is enough
            closing = view.closings.get(session_id)
            if closing is not None and (closing["key"][2] == pid or (not pid and result.startswith("error"))):
                closing["failed"] += 1

    # -- break reminders -----------------------------------------------------------------
    def _wellbeing(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        for user, sessions in by_user.items():
            self._step(f"breaks of {user}", self._wellbeing_user, user, now, sessions)

    def _wellbeing_user(self, user: str, now: datetime, sessions: list[SessionInfo]) -> None:
        policy = self.timekeeper.policy_for(user)
        if policy is None:
            return
        enabled = self.timekeeper.breaks_enabled(user, policy)
        engaged = any(s.presence in ("active", "passive") for s in sessions)
        # what the agents say of the seats in use: the last real input, and whether a film is on
        seated = [s for s in sessions if s.presence in ("active", "passive") and s.report is not None]
        input_idle = min((s.report.idle_seconds for s in seated), default=None)
        due = self.breaks.tick(user, policy.breaks, now.timestamp(), now.date(), engaged=engaged, enabled=enabled,
                               input_idle_seconds=input_idle, passive=any(s.presence == "passive" for s in seated))
        if due:
            targets = [s.session_id for s in sessions if s.presence in ("active", "passive")]
            self.notify_sessions(targets, due["title"], due["message"], "info")
            self.store.log_event(user, "break_reminder", f"after {due['continuous_minutes']} min")
            self._emit_user_event(user, "break_due", continuous_minutes=due["continuous_minutes"])

    # -- transitions -> device trigger events ---------------------------------------------
    def _transitions(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        for user, view in list(self._users.items()):
            self._step(f"transitions of {user}", self._transitions_user, user, view, now, by_user.get(user, []))

    def _transitions_user(self, user: str, view: _UserView, now: datetime, sessions: list[SessionInfo]) -> None:
        logged_in = any(s.logged_in for s in sessions)
        if view.logged_in is not None and logged_in != view.logged_in:
            self._emit_user_event(user, "logged_in" if logged_in else "logged_off")
            self.store.log_event(user, "logged_in" if logged_in else "logged_off", "")
            if not logged_in:
                self.breaks.reset(user)
        view.logged_in = logged_in
        presence = presence_user_state(sessions)
        if view.presence is not None and presence != view.presence:
            if presence == "away" and view.presence != "none":
                self._emit_user_event(user, "presence_away", reason=self._presence_reason(sessions))
            elif view.presence == "away" and presence in ("active", "passive"):
                self._emit_user_event(user, "presence_back")
        view.presence = presence
        if not self.timekeeper.is_managed(user):
            view.time_up_sent = None
            return
        st = self._status(user, now)
        if not logged_in or not (st.enforcing and st.out_of_time):
            view.time_up_sent = None   # gone, or has time again: the next "time is up" is a new one
        if view.focus_active is not None and st.focus_active != view.focus_active:
            self._emit_user_event(user, "focus_started" if st.focus_active else "focus_ended", source=st.focus_source)
            if not st.focus_active:
                self.rules.reset(user)
        view.focus_active = st.focus_active
        self._expire_time_request(user)
        self._weekly_summary(user, now)

    def _presence_reason(self, sessions: list[SessionInfo]) -> str:
        best = self._pick_session(sessions)
        return best.presence_reason if best else ""

    # -- "ask for more time" ---------------------------------------------------------------
    # what keeps a user off the PC that more minutes cannot help with
    _NOT_FOR_BONUS = {"hours": "it is outside the allowed hours", "bedtime": "it is bedtime",
                      "manual": "access is paused"}

    def _time_request_options(self, user: str, st: Optional[TimeStatus] = None) -> list[int]:
        """The minutes ``user`` may ask for from the tray ([]: not offered).  Nothing is
        offered while the allowed hours, bedtime or a pause keep the user off: bonus
        minutes cannot help with those, and asking would only raise false hopes."""
        requests = self.cfg.time_limits.time_requests
        if not requests.enabled or not requests.options or not self.timekeeper.is_managed(user):
            return []
        if st is None:
            st = self._status(user, self.clock())
        if st.limiting_factor in self._NOT_FOR_BONUS:
            return []
        return [int(m) for m in requests.options]

    def pending_time_request(self, user: str) -> Optional[dict[str, Any]]:
        """The request ``user`` made that nobody answered yet: {"minutes", "at", "expires"} (wall time)."""
        pending = self.store.get_setting(user, SETTING_TIME_REQUEST)
        if not isinstance(pending, dict) or not isinstance(pending.get("minutes"), int) \
                or not isinstance(pending.get("expires"), (int, float)):
            return None
        return pending

    def _time_request_stale(self, pending: dict[str, Any], today: date) -> bool:
        """A request is for the day it was made on (``day``; older ones have none): after
        midnight approving it would credit the new day, so it has expired instead."""
        day = pending.get("day")
        return isinstance(day, str) and day != today.isoformat()

    def _time_request(self, session_id: int, minutes: Any) -> None:
        """A child asked for more time from the tray.  The tray shows nothing of its own, so
        every request is answered with a notification - also one that is not taken."""
        user = next((s.user for s in self._last_sessions if s.session_id == session_id), None)
        if user is None:
            return
        requests = self.cfg.time_limits.time_requests
        now_ts, today = self._wall(), self.clock().date()

        def answer(text: str) -> None:
            self.notify_sessions([session_id], "More time", text, "info")

        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in self._time_request_options(user):
            why = self._NOT_FOR_BONUS.get(self._status(user, self.clock()).limiting_factor or "") \
                if self.timekeeper.is_managed(user) else None
            return answer(f"More minutes would not help: {why}." if why else "Asking for more time is not available.")
        if self.pending_time_request(user) is not None:
            return answer("You have already asked. Please wait for the answer.")
        last = self.store.get_setting(user, SETTING_TIME_REQUEST_LAST)
        wait = requests.cooldown_minutes * 60 - (now_ts - last) if isinstance(last, (int, float)) else 0
        if 0 < wait <= requests.cooldown_minutes * 60:
            return answer(f"You asked a moment ago. You can ask again in {max(1, math.ceil(wait / 60))} minutes.")
        count_key = f"{SETTING_TIME_REQUEST_COUNT}:{today.isoformat()}"
        asked = int(self.store.get_setting(user, count_key, 0) or 0)
        if requests.max_per_day > 0 and asked >= requests.max_per_day:
            return answer("You cannot ask for more time again today.")
        pending = {"minutes": minutes, "at": now_ts, "expires": now_ts + max(1, requests.expire_minutes) * 60,
                   "day": today.isoformat()}
        self.store.set_setting(user, SETTING_TIME_REQUEST, pending)
        self.store.set_setting(user, SETTING_TIME_REQUEST_LAST, now_ts)
        self.store.set_setting(user, count_key, asked + 1)
        self.store.log_event(user, "time_requested", f"{minutes} min")
        self._emit_user_event(user, "time_requested", minutes=minutes)
        answer(f"You asked for {minutes} more minutes. Waiting for an answer.")

    def _answer_time_request(self, user: str, approve: bool, minutes: Any = None, pin: Any = None) -> dict[str, Any]:
        pending = self.pending_time_request(user)
        if pending is None:
            return {"ok": False, "error": f"{user} has not asked for more time"}
        sessions = [s.session_id for s in self._last_sessions if s.user == user and s.state == "active"]
        now = self.clock()
        if approve and self._time_request_stale(pending, now.date()):
            # asked yesterday, answered after midnight: granting would credit today
            self._expire_time_request(user, force=True)
            return {"ok": False, "error": f"{user} asked on {pending['day']}; the request expired at midnight"}
        if approve:
            granted = pending["minutes"] if minutes is None else minutes
            if isinstance(granted, bool) or not isinstance(granted, (int, float)) or not math.isfinite(granted) \
                    or not 1 <= granted <= 480:
                return {"ok": False, "error": "minutes must be between 1 and 480"}
            granted = int(granted)
            why = self._NOT_FOR_BONUS.get(self._status(user, now).limiting_factor or "")
            if why:
                # The minutes would change nothing now: say so to the parent rather than tell the
                # child "you got 30 more minutes".  The request stays open (deny it, or it expires).
                return {"ok": False, "error": f"more minutes would not help {user}: {why} "
                                              "(change the allowed hours or extend bedtime instead)"}
            if (ref := self._gate("grant", "time_request_answer", user, pin)):
                return ref
            self.timekeeper.add_bonus_minutes(user, now.date(), granted, reason="time request")
        self.store.delete_setting(user, SETTING_TIME_REQUEST)
        if approve:
            self.store.log_event(user, "time_request_approved", f"{granted} min")
            self._emit_user_event(user, "time_request_approved", minutes=granted)
            self.notify_sessions(sessions, "More time", f"You got {granted} more minutes.", "info")
            return {"ok": True, "approved": True, "minutes": granted}
        self.store.log_event(user, "time_request_denied", f"{pending['minutes']} min")
        self._emit_user_event(user, "time_request_denied", minutes=pending["minutes"])
        self.notify_sessions(sessions, "More time", "Your request for more time was not granted.", "info")
        return {"ok": True, "approved": False}

    def _expire_time_request(self, user: str, force: bool = False) -> None:
        """A request nobody answered in time - or by midnight of the day it was made - expires."""
        pending = self.pending_time_request(user)
        if pending is None:
            return
        if not force and self._wall() < pending["expires"] and not self._time_request_stale(pending, self.clock().date()):
            return
        self.store.delete_setting(user, SETTING_TIME_REQUEST)
        self.store.log_event(user, "time_request_expired", f"{pending['minutes']} min")
        self._emit_user_event(user, "time_request_expired", minutes=pending["minutes"])
        self.notify_sessions([s.session_id for s in self._last_sessions if s.user == user and s.state == "active"],
                             "More time", "Nobody answered your request for more time.", "info")

    def _time_request_payload(self, user: str, now: datetime) -> Optional[dict[str, Any]]:
        pending = self.pending_time_request(user)
        if pending is None:
            return None
        return {"minutes": pending["minutes"], "at": _iso(pending.get("at"), now.tzinfo),
                "expires": _iso(pending["expires"], now.tzinfo)}

    def _stored_summary(self, user: str) -> dict[str, Any]:
        stored = self.store.get_setting(user, SETTING_WEEKLY_SUMMARY)
        return stored if isinstance(stored, dict) else {}

    # -- profiles (holiday / school day) -------------------------------------------------------
    def _set_profile(self, user: Optional[str], name: Any, pin: Any = None) -> dict[str, Any]:
        """Switch one user - or, without a user, everybody who has it - to a profile ("normal":
        back to the usual limits).  A switch that gives more needs the parent PIN."""
        tk = self.timekeeper
        wanted = str(name if name is not None else "").strip().lower()
        if not wanted or len(wanted) > 32:
            return {"ok": False, "error": "profile required"}
        normal = wanted == "normal"
        if user:
            if not tk.is_managed(user) or not (normal or wanted in tk.profile_names(user)):
                return {"ok": False, "error": f"{user} has no profile {_clean(wanted, 32)}"}
            users = [user]
        else:
            users = [u for u in tk.managed_users() if (tk.profile_names(u) if normal else wanted in tk.profile_names(u))]
            if not users:
                return {"ok": False, "error": f"nobody has a profile {_clean(wanted, 32)}"}
        now = self.clock()
        if any(tk.is_loosening_profile(u, wanted, now) for u in users) \
                and (ref := self._gate("grant", "set_profile", user, pin)):
            return ref
        changed = []
        for name_ in users:
            before = tk.active_profile(name_) or "normal"
            tk.set_profile(name_, wanted)
            after = tk.active_profile(name_) or "normal"
            self._ensure_user(name_)
            if after != before:
                changed.append(name_)
                self._policy_cache.pop(name_, None)
                self._rules_policy_cache.pop(name_, None)
                self._emit_user_event(name_, "profile_changed", profile=after)
        return {"ok": True, "profile": wanted, "users": users, "changed": changed}

    # -- weekly summary ---------------------------------------------------------------------------
    def _weekly_summary(self, user: str, now: datetime) -> None:
        """Once per week and user: what the week that ended looked like.  It is built on the
        first tick of a new week - and at start when last week's is missing - and kept as a
        setting, so the state payload has it without a query."""
        today = now.date()
        start = self.timekeeper.week_start(today) - timedelta(days=7)
        if self._summary_checked.get(user) == start:
            return
        stored = self.store.get_setting(user, SETTING_WEEKLY_SUMMARY)
        if isinstance(stored, dict) and stored.get("week_start") == start.isoformat():
            self._summary_checked[user] = start
            return
        # (marked as done only at the end: a summary that could not be built or stored is
        # tried again on the next tick, not next week)
        end = start + timedelta(days=6)
        figures = self.store.summary_range(user, start, end, top=5)
        days = {day: round(seconds / 60) for day, seconds in figures["days"].items()}
        total = round(figures["active_seconds"] / 60)
        busiest = max(days, key=lambda d: days[d]) if total else None
        since = datetime.combine(start, datetime.min.time(), tzinfo=now.tzinfo).timestamp()
        until = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo).timestamp()

        def top(items: list[dict[str, Any]]) -> dict[str, int]:
            return {item["name"]: round(item["seconds"] / 60) for item in items}

        summary = {
            "week_start": start.isoformat(), "week_end": end.isoformat(),
            "total_minutes": total, "days": days, "daily_average_minutes": round(total / 7),
            "busiest_day": busiest, "busiest_day_minutes": days[busiest] if busiest else 0,
            "top_apps": top(figures["apps"]), "top_sites": top(figures["sites"]), "top_games": top(figures["games"]),
            "time_ran_out": self.store.count_events(user, "time_up", since, until),
            "bonus_minutes": round(sum(self.timekeeper.local_bonus_seconds(user, start + timedelta(days=i))
                                       for i in range(7)) / 60),
        }
        self.store.set_setting(user, SETTING_WEEKLY_SUMMARY, summary)
        self._summary_checked[user] = start
        # A week nothing was recorded in (a fresh install, a PC that stayed off) is kept - the
        # payload says so - but not announced: "0 minutes last week" would be noise or wrong.
        if not (figures["active_seconds"] or figures["apps"] or figures["sites"] or figures["games"]):
            return
        self.store.log_event(user, "weekly_summary", f"{start.isoformat()}: {total} min")
        self._emit_user_event(user, "weekly_summary", minutes=total, week_start=start.isoformat())

    # -- events ----------------------------------------------------------------------------
    def _emit_user_event(self, user: str, kind: str, **data: Any) -> None:
        self._stats["events"] += 1
        payload = {"ts": time.time(), "user": user, "event": kind, **{k: v for k, v in data.items() if v is not None}}
        log.debug("event %s for %s %s", kind, user, data)
        self._recent_events.append(payload)
        if self.api is not None:
            self.api.push({"type": "event", **payload})

    def _emit_pc_event(self, kind: str, **data: Any) -> None:
        self._stats["events"] += 1
        payload = {"ts": time.time(), "event": kind, **{k: v for k, v in data.items() if v is not None}}
        self._recent_events.append(payload)
        if self.api is not None:
            self.api.push({"type": "event", **payload})

    # -- agent status (tray tooltip) ---------------------------------------------
    def _push_agent_status(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        if self.agent_server is None:
            return
        # An agent counts what is the same for the whole PC (a game controller, "keep the display
        # on") as its own only when no other seat is in use.  It cannot always see the other seats
        # itself (inside an ASTER workplace Windows lists none), so it is told how many there are.
        seats = sum(1 for ss in by_user.values() for s in ss if s.state == "active")
        for user, sessions in by_user.items():
            st = self._status(user, now)
            if st.managed and st.enforcing:
                if st.effective_remaining_seconds is None:
                    text = "No time limit today"
                else:
                    text = f"{human_minutes(st.effective_remaining_seconds)} left today"
                if st.manually_blocked:
                    text = "Computer access paused"
                elif st.limiting_factor == "bedtime":
                    text = "Bedtime"
                elif st.limiting_factor == "hours":
                    text = "Outside allowed hours"
                elif st.focus_active:
                    text = f"Focus mode ({human_minutes((st.focus_until or 0) - now.timestamp())} left)"
            else:
                text = "No time limit"
            offered = self._time_request_options(user, st)
            payload = {"type": "status", "text": text, "remaining_minutes": st.remaining_minutes,
                       "used_minutes": round(st.used_seconds / 60), "limit_minutes": st.limit_minutes,
                       "focus": st.focus_active, "time_requests": offered}   # what "Ask for more time" offers
            # remembered per seat, and forgotten when its agent (re)connects: every agent gets the
            # status that applies, not only the first seat or the first agent
            for s in sessions:
                others = seats - (s.state == "active")
                shown = (text, tuple(offered), others)
                if self._agent_status_sent.get(s.session_id) != shown and \
                        self.agent_server.send(s.session_id, dict(payload, other_seats=others)):
                    self._agent_status_sent[s.session_id] = shown

    # ------------------------------------------------------------------
    # state for Home Assistant
    # ------------------------------------------------------------------
    def user_status(self, sessions: list[SessionInfo]) -> str:
        logged = [s for s in sessions if s.logged_in]
        if not logged:
            return "logged_off"
        active = [s for s in logged if s.state == "active"]
        if not active:
            return "disconnected"
        unlocked = [s for s in active if not s.locked]
        if not unlocked:
            return "locked"
        states = [s.presence or "active" for s in unlocked]
        if any(st in ("active", "passive") for st in states):
            return "active"
        if any(st == "idle" for st in states):
            return "idle"
        return "away"

    def _payload(self, user: str, now: datetime, sessions: list[SessionInfo]) -> dict[str, Any]:
        """``user_payload``; inside a tick it is built once and shared by everything that reads it."""
        if self._tick_now is not now:
            return self.user_payload(user, now, sessions)
        payload = self._tick_payloads.get(user)
        if payload is None:
            payload = self._tick_payloads[user] = self.user_payload(user, now, sessions)
        return payload

    def user_payload(self, user: str, now: datetime, sessions: list[SessionInfo]) -> dict[str, Any]:
        st: TimeStatus = self._status(user, now)
        policy: Optional[UserPolicy] = self.timekeeper.policy_for(user)
        view = self._ensure_user(user)
        today = now.date()
        totals = self.store.usage(user, today)
        top_apps = self.store.top_apps(user, today, self.cfg.activity.top_n * 2)
        games = {a[5:]: round(s / 60, 1) for a, s in top_apps if a.startswith("game:")}
        apps = {a.split(":", 1)[1]: round(s / 60, 1) for a, s in top_apps}
        sites = {d: round(s / 60, 1) for d, s in self.store.top_sites(user, today, self.cfg.activity.top_n)}
        act = view.activity
        best = self._pick_session(sessions)
        report = best.report if best else None
        presence = self._presence.get(best.session_id) if best else None
        media = report.media if report else None
        unlimited = 1440
        payload: dict[str, Any] = {
            "user": user,
            "name": policy.label if policy else user,
            "date": today.isoformat(),
            "managed": st.managed,
            "status": self.user_status(sessions),
            "presence": presence_user_state(sessions),
            "presence_details": presence.to_dict() if presence else {"state": "none"},
            "logged_in": any(s.logged_in for s in sessions),
            "sessions": [s.to_dict() for s in sessions],
            "activity": act.category,
            # no query string / fragment and a capped title: this goes to HA (and its history) on every push
            "activity_details": {"app": act.app, "exe": act.exe,
                                 "title": _clean(act.title, MAX_PUBLISHED_TITLE) if act.title else act.title,
                                 "domain": act.domain, "url": _public_url(act.url),
                                 # a web app window whose address could not be read (domain only a guess)
                                 "site_unknown": act.site_unknown, "app_id": act.app_id},
            "app": act.app,
            "domain": act.domain,
            "now_playing": (media.label() or None) if media and media.is_playing else None,
            "media": media.to_dict() if media else {},
            "volume": report.volume if report else None,
            "muted": report.muted if report else None,
            # the seat's display as its agent sees it (None: no agent, or Windows did not say)
            "display_on": (report.display_state != "off") if report and report.display_state else None,
            "used_minutes": round(totals.active / 60, 1),
            "gaming_minutes": round(totals.gaming / 60, 1),
            "browsing_minutes": round(totals.browsing / 60, 1),
            "weekly_used_minutes": round(st.weekly_used_seconds / 60, 1) if st.managed else round(self._week_used(user, today) / 60, 1),
            "top_games": games,
            "top_apps": dict(list(apps.items())[: self.cfg.activity.top_n]),
            "top_app": next(iter(apps), None),
            "top_sites": sites,
            "history": self.store.history(user, self.cfg.activity.history_days, today),
            "usage_recent": self._recent_usage_cached(user, now),
            **{f"{kind}_minutes_today": minutes for kind, minutes in self.presence_today(user, now).items()
               if kind in ("idle", "away", "locked")},
            "time_limits_enabled": self.timekeeper.global_enabled,
            "screenshot": view.screenshot,
            # a sign that the user worked around PC Link (see _flag_tamper); stays until a parent clears it
            "tamper": view.tamper is not None,
            "tamper_reason": view.tamper["reason"] if view.tamper else None,
            "tamper_at": _iso(view.tamper.get("ts"), now.tzinfo) if view.tamper else None,
            "tamper_details": {"reason": view.tamper["reason"], "at": _iso(view.tamper.get("ts"), now.tzinfo),
                               "detail": view.tamper.get("detail") or None} if view.tamper else {},
        }
        if st.managed and policy is not None:
            weekday_default = policy.daily_limits.get(0)
            weekend_default = policy.daily_limits.get(5)
            focus_minutes = int(self.store.get_setting(user, "focus_minutes", policy.focus.default_minutes) or policy.focus.default_minutes)
            effective = self._effective_policy(user, policy)
            rule_statuses = self.rules.statuses(user, effective, now, act)
            blocked_items = self.blocked_items(user)
            custom = self._custom_rules(user)
            configured = {r.slug for r in policy.rules}
            rules = {}
            for rule, rs in zip(effective.rules, rule_statuses):
                d = rs.to_dict()
                d["state"] = rs.state
                d["limit_minutes"] = unlimited if rs.limit_minutes is None else rs.limit_minutes
                d["weekly_limit"] = rs.weekly_limit_minutes or 0
                if rule.definition:  # not for the block list's entries, which are no rules to edit
                    d["definition"] = rule.definition
                    d["source"] = ("config" if rs.slug not in custom else
                                   "config, changed in Home Assistant" if rs.slug in configured else "Home Assistant")
                rules[rs.slug] = d
            payload.update(
                {
                    "remaining_minutes": st.remaining_minutes,
                    "limit_minutes": st.limit_minutes,
                    "limit_today": unlimited if st.limit_minutes is None else st.limit_minutes,
                    "weekday_limit": st.weekday_limit if st.weekday_limit is not None else (unlimited if weekday_default is None else weekday_default),
                    "weekend_limit": st.weekend_limit if st.weekend_limit is not None else (unlimited if weekend_default is None else weekend_default),
                    "weekly_limit": st.weekly_limit_minutes or 0,
                    "weekly_limit_minutes": st.weekly_limit_minutes,
                    "weekly_remaining_minutes": st.weekly_remaining_minutes,
                    "weekly_details": {
                        "week_start": self.timekeeper.week_start(today).isoformat(),
                        "limit_minutes": st.weekly_limit_minutes,
                        "used_minutes": round(st.weekly_used_seconds / 60, 1),
                        "remaining_minutes": st.weekly_remaining_minutes,
                        "other_pcs_today_minutes": round(st.external_used_seconds / 60, 1),
                    },
                    "rollover_minutes": round(st.rollover_seconds / 60),
                    # the bonus that counts (with a shared budget: also what other PCs granted) and,
                    # for the number entity and the other PCs, what was granted on this PC alone
                    "bonus_minutes": round(st.bonus_seconds / 60),
                    "bonus_local_minutes": round(self.timekeeper.local_bonus_seconds(user, today) / 60, 1),
                    "bedtime_extension_local_minutes": self.timekeeper.local_bedtime_extension_minutes(
                        user, self.timekeeper.bedtime_day(user, now)),
                    "profile": self.timekeeper.active_profile(user) or "normal",
                    "profiles": ["normal", *self.timekeeper.profile_names(user)],
                    "time_request": self._time_request_payload(user, now),
                    "time_request_minutes": (self.pending_time_request(user) or {}).get("minutes", 0),
                    "weekly_summary": self._stored_summary(user),
                    "last_week_minutes": self._stored_summary(user).get("total_minutes", 0),
                    "earned_minutes": round(st.earned_seconds / 60),
                    "add_minutes": 0,
                    "limit_enabled": st.limit_enabled,
                    "blocked": st.manually_blocked,
                    "login_blocked": st.block_desired,
                    "in_allowed_hours": st.in_window,
                    "grace_active": st.grace_active,
                    "out_of_time": st.out_of_time,
                    "limiting_factor": st.limiting_factor,
                    "bedtime": _hhmm(st.bedtime_minute) if st.has_bedtime else "none",
                    "past_bedtime": st.past_bedtime,
                    "bedtime_extension_minutes": st.bedtime_extension_minutes,
                    "bedtime_details": {
                        "wake": _hhmm(st.wake_minute), "bedtime": _hhmm(st.bedtime_minute),
                        "extension_minutes": st.bedtime_extension_minutes,
                        "auto_extension_minutes": st.bedtime_auto_extension_minutes,
                        "remaining_minutes": None if st.bedtime_remaining_seconds is None else round(st.bedtime_remaining_seconds / 60),
                        "parent_present": self.timekeeper.parent_present,
                    } if st.has_bedtime else {},
                    "focus_active": st.focus_active,
                    "focus_minutes": focus_minutes,
                    "focus_remaining_minutes": round(max(0.0, (st.focus_until or 0) - now.timestamp()) / 60) if st.focus_active else 0,
                    "focus_details": {
                        "source": st.focus_source,
                        "until": _iso(st.focus_until, now.tzinfo) if st.focus_until else None,
                        "pauses_limit": st.focus_pauses_limit,
                        "allow_apps": policy.focus.allow_apps, "allow_sites": policy.focus.allow_sites,
                        "allow_categories": policy.focus.allow_categories,
                        "allow_unknown_sites": policy.focus.allow_unknown_sites,
                        "extra_allow": self.timekeeper.focus_extra_allow(user) if st.focus_active else [],
                    },
                    "blocked_count": len(blocked_items),
                    "blocked_details": {"items": blocked_items,
                                        "apps": [i for i in blocked_items if _is_app_item(i)],
                                        "sites": [i for i in blocked_items if not _is_app_item(i)]},
                    "breaks_enabled": self.timekeeper.breaks_enabled(user, policy),
                    **{k: v for k, v in self.breaks.status(user, policy.breaks, now.timestamp()).items() if k == "continuous_minutes"},
                    "break_details": self.breaks.status(user, policy.breaks, now.timestamp()),
                    "rules": rules,
                    "limit_details": {
                        "limit_minutes": st.limit_minutes,
                        "bonus_minutes": round(st.bonus_seconds / 60),
                        "rollover_minutes": round(st.rollover_seconds / 60),
                        "earned_minutes": round(st.earned_seconds / 60),
                        "used_minutes": round(st.used_seconds / 60, 1),
                        "window_remaining_minutes": None if st.window_remaining_seconds is None else round(st.window_remaining_seconds / 60),
                        "limit_today_override": st.limit_today_override,
                        "limiting_factor": st.limiting_factor,
                        "warn_at_minutes": policy.warn_at_minutes,
                        "action": policy.action,
                        "schedule": {day: policy.daily_limits.get(i) for i, day in enumerate(DAYS)},
                        "allowed_hours": {
                            day: ", ".join(f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in policy.allowed_windows.get(i, [])) or "any"
                            for i, day in enumerate(DAYS)
                        },
                        "bedtime_schedule": {day: _hhmm(policy.bedtime.get(i)) for i, day in enumerate(DAYS)} if policy.has_bedtime else None,
                        "block_method": self.cfg.time_limits.block_method,
                        "count_idle": policy.count_idle, "count_passive": policy.count_passive,
                        "pin_required": self.pin.enabled,
                        "last_refusal": view.last_refusal,
                        "rewards": [{"name": r.name, "minutes": r.minutes, "daily_max": r.daily_max,
                                     "claimed_today": self.timekeeper.reward_claims(user, r.slug, today)} for r in policy.rewards],
                    },
                }
            )
        return payload

    def usage_report(self, user: str, hours: Optional[float] = None, now: Optional[datetime] = None,
                     limit: int = 25) -> dict[str, Any]:
        """Applications, games and websites ``user`` used in the last ``hours`` hours, longest first.

        Usage is recorded per clock hour, so the window starts at the full hour ``hours`` ago
        (``from``); minutes are time in the foreground while the user was active."""
        now = now or self.clock()
        hours = float(self.cfg.activity.report_hours if hours is None else hours)
        hours = max(1.0, min(hours, 336.0))
        since = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        usage = self.store.recent_usage(user, since)

        def ranked(kind: str) -> dict[str, float]:
            items = sorted(usage.get(kind, {}).items(), key=lambda kv: kv[1], reverse=True)[:max(1, limit)]
            return {name: round(secs / 60, 1) for name, secs in items if secs >= 30}

        apps_seconds = sum(usage.get("app", {}).values()) + sum(usage.get("game", {}).values())
        presence = usage.get("presence", {})
        return {
            "user": user, "hours": round(hours, 1),
            "from": since.isoformat(timespec="minutes"), "to": now.isoformat(timespec="minutes"),
            "total_minutes": round(apps_seconds / 60, 1),
            "website_minutes": round(sum(usage.get("site", {}).values()) / 60, 1),
            # where the logged-in time went: at the PC (active), watching / listening (passive),
            # no input for a while (idle), gone (away), screen locked
            **{f"{kind}_minutes": round(presence.get(kind, 0.0) / 60, 1) for kind in PRESENCE_KINDS},
            "apps": ranked("app"), "games": ranked("game"), "sites": ranked("site"),
        }

    def presence_today(self, user: str, now: datetime) -> dict[str, float]:
        """Minutes per presence state since midnight (refreshed at most once a minute)."""
        cached = self._presence_cache.get(user)
        stamp = time.monotonic()
        if cached is None or stamp - cached[0] >= 60 or cached[1] != now.date():
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            seconds = self.store.recent_usage(user, midnight).get("presence", {})
            cached = (stamp, now.date(), {kind: round(seconds.get(kind, 0.0) / 60, 1) for kind in PRESENCE_KINDS})
            self._presence_cache[user] = cached
        return cached[2]

    # -- apps / websites blocked from Home Assistant -----------------------------------------
    def blocked_items(self, user: str) -> list[str]:
        value = self.store.get_setting(user, SETTING_BLOCKED_ITEMS, [])
        return [str(v) for v in value] if isinstance(value, list) else []

    # -- rules made or changed from Home Assistant ------------------------------------------
    def _custom_rules(self, user: str) -> dict[str, AppRule]:
        """Rules stored for ``user`` from Home Assistant, by slug (parsed once, until they change)."""
        cached = self._custom_rules_cache.get(user)
        if cached is not None:
            return cached
        stored = self.store.get_setting(user, SETTING_CUSTOM_RULES, {})
        rules: dict[str, AppRule] = {}
        for slug, definition in (stored.items() if isinstance(stored, dict) else []):
            try:
                rule = parse_rule(definition, f"user {user!r}: rules from Home Assistant")
            except ConfigError as exc:
                log.warning("ignoring a stored rule: %s", exc)
                continue
            if rule.slug == slug:
                rules[slug] = rule
        self._custom_rules_cache[user] = rules
        return rules

    def _rules_policy(self, user: str, policy: Optional[UserPolicy]) -> Optional[UserPolicy]:
        """The user's policy with the rules from Home Assistant applied: one of them replaces
        the configured rule of the same name, the others are added.  These are the rules
        that have entities; :meth:`_effective_policy` adds the block list on top."""
        if policy is None or policy.unlimited:
            return policy
        custom = self._custom_rules(user)
        if not custom:
            return policy
        cached = self._rules_policy_cache.get(user)
        if cached is not None and cached[0] is policy and cached[1] is custom:
            return cached[2]
        configured = {r.slug for r in policy.rules}
        merged = replace(policy, rules=[custom.get(r.slug, r) for r in policy.rules]
                         + [r for slug, r in custom.items() if slug not in configured])
        self._rules_policy_cache[user] = (policy, custom, merged)
        return merged

    def user_rules_policy(self, user: str) -> Optional[UserPolicy]:
        return self._rules_policy(user, self.timekeeper.policy_for(user))

    def _set_rule(self, user: str, data: dict[str, Any], pin: Any = None) -> dict[str, Any]:
        """Create a rule, or change one (also one from config.yaml), from Home Assistant.

        Only the fields that are given change.  A new rule and a change that only
        restricts are free; anything that allows more needs the parent PIN."""
        base = self.timekeeper.policy_for(user)
        if base is None or base.unlimited:
            return {"ok": False, "error": f"{user} is not a managed user (rules only apply to those)"}
        name = str(data.get("rule") or data.get("name") or "").strip()
        if not name or len(name) > 80:
            return {"ok": False, "error": "rule (its name) is required"}
        slug = slugify(name)
        policy = self._rules_policy(user, base)
        existing = next((r for r in policy.rules if r.slug == slug), None)
        definition = dict(existing.definition) if existing else {}
        # "action" is the command's own name here, so the rule's action travels as "rule_action"
        definition.update({key: data[key] for key in RULE_FIELDS if key in data and key != "action"})
        if "rule_action" in data:
            definition["action"] = data["rule_action"]
        definition["name"] = existing.name if existing else name
        try:
            rule = parse_rule(definition, "rule")   # (ConfigError also for a yes/no field that is neither)
            # the user's rules as they would be - configured and from HA - must not give two
            # rules the same entity id
            check_rule_ids([r for r in policy.rules if r.slug != slug] + [rule], "rule")
        except (ConfigError, ValueError, TypeError, AttributeError) as exc:
            return {"ok": False, "error": str(exc)}
        custom = self._custom_rules(user)
        if existing is None and len(custom) >= MAX_CUSTOM_RULES:
            return {"ok": False, "error": f"too many rules from Home Assistant ({MAX_CUSTOM_RULES})"}
        if existing is not None and rule_loosens(existing, rule) and (ref := self._gate("grant", "rule_set", user, pin)):
            return ref
        self._store_custom_rules(user, {**{s: r.definition for s, r in custom.items()}, slug: rule.definition})
        self.store.log_event(user, "rule_set", f"{rule.name}: {json.dumps(rule.definition, sort_keys=True)[:400]}")
        return {"ok": True, "rule": slug, "created": existing is None, "definition": rule.definition}

    def _remove_rule(self, user: str, name: Any, pin: Any = None) -> dict[str, Any]:
        """Drop a rule made in Home Assistant, or the changes made there to a configured rule."""
        base = self.timekeeper.policy_for(user)
        slug = slugify(name or "")
        custom = self._custom_rules(user)
        if base is None or slug not in custom:
            if base is not None and any(r.slug == slug for r in base.rules):
                return {"ok": False, "error": f"{name} comes from config.yaml and was not changed here; switch it off instead"}
            return {"ok": False, "error": f"unknown rule {name}"}
        configured = next((r for r in base.rules if r.slug == slug), None)
        # without the rule (or with the configured one back) the user may be allowed more
        if (configured is None or rule_loosens(custom[slug], configured)) and (ref := self._gate("grant", "rule_remove", user, pin)):
            return ref
        self._store_custom_rules(user, {s: r.definition for s, r in custom.items() if s != slug})
        if configured is None:  # nothing is left of it: a rule of that name made later starts afresh
            for key in (f"rule_enabled:{slug}", f"rule_weekly_limit:{slug}"):
                self.store.delete_setting(user, key)
        self.store.log_event(user, "rule_remove", slug)
        return {"ok": True, "rule": slug, "restored": configured is not None}

    def _store_custom_rules(self, user: str, definitions: dict[str, Any]) -> None:
        if definitions:
            self.store.set_setting(user, SETTING_CUSTOM_RULES, definitions)
        else:
            self.store.delete_setting(user, SETTING_CUSTOM_RULES)
        self._custom_rules_cache.pop(user, None)
        self._rules_policy_cache.pop(user, None)
        self._policy_cache.pop(user, None)
        self._emit_user_event(user, "rules_changed")

    def _effective_policy(self, user: str, policy: Optional[UserPolicy]) -> Optional[UserPolicy]:
        """The user's policy with the rules from Home Assistant, plus one always-blocked rule
        per item blocked from Home Assistant."""
        policy = self._rules_policy(user, policy)
        if policy is None:
            return None
        items = tuple(self.blocked_items(user))
        if not items:
            return policy
        cached = self._policy_cache.get(user)
        if cached is not None and cached[0] is policy and cached[1] == items:
            return cached[2]
        extra = [AppRule(name=f"Blocked {item}", apps=[item] if _is_app_item(item) else [],
                         sites=[] if _is_app_item(item) else [item], blocked=True, warn_at_minutes=[],
                         message=f"{item} is blocked.") for item in items]
        merged = replace(policy, rules=list(policy.rules) + extra)
        self._policy_cache[user] = (policy, items, merged)
        return merged

    def _change_blocked(self, user: str, item: Any, add: bool, pin: Any = None) -> dict[str, Any]:
        """Add or remove a blocked app / website.  Blocking is free; lifting a block needs the parent PIN."""
        if not self.timekeeper.is_managed(user):
            return {"ok": False, "error": f"{user} is not a managed user (apps and websites are only blocked for those)"}
        items = self.blocked_items(user)
        if not add and str(item or "").strip().lower() in ("*", "all"):
            wanted: Optional[str] = None
        else:
            wanted = normalize_block_item(item)
            if wanted is None:
                return {"ok": False, "error": "give a program name (game.exe) or a website (example.com)"}
        if add:
            if _is_app_item(wanted) and not closable(wanted):
                return {"ok": False, "error": f"{wanted} is part of Windows or of PC Link and cannot be blocked"}
            if wanted in items:
                return {"ok": True, "items": items}
            if len(items) >= MAX_BLOCKED_ITEMS:
                return {"ok": False, "error": f"the block list is full ({MAX_BLOCKED_ITEMS} entries)"}
            items = sorted(items + [wanted])
        else:
            if wanted is not None and wanted not in items:
                return {"ok": False, "error": f"{wanted} is not on the block list"}
            if (ref := self._gate("disable", "unblock_item", user, pin)):
                return ref
            items = [] if wanted is None else [i for i in items if i != wanted]
        if items:
            self.store.set_setting(user, SETTING_BLOCKED_ITEMS, items)
        else:
            self.store.delete_setting(user, SETTING_BLOCKED_ITEMS)
        self.store.log_event(user, "block_item" if add else "unblock_item", wanted or "all")
        self._emit_user_event(user, "blocklist_changed", item=wanted or "all", blocked=add)
        return {"ok": True, "items": items}

    def _recent_usage_cached(self, user: str, now: datetime) -> dict[str, Any]:
        """The report for the state payload, refreshed at most once a minute (it is a database query)."""
        cached = self._report_cache.get(user)
        stamp = time.monotonic()
        if cached is None or stamp - cached[0] >= 60 or cached[1].get("hours") != float(self.cfg.activity.report_hours):
            cached = (stamp, self.usage_report(user, now=now, limit=self.cfg.activity.top_n * 2))
            self._report_cache[user] = cached
        return cached[1]

    def _week_used(self, user: str, today: date) -> float:
        return self.store.usage_range(user, self.timekeeper.week_start(today), today).active

    def pc_payload(self, sessions: list[SessionInfo]) -> dict[str, Any]:
        snap: dict[str, Any] = {}
        if self.telemetry is not None:
            try:
                snap = self.telemetry.snapshot()
            except Exception:  # noqa: BLE001
                log.exception("telemetry snapshot failed")
        readings = self.hwsensors.latest() if self.hwsensors is not None else []
        for r in readings:
            snap[f"hw_{r.key}"] = r.value
        snap["hw_sensor_count"] = len(readings)
        if self.devices is not None:
            try:
                snap.update(self.devices.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("device snapshot failed")
        if self.winupdate is not None:
            try:
                snap.update(self.winupdate.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("windows update snapshot failed")
        if self.updater is not None:
            try:
                snap.update(self.updater.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("self-update snapshot failed")
        active = [s for s in sessions if s.state == "active"]
        snap["device_name"] = self.cfg.device.name
        snap["active_sessions"] = len(active)
        snap["sessions"] = [s.to_dict() for s in sessions]
        users = sorted({s.user for s in sessions if s.logged_in})
        snap["logged_in_users"] = ", ".join(users) if users else "none"
        snap["any_user_active"] = any(not s.locked for s in active)
        snap["anyone_present"] = any(s.presence in ("active", "passive") for s in active)
        snap["time_limits_enabled"] = self.timekeeper.global_enabled
        # tells the Home Assistant integration to pass this PC the usage of the other PCs
        snap["shared_budget"] = bool(self.cfg.time_limits.shared_budget and self.fleet is not None)
        snap["parent_present"] = self.timekeeper.parent_present
        snap["parent_pin_enabled"] = self.pin.enabled
        snap["parent_unlocked"] = self.pin.unlocked()
        snap["parent_pin_state"] = ""
        snap["pin_last_refusal"] = self._pin_notice
        snap["service_status"] = "running"
        snap["service_info"] = {
            "version": __version__,
            "python": sys.version.split()[0],
            "started": datetime.fromtimestamp(self._started_at).isoformat(timespec="seconds"),
            "ticks": self._stats["ticks"],
            "events": self._stats["events"],
            "agents_connected": sorted(self.agent_server.connected_sessions()) if self.agent_server else [],
            "hw_backend": getattr(getattr(self.hwsensors, "backend", None), "name", None),
            "gpu_backend": getattr(getattr(self.telemetry, "gpu", None), "name", None),
            "config": self.cfg.source_path,
            "block_method": self.cfg.time_limits.block_method,
            # the zone the service keeps to: configured, else pinned at the first run (_pin_zone)
            "time_zone": self.cfg.time_limits.time_zone
                         or (f"{self._pinned_zone} (pinned)" if self._own_clock and self._pinned_zone else "local"),
            "managed_users": self.timekeeper.managed_users(),
            "device_name": self.cfg.device.name,
            "restart_needed": self._restart_needed,
            "api": (f"{'https' if self.cfg.api.tls_cert and self.cfg.api.tls_key else 'http'}://"
                    f"{self.cfg.api.host}:{self.cfg.api.port}/") if self.api is not None else None,
            "pin": self.pin.status(),
        }
        # health of the service itself (see _finish_tick) and the version of the entity catalog
        health = self.health()
        snap["degraded"] = health["degraded"]
        snap["tick_failures"] = health["tick_failures"]
        snap["last_good_tick"] = health["last_good_tick"]
        snap["health"] = health
        snap["config_problem"] = health["config_problem"]
        try:
            snap["catalog_hash"] = self._catalog(snap)[2]
        except Exception:  # noqa: BLE001 - the state is worth more than the hint to refresh
            log.debug("could not work out the catalog hash", exc_info=True)
            snap["catalog_hash"] = None
        self._last_pc_snapshot = snap
        self._last_pc_snapshot_at = self._mono()
        return snap

    def _fresh_pc_snapshot(self) -> dict[str, Any]:
        """The PC payload, rebuilt when the cached one is older than the telemetry interval."""
        max_age = max(0.0, float(self.cfg.telemetry.interval_seconds))
        if self._last_pc_snapshot is None or self._mono() - self._last_pc_snapshot_at >= max_age:
            return self.pc_payload(self._last_sessions)
        return self._last_pc_snapshot

    def _catalog(self, snap: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
        """(users, entries, hash) of the entity catalog that goes with the PC payload ``snap``."""
        readings = self.hwsensors.latest() if self.hwsensors is not None else []
        ents: list[Entity] = list(self.entities.pc_entities(snap)) + list(self.entities.hw_entities(readings))
        users: dict[str, dict[str, Any]] = {}
        for user in self._visible_users({s.user for s in self._last_sessions}):
            managed = self.timekeeper.is_managed(user)
            policy = self.user_rules_policy(user)
            users[user] = {"name": policy.label if policy else user, "managed": managed}
            ents += self.entities.user_entities(user, policy, managed, self.timekeeper.profile_names(user))
        entries = [entry for entry in (e.catalog_entry() for e in ents) if entry]
        return users, entries, catalog_hash(entries)

    def _visible_users(self, logged_in: set[str]) -> list[str]:
        """Users that are reported to Home Assistant (the same set in the catalog and in state)."""
        return [u for u in sorted(self._users)
                if self.timekeeper.is_managed(u) or self.cfg.activity.track_unmanaged_users or u in logged_in]

    def _api_push(self, now: datetime, by_user: dict[str, list[SessionInfo]], mono: float) -> None:
        if self.api is None or self.api.client_count == 0:
            return
        if mono - self._last_api_push < self.cfg.api.push_interval_seconds:
            return
        self._last_api_push = mono
        users: dict[str, dict[str, Any]] = {}
        for user in self._visible_users(set(by_user)):   # one user whose payload fails costs the others nothing
            payload = self._step(f"payload {user}", self._payload, user, now, by_user.get(user, []))
            if payload is not None:
                users[user] = payload
        self.api.push({"type": "state", "pc": self._fresh_pc_snapshot(), "users": users})

    # ------------------------------------------------------------------
    # commands from Home Assistant / the API
    # ------------------------------------------------------------------
    def _drain_commands(self) -> None:
        for _ in range(MAX_QUEUED_COMMANDS):   # what arrives meanwhile waits for the next tick
            try:
                object_id, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if object_id == "_agent_message":
                    self._handle_agent_message(*payload)
                elif object_id == "_bad_report":
                    self._bad_report(*payload)
                else:
                    self.dispatch(object_id, payload)
            except Exception:  # noqa: BLE001
                log.exception("command %s failed", _clean(object_id, 60))

    def _bad_report(self, session_id: int, why: str) -> None:
        """An agent sent a report that is not one (see ``AgentReport.from_dict``): the real agent
        never does, so somebody is talking to the service in its place."""
        user = next((s.user for s in self._last_sessions if s.session_id == session_id), None)
        log.warning("report from session %s rejected: %s", session_id, _clean(why))
        if user is not None:
            self._flag_tamper(user, "report_rejected", f"session {session_id}: {why}")

    def _refuse(self, user: Optional[str], action: str, reason: str) -> dict[str, Any]:
        notice = {"ts": time.time(), "user": user, "action": action, "reason": reason}
        self._pin_notice = notice
        if user and user in self._users:
            self._users[user].last_refusal = notice
        log.warning("refused %s for %s: %s", action, user or "-", reason)
        self.store.log_event(user, "refused", f"{action}: {reason}")
        return {"ok": False, "error": reason}

    def _gate(self, kind: str, action: str, user: Optional[str] = None, pin: Any = None) -> Optional[dict[str, Any]]:
        """Return a refusal dict when the parent PIN is required and missing."""
        allowed, reason = self.pin.authorize(kind, pin)
        if allowed:
            return None
        return self._refuse(user, action, reason)

    def dispatch(self, object_id: str, payload: Any, pin: Any = None) -> dict[str, Any]:
        """Run an entity command: ``object_id`` is the entity key (``user_kevin_blocked``),
        ``payload`` the value HA would publish for it (``ON`` / ``OFF`` / ``PRESS`` / a number / text)."""
        if object_id == "cmd":
            return self._dispatch_json(payload if isinstance(payload, dict) else {})
        if isinstance(payload, bool):
            text = "ON" if payload else "OFF"
        else:
            text = payload.strip() if isinstance(payload, str) else str(payload)
        log.info("HA command %s = %r", object_id, "***" if object_id == "parent_pin" else text[:100])
        now = self.clock()
        if object_id in ("shutdown", "reboot", "sleep", "hibernate", "cancel_shutdown"):
            if not self._power_action(object_id):
                return {"ok": False, "error": f"{object_id} is disabled in the configuration"}
        elif object_id == "lock_all":
            for s in self._last_sessions:
                if s.state == "active":
                    self._lock_session(s.session_id)
        elif object_id == "time_limits_enabled":
            on = text.upper() == "ON"
            if not on and (ref := self._gate("disable", "time_limits_off", None, pin)):
                return ref
            self.timekeeper.set_global_enabled(on)
        elif object_id == "broadcast_message":
            if text:
                self.notify_sessions([s.session_id for s in self._last_sessions if s.state == "active"], "Message", text, "info")
        elif object_id == "parent_pin":
            ok = self.pin.try_unlock(text)
            self._push_soon()
            if not ok:
                return {"ok": False, "error": "wrong pin"}
        elif object_id == "parent_present":
            present = text.upper() == "ON"
            if present and (ref := self._gate("parent_present", "parent_present", None, pin)):
                return ref
            self.timekeeper.set_parent_present(present)
            self._push_soon()
        elif object_id == "reload_config":
            self.reload_config()
        elif object_id == "check_updates":
            if self.winupdate is not None:
                self.winupdate.check_now()
        elif object_id == "install_updates":
            if self.winupdate is not None:
                self.winupdate.install()
        elif object_id == "pclink_update_check":
            if self.updater is None:
                return {"ok": False, "error": "update checks are switched off (updates.enabled)"}
            self.updater.check_now()
        elif object_id == "pclink_update_install":
            return self._install_self_update()
        elif object_id.startswith("launch_"):
            self._launch_app(object_id[7:])
        elif object_id.startswith("close_"):
            self._close_app(object_id[6:])
        elif object_id.startswith("cmd_"):
            self._run_custom(object_id[4:])
        elif object_id.startswith("user_"):
            return self._dispatch_user(object_id[5:], text, now, pin=pin)
        else:
            log.warning("unknown command entity %s", object_id)
            return {"ok": False, "error": f"unknown command {object_id}"}
        return {"ok": True}

    @staticmethod
    def _focus_minutes(value: Any) -> Optional[int]:
        """How long focus mode is to last: None (the user's default) or 1..MAX_FOCUS_MINUTES.
        Anything else is refused - a stored end that is no date would break every later tick."""
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("minutes must be a number")
        minutes = float(value)
        if not math.isfinite(minutes) or not 1 <= minutes <= MAX_FOCUS_MINUTES:
            raise ValueError(f"minutes must be between 1 and {MAX_FOCUS_MINUTES}")
        return int(minutes)

    def _focus_gate(self, user: str, policy: Optional[UserPolicy], pin: Any) -> Optional[dict[str, Any]]:
        """Focus mode that pauses the time budget gives the user more time: that needs the
        parent PIN (kind ``focus``).  Focus that counts towards the limit only restricts."""
        if policy is None or policy.unlimited or policy.focus.counts_toward_limit:
            return None
        return self._gate("focus", "focus_on", user, pin)

    def _push_soon(self) -> None:
        """Something every payload shows changed: send the API clients the state on the next tick."""
        self._last_api_push = NEVER

    def _dispatch_user(self, rest: str, text: str, now: datetime, pin: Any = None) -> dict[str, Any]:
        user = sub = None
        for slug in sorted(self.slugs.slugs(), key=len, reverse=True):
            if rest.startswith(slug + "_"):
                user, sub = self.slugs.user(slug), rest[len(slug) + 1:]
                break
        if user is None or sub is None or user not in self._users:
            log.warning("command for unknown user: %s", _clean(rest, 80))
            return {"ok": False, "error": "unknown user"}
        today = now.date()
        tk = self.timekeeper
        policy = tk.policy_for(user)
        sessions = [s.session_id for s in self._last_sessions if s.user == user and s.logged_in]
        active = [s.session_id for s in self._last_sessions if s.user == user and s.state == "active"]
        on = text.upper() == "ON"
        result: dict[str, Any] = {"ok": True}

        def number() -> float:
            return float(text)

        if sub == "limit_today":
            minutes = max(0, int(number()))
            if tk.is_loosening_limit(user, "today", minutes, today) and (ref := self._gate("grant", "limit_today", user, pin)):
                return ref
            tk.set_limit_today(user, today, minutes)
        elif sub in ("weekday_limit", "weekend_limit"):
            minutes = max(0, int(number()))
            scope = sub[:-len("_limit")]
            if tk.is_loosening_limit(user, scope, minutes, today) and (ref := self._gate("grant", sub, user, pin)):
                return ref
            (tk.set_weekday_limit if scope == "weekday" else tk.set_weekend_limit)(user, minutes)
        elif sub == "weekly_limit":
            # 0 (the entity's minimum) or less is stored as such and means "no weekly limit";
            # dropping the override instead would bring the configured limit back
            weekly = max(0, int(number()))
            if tk.is_loosening_limit(user, "weekly", weekly, today) and (ref := self._gate("grant", "weekly_limit", user, pin)):
                return ref
            tk.set_weekly_limit(user, weekly)
        elif sub == "bonus_today":
            minutes = number()
            # the number shows and sets what this PC granted (other PCs' bonus is theirs to change)
            if minutes > tk.local_bonus_seconds(user, today) / 60 and (ref := self._gate("grant", "bonus_today", user, pin)):
                return ref
            tk.set_bonus_minutes(user, today, minutes)
        elif sub in ("add_15", "add_30", "add_minutes"):
            minutes = 15 if sub == "add_15" else 30 if sub == "add_30" else number()
            if minutes <= 0:
                return {"ok": True}
            if (ref := self._gate("grant", sub, user, pin)):
                return ref
            tk.add_bonus_minutes(user, today, minutes)
        elif sub.startswith("reward_"):
            if policy is None:
                return {"ok": False, "error": "no policy"}
            reward = next((r for r in policy.rewards if r.slug == sub[7:]), None)
            if reward is None:
                return {"ok": False, "error": f"unknown reward {sub[7:]}"}
            if (ref := self._gate("grant", "reward", user, pin)):
                return ref
            ok, why = tk.claim_reward(user, reward, today)
            if ok:
                self.notify_sessions(active, "Bonus time", f"{reward.name}: +{reward.minutes} minutes!", "info")
                self._emit_user_event(user, "reward_claimed", reward=reward.name, minutes=reward.minutes)
            result = {"ok": ok, "error": why or None}
        elif sub == "limit_enabled":
            if not on and (ref := self._gate("disable", "limit_enabled_off", user, pin)):
                return ref
            tk.set_limit_enabled(user, on)
        elif sub == "blocked":
            if not on and (ref := self._gate("unblock", "unblock", user, pin)):
                return ref
            tk.set_manually_blocked(user, on)
        elif sub == "bedtime_extension":
            minutes = max(0, int(number()))
            night = tk.bedtime_day(user, now)
            if minutes > tk.local_bedtime_extension_minutes(user, night) and (ref := self._gate("bedtime", "bedtime_extension", user, pin)):
                return ref
            tk.set_bedtime_extension(user, night, minutes)
        elif sub == "extend_bedtime_30":
            if (ref := self._gate("bedtime", "extend_bedtime", user, pin)):
                return ref
            tk.extend_bedtime(user, tk.bedtime_day(user, now), 30, now)   # from now when bedtime has passed
        elif sub == "focus_mode":
            if on:
                if (ref := self._focus_gate(user, policy, pin)):
                    return ref
                stored = self.store.get_setting(user, "focus_minutes", 0)
                minutes = min(int(stored), MAX_FOCUS_MINUTES) if isinstance(stored, (int, float)) and 1 <= stored < math.inf else None
                tk.start_focus(user, now, minutes)
                self.notify_sessions(active, "Focus mode", policy.focus.message if policy else "Focus mode is on.", "info")
            else:
                if (ref := self._gate("focus_off", "focus_off", user, pin)):
                    return ref
                tk.stop_focus(user, now)
        elif sub == "focus_minutes":
            self.store.set_setting(user, "focus_minutes", self._focus_minutes(number()))
        elif sub == "breaks_enabled":
            tk.set_breaks_enabled(user, on)
        elif sub.startswith("rule_") and policy is not None:
            return self._dispatch_rule(user, self._rules_policy(user, policy), sub[5:], text, today, pin)
        elif sub == "display":
            result = self._display_power(active, on)
        elif sub == "volume":
            self._send_agent(active, {"type": "volume", "level": int(number())})
        elif sub == "muted":
            self._send_agent(active, {"type": "mute", "muted": on})
        elif sub == "screenshot":
            result = self.request_screenshot(user, pin=pin)
        elif sub == "logoff":
            for sid in sessions:
                self._logoff_session(sid)
        elif sub == "lock":
            for sid in sessions:
                self._lock_session(sid)
        elif sub == "message":
            if text:
                self.notify_sessions(sessions, "Message", text, "info")
        elif sub in ("block_item", "unblock_item"):
            if not text:
                return {"ok": True}
            result = self._change_blocked(user, text, add=sub == "block_item", pin=pin)
        elif sub == "clear_tamper":
            result = self._clear_tamper(user, pin)
        elif sub in ("approve_time_request", "deny_time_request"):
            result = self._answer_time_request(user, sub == "approve_time_request", pin=pin)
        elif sub == "profile":
            result = self._set_profile(user, text, pin)
        else:
            log.warning("unknown user command %s for %s", _clean(sub, 60), user)
            return {"ok": False, "error": f"unknown user command {sub}"}
        return result

    def _dispatch_rule(self, user: str, policy: UserPolicy, rest: str, text: str, today: date, pin: Any) -> dict[str, Any]:
        for rule in sorted(policy.rules, key=lambda r: len(r.slug), reverse=True):
            if rest == rule.slug + "_enabled":
                on = text.upper() == "ON"
                if not on and (ref := self._gate("disable", "rule_off", user, pin)):
                    return ref
                self.rules.set_rule_enabled(user, rule.slug, on)
                break
            if rest == rule.slug + "_limit_today":
                minutes = int(float(text))
                # the entity's maximum (and anything negative) means "no limit today", which is
                # stored as NO_LIMIT: dropping the override would bring the configured limit back
                limit = NO_LIMIT if (minutes >= 1440 or minutes < 0) else minutes
                if self.rules.is_loosening_rule_limit(user, rule, limit, today) and (ref := self._gate("grant", "rule_limit", user, pin)):
                    return ref
                self.rules.set_rule_limit_today(user, rule.slug, today, limit)
                break
            if rest == rule.slug + "_weekly_limit":
                minutes = int(float(text))   # 0 (the entity's minimum) or less: no weekly limit
                if self.rules.is_loosening_rule_weekly_limit(user, rule, minutes) and (ref := self._gate("grant", "rule_weekly_limit", user, pin)):
                    return ref
                self.rules.set_rule_weekly_limit(user, rule.slug, minutes)
                break
        else:
            return {"ok": False, "error": f"unknown rule command {rest}"}
        return {"ok": True}

    def _dispatch_json(self, data: dict[str, Any]) -> dict[str, Any]:
        action = str(data.get("action", "")).lower()
        user = str(data.get("user") or "").strip().lower() or None
        if user is None and data.get("user") is not None:
            # an empty name (a template that rendered to nothing) must not mean "everyone"
            return {"ok": False, "error": "user is empty"}
        pin = data.get("pin")
        now = self.clock()
        today = now.date()
        if action == "peer_usage":
            # Home Assistant repeats this every half minute: handled here, and kept out of the log
            if self.fleet is None:
                return {"ok": False, "error": "fleet.enabled is false"}
            if not isinstance(data.get("peers"), dict):
                return {"ok": False, "error": "peers required"}
            log.debug("usage of %d other PC(s) from Home Assistant", len(data["peers"]))
            return {"ok": True, "peers": self.fleet.update_peers(data["peers"]),
                    "shared_budget": bool(self.cfg.time_limits.shared_budget)}
        log.info("JSON command: %s", _redact(data))
        sessions = [s.session_id for s in self._last_sessions if (user is None or s.user == user) and s.logged_in]
        active = [s.session_id for s in self._last_sessions if (user is None or s.user == user) and s.state == "active"]
        if "session" in data:
            sessions = active = [int(data["session"])]
        tk = self.timekeeper
        result: dict[str, Any] = {"ok": True}
        # A name in a command is no reason to start managing somebody (manage_unknown_users):
        # only users we have met - configured, seen logged in, or with something stored - count.
        known = bool(user) and (user in self._users or self.cfg.user_policy(user) is not None
                                or (valid_account_name(user) and bool(self.store.settings_for(user))))
        policy = self._rules_policy(user, tk.policy_for(user, create=known)) if user else None

        def need_user() -> Optional[dict[str, Any]]:
            if not user:
                return {"ok": False, "error": "user required"}
            if not known:
                return {"ok": False, "error": f"unknown user {_clean(user, 40)}"}
            return None

        if action in ("logoff", "lock", "notify") and user and not sessions:
            return {"ok": False, "error": f"{user} has no session on this PC"}
        if action in ("shutdown", "reboot", "sleep", "hibernate", "cancel_shutdown"):
            if not self._power_action(action, delay=data.get("delay"), message=data.get("message")):
                return {"ok": False, "error": f"{action} is disabled in the configuration"}
        elif action == "logoff":
            for sid in sessions:
                self._logoff_session(sid)
        elif action == "lock":
            for sid in sessions:
                self._lock_session(sid)
        elif action == "notify":
            self.notify_sessions(sessions, str(data.get("title", "Message")), str(data.get("message", "")), str(data.get("level", "info")))
        elif action == "wol":
            if not data.get("mac"):
                return {"ok": False, "error": "mac required"}
            send_wol(str(data["mac"]), str(data.get("broadcast", self.cfg.power.wol_broadcast)), int(data.get("port", self.cfg.power.wol_port)))
        elif action in ("add_minutes", "earn"):
            if (err := need_user()):
                return err
            minutes = float(data.get("minutes", 15))
            if minutes > 0 and (ref := self._gate("grant", action, user, pin)):
                return ref
            if action == "earn":
                tk.earn_minutes(user, today, minutes, reason=str(data.get("reason", "")))
                self._emit_user_event(user, "reward_claimed", reward=data.get("reason"), minutes=minutes)
            else:
                tk.add_bonus_minutes(user, today, minutes)
        elif action == "reward":
            if (err := need_user()) or policy is None:
                return err or {"ok": False, "error": "no policy"}
            slug = slugify(data.get("name", ""))
            reward = next((r for r in policy.rewards if r.slug == slug), None)
            if reward is None:
                return {"ok": False, "error": f"unknown reward {data.get('name')}"}
            if (ref := self._gate("grant", "reward", user, pin)):
                return ref
            ok, why = tk.claim_reward(user, reward, today)
            if ok:
                self.notify_sessions(active, "Bonus time", f"{reward.name}: +{reward.minutes} minutes!", "info")
                self._emit_user_event(user, "reward_claimed", reward=reward.name, minutes=reward.minutes)
            result = {"ok": ok, "error": why or None}
        elif action == "set_bonus":
            if (err := need_user()):
                return err
            minutes = float(data.get("minutes", 0))
            if minutes > tk.local_bonus_seconds(user, today) / 60 and (ref := self._gate("grant", action, user, pin)):
                return ref
            tk.set_bonus_minutes(user, today, minutes)
        elif action == "set_limit":
            if (err := need_user()):
                return err
            minutes = data.get("minutes")
            minutes = None if minutes is None else int(minutes)
            scope = str(data.get("scope", "today"))
            if scope not in ("today", "weekday", "weekend", "weekly"):
                return {"ok": False, "error": f"unknown scope {scope}"}
            # No minutes: drop the override, the configured limit applies again.  "No limit" is a
            # value of its own - 0 for the week (stored as such), a whole day for the others -
            # because dropping the override would bring the configured limit back instead.
            if minutes is not None and scope == "weekly":
                minutes = max(0, minutes)
            elif minutes is not None and minutes < 0:
                minutes = NO_LIMIT
            if tk.is_loosening_limit(user, scope, minutes, today) and (ref := self._gate("grant", action, user, pin)):
                return ref
            if scope == "today":
                tk.set_limit_today(user, today, minutes)
            elif scope == "weekday":
                tk.set_weekday_limit(user, minutes)
            elif scope == "weekend":
                tk.set_weekend_limit(user, minutes)
            else:
                tk.set_weekly_limit(user, minutes)
        elif action == "clear_overrides":
            if (err := need_user()):
                return err
            if (ref := self._gate("grant", action, user, pin)):
                return ref
            tk.set_limit_today(user, today, None)
            tk.set_weekday_limit(user, None)
            tk.set_weekend_limit(user, None)
            tk.set_weekly_limit(user, None)
            tk.set_bonus_minutes(user, today, 0)
            tk.set_bedtime_extension(user, tk.bedtime_day(user, now), 0)
        elif action in ("block", "unblock"):
            if (err := need_user()):
                return err
            if action == "unblock" and (ref := self._gate("unblock", action, user, pin)):
                return ref
            tk.set_manually_blocked(user, action == "block")
        elif action == "limit_enabled":
            if (err := need_user()):
                return err
            enabled = bool(data.get("enabled", True))
            if not enabled and (ref := self._gate("disable", action, user, pin)):
                return ref
            tk.set_limit_enabled(user, enabled)
        elif action == "limits":
            enabled = bool(data.get("enabled", True))
            if not enabled and (ref := self._gate("disable", "time_limits_off", None, pin)):
                return ref
            tk.set_global_enabled(enabled)
        elif action == "focus":
            if (err := need_user()):
                return err
            allow = data.get("allow")
            allow = [str(a) for a in allow if str(a).strip()] if isinstance(allow, list) else None
            # an extra allow-list decides what may run while the time limit is paused: that is a grant
            if allow and (ref := self._gate("grant", "focus_allow", user, pin)):
                return ref
            minutes = self._focus_minutes(data.get("minutes"))
            if (ref := self._focus_gate(user, tk.policy_for(user, create=False), pin)):
                return ref
            until = tk.start_focus(user, now, minutes, allow or None)
            self.notify_sessions(active, "Focus mode", policy.focus.message if policy else "Focus mode is on.", "info")
            result = {"ok": True, "until": until}
        elif action == "focus_off":
            if (err := need_user()):
                return err
            if (ref := self._gate("focus_off", action, user, pin)):
                return ref
            tk.stop_focus(user, now)
        elif action in ("extend_bedtime", "bedtime_extension"):
            if (err := need_user()):
                return err
            if (ref := self._gate("bedtime", action, user, pin)):
                return ref
            minutes = max(0, int(data.get("minutes", 30)))
            night = tk.bedtime_day(user, now)
            if action == "extend_bedtime":
                tk.extend_bedtime(user, night, minutes, now)
            else:
                tk.set_bedtime_extension(user, night, minutes)
        elif action == "rule_enabled":
            if (err := need_user()) or policy is None:
                return err or {"ok": False, "error": "no policy"}
            slug = slugify(data.get("rule", ""))
            if slug not in {r.slug for r in policy.rules}:
                return {"ok": False, "error": f"unknown rule {data.get('rule')}"}
            enabled = bool(data.get("enabled", True))
            if not enabled and (ref := self._gate("disable", action, user, pin)):
                return ref
            self.rules.set_rule_enabled(user, slug, enabled)
        elif action == "rule_limit":
            if (err := need_user()) or policy is None:
                return err or {"ok": False, "error": "no policy"}
            slug = slugify(data.get("rule", ""))
            rule = next((r for r in policy.rules if r.slug == slug), None)
            if rule is None:
                return {"ok": False, "error": f"unknown rule {data.get('rule')}"}
            minutes = data.get("minutes")
            minutes = None if minutes is None else int(minutes)
            scope = str(data.get("scope", "today"))
            if scope not in ("today", "weekly"):
                return {"ok": False, "error": f"unknown scope {scope}"}
            if scope == "weekly":
                # no minutes: back to the configured weekly budget; 0 or less: no weekly budget
                configured = rule.weekly_limit_minutes if minutes is None else minutes
                if self.rules.is_loosening_rule_weekly_limit(user, rule, configured) and (ref := self._gate("grant", action, user, pin)):
                    return ref
                self.rules.set_rule_weekly_limit(user, slug, minutes)
            else:
                if minutes is not None and minutes < 0:
                    minutes = NO_LIMIT  # stored: "no limit today" (None drops the override: the configured limit again)
                if self.rules.is_loosening_rule_limit(user, rule, minutes, today) and (ref := self._gate("grant", action, user, pin)):
                    return ref
                self.rules.set_rule_limit_today(user, slug, today, minutes)
        elif action in ("rule_set", "set_rule"):
            if (err := need_user()):
                return err
            result = self._set_rule(user, data, pin)
        elif action in ("rule_remove", "remove_rule"):
            if (err := need_user()):
                return err
            result = self._remove_rule(user, data.get("rule", data.get("name")), pin)
        elif action == "display":
            result = self._display_power(active, bool(data.get("on", False)))
        elif action == "breaks":
            if (err := need_user()):
                return err
            tk.set_breaks_enabled(user, bool(data.get("enabled", True)))
        elif action == "volume":
            self._send_agent(active, {"type": "volume", "level": int(data.get("level", 50))})
        elif action == "mute":
            self._send_agent(active, {"type": "mute", "muted": bool(data.get("muted", True))})
        elif action == "screenshot":
            if (err := need_user()):
                return err
            result = self.request_screenshot(user, pin=pin)
        elif action == "launch":
            result = self._launch_app(slugify(data.get("name", "")), command=data.get("command"), user=user)
        elif action == "close_app":
            result = self._close_app(slugify(data.get("name", "")), process=data.get("process"), user=user)
        elif action == "parent_present":
            present = bool(data.get("present", True))
            if present and (ref := self._gate("parent_present", action, None, pin)):
                return ref
            tk.set_parent_present(present)
            self._push_soon()
        elif action == "pin":
            ok = self.pin.try_unlock(data.get("pin", data.get("value")))
            self._push_soon()
            result = {"ok": ok, "error": None if ok else "wrong pin"}
        elif action == "pin_lock":
            self.pin.lock()
            self._push_soon()
        elif action == "check_updates":
            if self.winupdate is not None:
                self.winupdate.check_now()
        elif action == "install_updates":
            ok = bool(self.winupdate is not None and self.winupdate.install())
            result = {"ok": ok, "error": None if ok else "install not allowed or unavailable"}
        elif action in ("block_item", "unblock_item"):
            if (err := need_user()):
                return err
            result = self._change_blocked(user, data.get("item", data.get("name")), add=action == "block_item", pin=pin)
        elif action == "usage_report":
            if (err := need_user()):
                return err
            hours = data.get("hours")
            result = {"ok": True, "report": self.usage_report(user, None if hours is None else float(hours), now)}
        elif action == "pclink_update_check":
            if self.updater is None:
                return {"ok": False, "error": "update checks are switched off (updates.enabled)"}
            self.updater.check_now()
        elif action == "pclink_update_install":
            result = self._install_self_update()
        elif action == "run":
            self._run_custom(slugify(data.get("name", "")))
        elif action == "reload":
            result = self.reload_config()
        elif action == "refresh":
            self._perform_resync()
        elif action == "clear_tamper":
            result = self._clear_tamper(user, pin)   # without a user: everybody's
        elif action == "time_request_answer":
            if (err := need_user()):
                return err
            result = self._answer_time_request(user, bool(data.get("approve", False)), data.get("minutes"), pin)
        elif action == "set_profile":
            if user and (err := need_user()):
                return err
            result = self._set_profile(user, data.get("profile"), pin)   # without a user: everybody who has it
        elif action == "set":
            # entity command from the native integration: {"action":"set","entity":"user_kevin_blocked","value":"ON"}
            entity = str(data.get("entity", "") or "")
            if not entity or entity == "cmd":
                return {"ok": False, "error": "entity required"}
            result = self.dispatch(entity, data.get("value", ""), pin=pin)
        else:
            log.warning("unknown JSON command: %s", _redact(data))
            return {"ok": False, "error": f"unknown action {action!r}"}
        return result

    def _power_action(self, action: str, delay=None, message=None) -> bool:
        """Run a power action; False when it is switched off in the configuration."""
        p = self.cfg.power
        allowed = {"shutdown": p.allow_shutdown, "reboot": p.allow_reboot, "sleep": p.allow_sleep,
                   "hibernate": p.allow_hibernate, "cancel_shutdown": True}
        if not allowed.get(action):
            log.warning("power action %s is disabled in config", action)
            return False
        active = [s.session_id for s in self._last_sessions if s.state == "active"]
        if action in ("shutdown", "reboot"):
            d = p.shutdown_delay_seconds if delay is None else max(0, int(delay))
            what = "shut down" if action == "shutdown" else "restart"
            self.notify_sessions(active, "Home Assistant", message or f"This computer will {what} in {d} seconds.", "warning")
            self.store.flush()
            if action == "shutdown":
                self.power.shutdown(message or "Shutdown requested by Home Assistant", d)
            else:
                self.power.reboot(message or "Reboot requested by Home Assistant", d)
        elif action == "cancel_shutdown":
            self.power.cancel()
        else:
            self.store.flush()
            ok = self.power.sleep() if action == "sleep" else self.power.hibernate()
            if not ok:
                log.error("%s failed", action)
        return True

    def _display_power(self, session_ids: list[int], on: bool) -> dict[str, Any]:
        """Turn the display of these sessions (seats) off or on, through their agents."""
        if not self.cfg.agent.display_control:
            return {"ok": False, "error": "display control is switched off (agent.display_control)"}
        sent = self._send_agent(session_ids, {"type": "display", "on": bool(on)})
        if sent:
            self.store.log_event(None, "display", f"{'on' if on else 'off'} in session(s) {sorted(session_ids)}")
        return {"ok": sent > 0, "sessions": sent, "error": None if sent else "no agent session to switch the display in"}

    def _install_self_update(self) -> dict[str, Any]:
        if self.updater is None:
            return {"ok": False, "error": "update checks are switched off (updates.enabled)"}
        ok, why = self.updater.install()
        if ok:
            self.store.log_event(None, "pclink_update", "install requested")
            try:
                self.store.flush()  # the installer stops the service
            except Exception:  # noqa: BLE001
                log.exception("flush before the update failed")
            self._perform_resync()
        return {"ok": ok, "error": why or None}

    def _run_custom(self, slug: str) -> None:
        cmd: Optional[CustomCommand] = next((c for c in self.cfg.commands if c.slug == slug), None)
        if cmd is None:
            log.warning("unknown custom command %s", slug)
            return
        log.info("running custom command %s (%s)", cmd.name, cmd.run_as)
        if cmd.run_as == "user":
            if self.agent_server is None:
                log.warning("cannot run %s as user: agents disabled", cmd.name)
                return
            targets = [s.session_id for s in self._last_sessions
                       if s.state == "active" and (cmd.user is None or s.user == cmd.user.lower())]
            for sid in targets:
                self.agent_server.send(sid, {"type": "run", "command": cmd.command})
            return
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(cmd.command, shell=True, creationflags=creation)  # noqa: S602 - operator-configured command
        except Exception as exc:  # noqa: BLE001
            log.error("custom command %s failed: %s", cmd.name, exc)

    # -- launch / close apps ---------------------------------------------------------
    def _app(self, slug: str) -> Optional[AppLauncher]:
        return next((a for a in self.cfg.apps if a.slug == slug), None)

    def _launch_app(self, slug: str, command: Optional[str] = None, user: Optional[str] = None) -> dict[str, Any]:
        app = self._app(slug)
        if app is None and not command:
            return {"ok": False, "error": f"unknown app {slug}"}
        if app is not None and not app.launch and not command:
            return {"ok": False, "error": f"app {app.name} has no launch command"}
        target_user = user or (app.user if app else None)
        targets = [s.session_id for s in self._last_sessions
                   if s.state == "active" and (target_user is None or s.user == target_user)]
        sent = self._send_agent(targets, {"type": "run", "command": command or app.launch})
        log.info("launch %s in %d session(s)", app.name if app else command, sent)
        self.store.log_event(target_user, "launch", app.name if app else str(command)[:100])
        return {"ok": sent > 0, "sessions": sent, "error": None if sent else "no agent session to launch in"}

    def _close_app(self, slug: str, process: Optional[str] = None, user: Optional[str] = None) -> dict[str, Any]:
        app = self._app(slug)
        name = (process or (app.process_name if app else None) or "").lower()
        if not name:
            return {"ok": False, "error": f"unknown app {slug}"}
        if not closable(name):
            return {"ok": False, "error": f"refusing to close {name}"}
        target_user = user or (app.user if app else None)
        targets = [s.session_id for s in self._last_sessions
                   if s.state == "active" and (target_user is None or s.user == target_user)]
        sent = self._send_agent(targets, {"type": "close", "name": name, "tab": False, "reason": "Closed from Home Assistant"})
        self.store.log_event(target_user, "close_app", name)
        return {"ok": sent > 0, "sessions": sent, "error": None if sent else "no agent session"}

    # -- screenshots ------------------------------------------------------------------------
    def screenshot_allowed(self, user: str) -> bool:
        sc = self.cfg.screenshots
        if not sc.enabled:
            return False
        if sc.users:
            return user.lower() in sc.users
        return self.timekeeper.is_managed(user)

    def request_screenshot(self, user: str, pin: Any = None) -> dict[str, Any]:
        sc = self.cfg.screenshots
        if not self.screenshot_allowed(user):
            return self._refuse(user, "screenshot", "screenshots disabled for this user")
        if sc.require_pin and self.pin.enabled and (ref := self._gate("screenshot", "screenshot", user, pin)):
            return ref
        sessions = [s for s in self._last_sessions if s.user == user and s.state == "active" and not s.locked]
        if not sessions:
            return {"ok": False, "error": "user has no active unlocked session"}
        best = self._pick_session(sessions) or sessions[0]
        req_id = secrets.token_hex(6)
        message = {"type": "screenshot", "id": req_id, "max_width": sc.max_width, "format": sc.format,
                   "quality": sc.quality, "notify": sc.notify_user}
        if not self._send_agent([best.session_id], message):
            return {"ok": False, "error": "no agent connected in the user's session"}
        mono = self._mono()
        self._pending_screenshots[req_id] = {"user": user, "session": best.session_id, "at": mono}
        # drop stale requests
        for rid in [r for r, info in self._pending_screenshots.items() if mono - info["at"] > 120]:
            self._pending_screenshots.pop(rid, None)
        self.store.log_event(user, "screenshot_requested", f"session {best.session_id}")
        return {"ok": True, "id": req_id}

    def _handle_agent_message(self, session_id: int, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "screenshot":
            info = self._pending_screenshots.pop(str(message.get("id")), None)
            if info is None or info["session"] != session_id:
                log.warning("unsolicited screenshot from session %s ignored", session_id)
                return
            user = info["user"]
            if message.get("error"):
                # the text comes from the user's own process: one short line, wherever it goes
                reason = _clean(message["error"], 200)
                log.warning("screenshot for %s failed: %s", user, reason)
                view = self._ensure_user(user)
                view.last_refusal = {"ts": time.time(), "action": "screenshot", "reason": reason}
                return
            try:
                data = base64.b64decode(message.get("data", ""))
            except (ValueError, TypeError):
                log.warning("bad screenshot payload from session %s", session_id)
                return
            # everything in the reply comes from the user's own process: never trust it for file names
            fmt = str(message.get("format", self.cfg.screenshots.format)).lower()
            if fmt not in ("jpeg", "png"):
                fmt = self.cfg.screenshots.format
            meta = {"ts": datetime.now().isoformat(timespec="seconds"), "session": session_id, "format": fmt,
                    "width": _int_or_none(message.get("width")), "height": _int_or_none(message.get("height")),
                    "bytes": len(data)}
            view = self._ensure_user(user)
            view.screenshot = meta
            view.screenshot_data = data
            if self.cfg.screenshots.save_to_disk:
                self._save_screenshot(user, data, fmt)
            self.store.log_event(user, "screenshot", f"{meta['width']}x{meta['height']} {len(data)} bytes")
            self._emit_user_event(user, "screenshot_taken", bytes=len(data))
        elif kind == "closed":
            self._close_result(session_id, message)
        elif kind == "notified":
            self._notified(session_id, message)
        elif kind == "time_request":
            self._time_request(session_id, message.get("minutes"))
        elif kind == "oversized":
            # the agent sent a line the IPC refused - in practice a screenshot too large to carry
            for req_id, info in [(r, i) for r, i in self._pending_screenshots.items() if i["session"] == session_id]:
                del self._pending_screenshots[req_id]
                view = self._ensure_user(info["user"])
                view.last_refusal = {"ts": time.time(), "action": "screenshot",
                                     "reason": "the screenshot was too large to transfer (lower screenshots.max_width or quality)"}
                log.warning("screenshot for %s was too large to transfer", info["user"])
                self.store.log_event(info["user"], "screenshot_failed", "too large to transfer")
        else:
            log.debug("agent message %s from session %s ignored", _clean(kind, 40), session_id)

    def _save_screenshot(self, user: str, data: bytes, fmt: str) -> None:
        folder = os.path.join(os.path.dirname(self.cfg.storage.path), "screenshots")
        try:
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            ext = "jpg" if fmt == "jpeg" else fmt
            with open(os.path.join(folder, f"{slugify(user)}-{stamp}.{ext}"), "wb") as fh:
                fh.write(data)
            files = sorted(f for f in os.listdir(folder) if f.startswith(slugify(user) + "-"))
            for old in files[: max(0, len(files) - self.cfg.screenshots.keep)]:
                os.remove(os.path.join(folder, old))
        except OSError as exc:
            log.warning("could not save screenshot: %s", exc)

    # ------------------------------------------------------------------
    # configuration hot reload
    # ------------------------------------------------------------------
    def _stat_config(self) -> Optional[float]:
        path = self.cfg.source_path
        if not path:
            return None
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def _check_config_reload(self, now_ts: float, mono: Optional[float] = None) -> None:
        mono = self._mono() if mono is None else mono
        if not self.cfg.source_path or mono - self._config_checked < CONFIG_POLL_SECONDS:
            return
        self._config_checked = mono
        mtime = self._stat_config()
        if mtime is None or mtime == self._config_mtime:
            return
        if now_ts - mtime < 2.0:
            return  # still being written
        log.info("config file changed on disk; reloading")
        self.reload_config()

    def reload_config(self, new_cfg: Optional[Config] = None) -> dict[str, Any]:
        """Apply a new configuration without restarting.  Returns {ok, restart_needed|error}."""
        if new_cfg is None:
            if not self.cfg.source_path:
                return {"ok": False, "error": "no config file to reload"}
            try:
                new_cfg = Config.load(self.cfg.source_path)
            except Exception as exc:  # noqa: BLE001 - whatever the file contains, keep running on the old config
                log.error("config reload failed: %s", exc)
                self.store.log_event(None, "config_reload_failed", str(exc)[:300])
                self._config_mtime = self._stat_config()
                return {"ok": False, "error": str(exc)}
        old = self.cfg
        try:
            # Everything that can fail is built before anything is swapped: a configuration
            # that cannot be applied leaves the old one fully in force (and is not tried
            # again every ten seconds - only when the file changes once more).
            restart = _restart_sections(old, new_cfg)
            classifier = ActivityClassifier(new_cfg.activity)
            clock = make_clock(new_cfg.time_limits.time_zone or self._pinned_zone or self._pin_zone()) \
                if self._own_clock else self.clock
            new_agent_settings = agent_settings(new_cfg)
            json.dumps(new_agent_settings)
            probe = replace(self.entities, cfg=new_cfg, slugs=SlugRegistry())
            probe.pc_entities({})
            for policy in new_cfg.time_limits.users:
                probe.user_entities(policy.key, policy, not policy.unlimited)
            if new_cfg.time_limits.manage_unknown_users:
                new_cfg.build_default_policy("somebody")
        except Exception as exc:  # noqa: BLE001
            log.exception("the new configuration cannot be applied; keeping the old one")
            self.store.log_event(None, "config_reload_failed", str(exc)[:300])
            self._config_mtime = self._stat_config()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._config_mtime = self._stat_config()
            self.cfg = new_cfg
            self.timekeeper.update_config(new_cfg.time_limits, new_cfg.build_default_policy)
            self.rules.week_start_index = new_cfg.time_limits.week_start_index
            self.clock = clock
            self.classifier = classifier
            self.pin.update_config(new_cfg.time_limits)
            self.power.delay = max(0, int(new_cfg.power.shutdown_delay_seconds))
            if self.entities is not None:
                self.entities.cfg = new_cfg
            if self.devices is not None:
                self.devices.update_config(new_cfg.telemetry)
            if self.telemetry is not None:
                self.telemetry.cfg = new_cfg.telemetry
            if self.winupdate is not None:
                self.winupdate.cfg = new_cfg.windows_update
            if self.updater is not None:
                self.updater.cfg = new_cfg.updates
            if self.fleet is not None:
                self.fleet.cfg = new_cfg.fleet
            self.timekeeper.external_usage = self.fleet.external_usage if self.fleet else None
            self._wire_shared_extras(new_cfg)
            self._summary_checked.clear()   # the week may start on another day now
            for policy in new_cfg.time_limits.users:
                self._ensure_user(policy.key)
            self._apply_api_config(new_cfg)
            self._attach_blocker_memory()
            if self.launcher is not None and agent_settings(old) != new_agent_settings:
                self.launcher.agent_settings = new_agent_settings
                if self.agent_server is not None:
                    for slot in self._agents.values():
                        slot.expected_exit = slot.handle is not None   # we ask them to go: not a sign of tampering
                    self.agent_server.broadcast({"type": "exit"})  # relaunched with the new settings
            self._restart_needed = sorted(set(self._restart_needed) | set(restart))
            self._policy_cache.clear()
            self._rules_policy_cache.clear()
            self._perform_resync()  # the integration gets the new state (and catalog hash) at once
            self.config_problem = None   # whatever was wrong at start: this configuration is in force now
        self._remember_good_config()
        log.info("configuration reloaded%s", f" (restart needed for: {', '.join(restart)})" if restart else "")
        self.store.log_event(None, "config_reloaded", ", ".join(restart))
        self._emit_pc_event("config_reloaded", restart_needed=restart or None)
        return {"ok": True, "restart_needed": restart}

    def _remember_good_config(self) -> None:
        """Keep a copy of the configuration file that was just applied: the service starts from
        it when config.yaml cannot be read at the next start (``hardening.load_config_or_last_good``).
        Only for the file in the data folder - that is where the service looks."""
        path = self.cfg.source_path
        if not path:
            return
        try:
            from .config import data_dir
            from .hardening import remember_good_config

            folder = data_dir()
            if os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(os.path.abspath(folder)):
                remember_good_config(path, folder)
        except Exception:  # noqa: BLE001 - a convenience for the next start, never a reason to fail a reload
            log.debug("could not keep a copy of the working configuration", exc_info=True)

    # ApiConfig settings the running server cannot take over: it is bound to these
    _API_RESTART_FIELDS = ("enabled", "host", "port", "advertise", "tls_cert", "tls_key")

    def _apply_api_config(self, new_cfg: Config) -> None:
        """Hand the running API server the settings it can use at once - the tokens,
        ``allow_config_edit``, the push interval.  What it is bound to (address, port, TLS)
        stays as it is until a restart, and is listed as such."""
        server = self.api if self.api is not None else self._down.get("api", (None,))[0]
        current = getattr(server, "cfg", None)
        if server is None or not is_dataclass(current):
            return
        live = {f.name: getattr(new_cfg.api, f.name) for f in fields(new_cfg.api)
                if f.name not in self._API_RESTART_FIELDS and hasattr(current, f.name)}
        server.cfg = replace(current, **live)

    # ------------------------------------------------------------------
    # local API backend
    # ------------------------------------------------------------------
    def api_health(self) -> dict[str, Any]:
        return {"ok": True, "version": __version__, "device": self.cfg.device.id, "name": self.cfg.device.name,
                "uptime_seconds": round(time.time() - self._started_at),
                "ticks": self._stats["ticks"],
                "restart_needed": self._restart_needed, "degraded": self._degraded,
                "tick_failures": self._stats["tick_failures"],
                "last_good_tick": _iso(self._last_good_tick) if self._last_good_tick else None}

    def api_entities(self) -> dict[str, Any]:
        """The entity catalog for the native Home Assistant integration.

        Each entry says which platform to create, where in the ``pc`` / ``users[user]``
        payload its value lives and which entity key to ``set`` for commands."""
        with self._lock:
            users, entries, digest = self._catalog(self._fresh_pc_snapshot())
            # "hash" is also in the PC state (catalog_hash): when the two differ, fetch this again
            return {"device": self.entities.device_block(), "version": __version__,
                    "users": users, "entities": entries, "hash": digest,
                    "triggers": {"user": dict(USER_TRIGGERS), "pc": dict(PC_TRIGGERS)}}

    def api_screenshot(self, user: str) -> Optional[tuple[bytes, str]]:
        """The last screenshot taken of ``user`` as (bytes, format) or None."""
        view = self._users.get(user.lower())
        if view is None or view.screenshot_data is None:
            return None
        return view.screenshot_data, str((view.screenshot or {}).get("format") or self.cfg.screenshots.format)

    def api_status(self) -> dict[str, Any]:
        with self._lock:
            if self._last_pc_snapshot is not None and 0 <= self._mono() - self._last_pc_snapshot_at < 5:
                return dict(self._last_pc_snapshot)
            return self.pc_payload(self._last_sessions)

    def api_users(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            now = self.clock()
            by_user: dict[str, list[SessionInfo]] = {}
            for s in self._last_sessions:
                by_user.setdefault(s.user, []).append(s)
            return {u: self.user_payload(u, now, by_user.get(u, [])) for u in self._visible_users(set(by_user))}

    def api_user(self, user: str) -> Optional[dict[str, Any]]:
        user = user.lower()
        with self._lock:
            if user not in self._users and self.cfg.user_policy(user) is None:
                return None
            return self.api_users().get(user) or self.user_payload(user, self.clock(), [])

    def api_report(self, user: str, hours: Optional[float] = None) -> Optional[dict[str, Any]]:
        user = user.lower()
        with self._lock:
            if user not in self._users and self.cfg.user_policy(user) is None:
                return None
            return self.usage_report(user, hours)

    def api_events(self, n: int, user: Optional[str] = None, since: Optional[float] = None,
                   announced: bool = False) -> list[dict[str, Any]]:
        """Recent entries of the event log, newest first.  With ``announced`` the events that
        were pushed to Home Assistant instead (device triggers), in the shape they were pushed
        in - what a client that was disconnected for a moment asks for with ``since``."""
        if announced:
            user = user.lower() if user else None
            events = [dict(e, type="event") for e in list(self._recent_events)
                      if (since is None or e["ts"] > since) and (user is None or e.get("user") == user)]
            return events[::-1][:max(1, int(n))]
        # every row also says "event" (what the pushed events call their kind) next to "kind"
        return [dict(row, event=row["kind"]) for row in self.store.recent_events(n, user=user, since=since)]

    def api_command(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                result = self._dispatch_json(data)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                log.warning("bad command %s: %s", _redact(data), exc)
                result = {"ok": False, "error": f"invalid value: {exc}"}
            except sqlite3.Error as exc:
                log.error("command %s could not be stored: %s", _redact(data), exc)
                result = {"ok": False, "error": f"storage error: {exc}"}
            except Exception as exc:  # noqa: BLE001 - the caller gets an answer, whatever went wrong
                log.exception("command %s failed", _redact(data))
                result = {"ok": False, "error": f"{type(exc).__name__}: {_clean(exc, 200)}"}
            self._last_api_push = NEVER  # push the new state to WebSocket clients on the next tick
        self._wake.set()
        return result

    def api_config_text(self) -> str:
        from .api import read_config_text

        return read_config_text(self.cfg.source_path)

    def api_config_write(self, text: str) -> dict[str, Any]:
        path = self.cfg.source_path
        if not path:
            return {"ok": False, "error": "service was started without a config file"}
        try:
            new_cfg = Config.from_yaml(text)   # sees how a value was written (a PIN that YAML changed)
        except Exception as exc:  # noqa: BLE001 - anything the text makes the parser raise
            return {"ok": False, "error": f"invalid configuration: {exc}"}
        new_cfg.source_path = path
        tmp = None
        try:
            # a fresh, uniquely named file: never write through something that already exists there
            fd, tmp = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=os.path.dirname(path) or ".")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except OSError as exc:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return {"ok": False, "error": f"could not write {path}: {exc}"}
        return self.reload_config(new_cfg)

    def api_reload(self) -> dict[str, Any]:
        return self.reload_config()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

_BLOCK_APP_RE = re.compile(r"^[a-z0-9][a-z0-9 ._+()-]{0,79}\.exe$")


def normalize_block_item(raw: Any) -> Optional[str]:
    """What the user typed -> ``name.exe`` (a program) or ``example.com`` (a website); None if neither.

    ``Roblox`` and ``roblox.exe`` are a program; ``youtube.com``, ``www.youtube.com`` and
    ``https://youtube.com/watch?v=...`` are the website ``youtube.com``."""
    text = str(raw or "").strip().lower()
    if not text or len(text) > 200:
        return None
    if text.endswith(".exe") or ("." not in text and "/" not in text and ":" not in text):
        name = text.replace("\\", "/").rsplit("/", 1)[-1]
        name = name if name.endswith(".exe") else name + ".exe"
        return name if _BLOCK_APP_RE.match(name) else None
    host = domain_from_url(text.lstrip("*."), "host")
    if not host or "." not in host:
        return None
    return host[4:] if host.startswith("www.") else host


def _is_app_item(item: str) -> bool:
    return item.endswith(".exe")


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """A command as it may be written to the log: no token, no PIN."""
    out = {k: v for k, v in data.items() if k not in ("token", "pin")}
    if str(out.get("action", "")).lower() == "pin" or str(out.get("entity", "")) == "parent_pin":
        out.pop("value", None)
    return out


def _hhmm(minute: Optional[int]) -> Optional[str]:
    if minute is None:
        return None
    minute = int(minute) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _restart_sections(old: Config, new: Config) -> list[str]:
    """Which changed settings need a service restart to take effect."""
    changed: list[str] = []
    for section in ("storage", "logging"):
        if asdict(getattr(old, section)) != asdict(getattr(new, section)):
            changed.append(section)
    for section in ("hardware_sensors",):
        if asdict(getattr(old, section)) != asdict(getattr(new, section)):
            changed.append(section)
    # the API's tokens and allow_config_edit are applied to the running server (_apply_api_config)
    for path in ("agent.ipc_host", "agent.ipc_port", "agent.enabled", "agent.python_path", "api.host", "api.port",
                 "api.enabled", "api.advertise", "api.tls_cert", "api.tls_key", "time_limits.block_method", "device.id",
                 "device.name", "windows_update.enabled",
                 "telemetry.usb_devices", "telemetry.printers", "telemetry.gpu", "updates.enabled"):
        sec, key = path.split(".")
        if getattr(getattr(old, sec), key, None) != getattr(getattr(new, sec), key, None):
            changed.append(path)
    if old.fleet.enabled != new.fleet.enabled:
        changed.append("fleet.enabled")
    return changed


# ----------------------------------------------------------------------
# factory
# ----------------------------------------------------------------------

def agent_settings(cfg: Config) -> dict[str, Any]:
    a = cfg.activity
    return {
        "poll_seconds": cfg.agent.poll_seconds,
        "heartbeat_seconds": cfg.agent.heartbeat_seconds,
        "audio_poll_seconds": cfg.agent.audio_poll_seconds,
        "media_poll_seconds": cfg.agent.media_poll_seconds,
        "tray_icon": cfg.agent.tray_icon,
        "browsers": a.browsers,
        "url_method": a.url_method,
        "group_sites_by": a.group_sites_by,
        "audio": a.audio_keeps_active,
        "controller_input": a.controller_input,
        "now_playing": a.now_playing,
        "media_apps": a.media_apps,
        "volume_control": cfg.agent.volume_control,
        "log_level": cfg.agent.log_level,
        "device_name": cfg.device.name,
        "version": __version__,
        # the agent restarts its own sampling loop when it hangs this long (0: never)
        "poll_watchdog_seconds": getattr(cfg.agent, "poll_watchdog_seconds", 60),
    }


def build_orchestrator(cfg: Config) -> Orchestrator:
    """Wire the real backends (Windows) or graceful no-ops (elsewhere)."""
    from .devices import DeviceMonitor
    from .hwsensors import HardwareSensors
    from .ipc import AgentServer
    from .logonblock import make_blocker
    from .telemetry import Telemetry
    from .winupdate import WindowsUpdate

    store = Store(cfg.storage.path)
    sessions = launcher = None
    if sys.platform == "win32":
        from .launcher import AgentLauncher
        from .winsessions import WindowsSessions

        sessions = WindowsSessions()
        if cfg.agent.enabled:
            launcher = AgentLauncher(cfg.agent, agent_settings(cfg))
    else:
        log.warning("not running on Windows: session control, agents and logon blocking are disabled")
    blocker = make_blocker(cfg.time_limits.block_method)
    gpu = None
    if cfg.telemetry.gpu:
        try:
            from .gpu import detect_gpu

            gpu = detect_gpu()
        except Exception as exc:  # noqa: BLE001
            log.info("GPU detection failed: %s", exc)
    telemetry = Telemetry(cfg.telemetry, gpu=gpu)
    hwsensors = HardwareSensors(cfg.hardware_sensors) if sys.platform == "win32" or cfg.hardware_sensors.backend == "lhm_http" else None
    orch = Orchestrator(cfg, store=store, sessions=sessions, blocker=blocker, launcher=launcher,
                        telemetry=telemetry, hwsensors=hwsensors)
    orch.devices = DeviceMonitor(cfg.telemetry, on_event=lambda kind, data: orch._emit_pc_event(kind, **data)) \
        if (cfg.telemetry.usb_devices or cfg.telemetry.printers) else None
    orch.winupdate = WindowsUpdate(cfg.windows_update, on_change=lambda d: orch._emit_pc_event(d.get("event", "updates_available"))) \
        if cfg.windows_update.enabled else None
    if cfg.updates.enabled:
        from .config import data_dir
        from .updater import SelfUpdate

        orch.updater = SelfUpdate(cfg.updates, os.path.join(data_dir(), "update"), on_change=orch._emit_pc_event)
    if not cfg.api.enabled:
        log.warning("api.enabled is false: Home Assistant cannot reach this PC")
    if cfg.agent.enabled and launcher is not None:
        orch.agent_server = AgentServer(
            cfg.agent.ipc_host, cfg.agent.ipc_port, on_report=orch.on_agent_report,
            validate=orch.validate_agent_token, on_connect=orch.on_agent_connected,
            on_disconnect=orch.on_agent_disconnected, on_message=orch.on_agent_message,
        )
    if cfg.api.enabled:
        from .api import ApiServer
        from .webui import SETUP_PAGE

        orch.api = ApiServer(cfg.api, orch, html=SETUP_PAGE)
        if cfg.api.advertise:
            from .discovery import MdnsAdvertiser

            orch.mdns = MdnsAdvertiser(cfg.api, device_id=cfg.device.id, device_name=cfg.device.name)
    return orch
