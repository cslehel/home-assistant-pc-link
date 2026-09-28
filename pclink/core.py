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
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional

import yaml

from . import __version__
from .activity import ActivityClassifier
from .config import AppLauncher, Config, ConfigError, CustomCommand, UserPolicy
from .entities import Entity, EntityFactory
from .fleet import FleetTracker
from .models import Activity, AgentReport, SessionInfo
from .pin import ParentPin
from .power import PowerControl, send_wol
from .presence import Presence, evaluate as evaluate_presence, user_state as presence_user_state
from .rules import RuleDecision, RuleEngine, closable
from .store import Store
from .timekeeper import Action, TimeKeeper, TimeStatus
from .util import human_minutes, slugify
from .wellbeing import BreakReminder
from .winsessions import SESSION_EVENT_NAMES, WTS_SESSION_LOGOFF

log = logging.getLogger(__name__)

STATUS_ORDER = {"active": 0, "idle": 1, "away": 2, "locked": 3, "disconnected": 4, "logged_off": 5}
PRESENCE_ORDER = {"active": 0, "passive": 1, "idle": 2, "away": 3, "none": 4}
FRESH_LOGON_GRACE = 15
CONFIG_POLL_SECONDS = 10.0
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class _AgentSlot:
    handle: Any = None
    token: Optional[str] = None
    next_launch: float = 0.0
    backoff: float = 15.0
    connected: bool = False


@dataclass
class _UserView:
    user: str
    activity: Activity = field(default_factory=Activity)
    last_activity_tick: Optional[float] = None
    last_payload: Optional[str] = None
    last_publish: float = 0.0
    discovered: bool = False
    last_agent_status: Optional[str] = None
    # transition tracking for events
    logged_in: Optional[bool] = None
    presence: Optional[str] = None
    focus_active: Optional[bool] = None
    out_of_time: Optional[bool] = None
    exceeded_rules: set = field(default_factory=set)
    screenshot: Optional[dict[str, Any]] = None
    last_refusal: Optional[dict[str, Any]] = None
    best_session: Optional[int] = None


class Orchestrator:
    def __init__(self, cfg: Config, *, store: Optional[Store] = None, sessions=None, blocker=None, launcher=None,
                 power: Optional[PowerControl] = None, mqtt=None, telemetry=None, hwsensors=None,
                 agent_server=None, clock: Optional[Callable[[], datetime]] = None, winupdate=None, devices=None,
                 api=None):
        self.cfg = cfg
        self.store = store or Store(cfg.storage.path)
        self.timekeeper = TimeKeeper(cfg.time_limits, self.store, cfg.build_default_policy)
        self.classifier = ActivityClassifier(cfg.activity)
        self.rules = RuleEngine(self.store)
        self.breaks = BreakReminder()
        self.pin = ParentPin(cfg.time_limits, on_event=self._on_pin_event)
        self.sessions = sessions
        self.blocker = blocker
        self.launcher = launcher
        self.power = power or PowerControl(cfg.power.shutdown_delay_seconds)
        self.mqtt = mqtt
        self.telemetry = telemetry
        self.hwsensors = hwsensors
        self.agent_server = agent_server
        self.winupdate = winupdate
        self.devices = devices
        self.api = api
        self.clock = clock or datetime.now
        self.entities: Optional[EntityFactory] = None
        if mqtt is not None:
            self.entities = EntityFactory(
                cfg, base_topic=mqtt.base, availability_topic=mqtt.availability_topic,
                state_topic=mqtt.state_topic, discovery_prefix=mqtt.discovery_prefix,
            )
        self.fleet = FleetTracker(cfg.fleet, cfg.mqtt.base_topic, cfg.device.id) if cfg.fleet.enabled else None
        if self.fleet is not None:
            self.timekeeper.external_usage = self.fleet.external_usage
        # runtime state -----------------------------------------------------
        self._reports: dict[int, AgentReport] = {}
        self._presence: dict[int, Presence] = {}
        self._agents: dict[int, _AgentSlot] = {}
        self._users: dict[str, _UserView] = {}
        self._slug_to_user: dict[str, str] = {}
        self._block_applied: dict[str, bool] = {}
        self._block_asserted_at: float = 0.0
        self._commands: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._last_pc_publish = 0.0
        self._last_pc_snapshot: Optional[dict[str, Any]] = None
        self._last_flush = time.monotonic()
        self._last_purge: Optional[date] = None
        self._discovered: set[str] = set()
        self._pc_discovery_signature: Optional[str] = None
        self._fleet_signature: Optional[str] = None
        self._last_fleet_publish = 0.0
        self._last_sessions: list[SessionInfo] = []
        self._started_at = time.time()
        self._stats = {"ticks": 0, "actions": 0, "events": 0}
        self._config_mtime: Optional[float] = self._stat_config()
        self._config_checked = 0.0
        self._restart_needed: list[str] = []
        self._pending_screenshots: dict[str, dict[str, Any]] = {}
        self._pin_notice: Optional[dict[str, Any]] = None
        self._last_api_push = 0.0
        self._started_event_sent = False
        for policy in cfg.time_limits.users:
            self._slug_to_user[policy.slug] = policy.key
            self._users[policy.key] = _UserView(user=policy.key)

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
        try:
            self.store.purge(self.cfg.storage.retention_days)
        except Exception:  # noqa: BLE001
            log.exception("store purge failed")
        for name, comp in (("hardware sensors", self.hwsensors), ("windows update", self.winupdate)):
            if comp is not None:
                try:
                    comp.start()
                except Exception:  # noqa: BLE001
                    log.exception("%s failed to start", name)
        if self.agent_server is not None:
            try:
                self.agent_server.start()
            except Exception:  # noqa: BLE001
                log.exception("agent IPC server failed to start; agents disabled")
                self.agent_server = None
        if self.api is not None:
            try:
                self.api.start()
            except Exception:  # noqa: BLE001
                log.exception("local API failed to start")
                self.api = None
        if self.mqtt is not None:
            self.mqtt.start()

    def _shutdown_components(self) -> None:
        log.info("shutting down")
        try:
            self.store.flush()
        except Exception:  # noqa: BLE001
            log.exception("final flush failed")
        if self.agent_server is not None:
            self.agent_server.broadcast({"type": "exit"})
            time.sleep(0.2)
            self.agent_server.stop()
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
        for comp in (self.hwsensors, self.winupdate, self.api):
            if comp is not None:
                try:
                    comp.stop()
                except Exception:  # noqa: BLE001
                    log.debug("component stop failed", exc_info=True)
        if self.mqtt is not None:
            self.mqtt.stop()
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
        self._reports[session_id] = report

    def on_agent_message(self, session_id: int, message: dict[str, Any]) -> None:
        """Non-report messages from an agent (screenshot results); handled on the main loop."""
        self._commands.put(("_agent_message", (session_id, message)))
        self._wake.set()

    def validate_agent_token(self, session_id: int, token: str) -> bool:
        slot = self._agents.get(session_id)
        return bool(slot and slot.token and secrets.compare_digest(slot.token, token))

    def on_agent_connected(self, session_id: int, user: str, pid: int) -> None:
        slot = self._agents.get(session_id)
        if slot:
            slot.connected = True
            slot.backoff = self.cfg.agent.relaunch_backoff_seconds
        self._wake.set()

    def on_agent_disconnected(self, session_id: int) -> None:
        slot = self._agents.get(session_id)
        if slot:
            slot.connected = False
        self._reports.pop(session_id, None)

    def on_command(self, object_id: str, payload: Any) -> None:
        """Called from the MQTT thread; executed on the main loop."""
        self._commands.put((object_id, payload))
        self._wake.set()

    def on_peer_message(self, topic: str, payload: bytes) -> None:
        """MQTT traffic from other PCs (fleet)."""
        if self.fleet is not None and self.fleet.on_message(topic, payload) and self.fleet.changed:
            self._wake.set()

    def on_mqtt_connected(self) -> None:
        self._discovered.clear()
        self._pc_discovery_signature = None
        self._fleet_signature = None
        for view in self._users.values():
            view.discovered = False
            view.last_payload = None
        self._last_pc_publish = 0.0
        self._wake.set()

    def _on_pin_event(self, kind: str) -> None:
        self._emit_pc_event(kind)

    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------
    def tick(self) -> None:
        with self._lock:
            self._tick()

    def _tick(self) -> None:
        now = self.clock()
        now_ts = time.time()
        self._stats["ticks"] += 1
        self._check_config_reload(now_ts)
        self._drain_commands()
        sessions = self._collect_sessions(now_ts)
        self._last_sessions = sessions
        self._manage_agents(sessions, now_ts)
        by_user: dict[str, list[SessionInfo]] = {}
        for s in sessions:
            by_user.setdefault(s.user, []).append(s)
        for user in by_user:
            self._ensure_user(user)
        actions = self.timekeeper.tick(now, by_user)
        for action in actions:
            self._stats["actions"] += 1
            try:
                self._execute(action, by_user)
            except Exception:  # noqa: BLE001
                log.exception("action %s for %s failed", action.kind, action.user)
        self._track_activity(now, by_user)
        self._enforce_rules(now, by_user)
        self._wellbeing(now, by_user)
        self._transitions(now, by_user)
        self._reassert_blocks(now, now_ts)
        self._push_agent_status(now, by_user)
        if not self._started_event_sent and self.mqtt is not None:
            self._started_event_sent = True
            self._emit_pc_event("started")
        if self.mqtt is not None:
            self._publish_users(now, by_user, now_ts)
            if now_ts - self._last_pc_publish >= self.cfg.telemetry.interval_seconds:
                self._publish_pc(sessions)
                self._last_pc_publish = now_ts
            self._publish_fleet(now, by_user, now_ts)
        self._api_push(now, by_user, now_ts)
        if time.monotonic() - self._last_flush >= self.cfg.storage.flush_seconds:
            self.store.flush()
            self._last_flush = time.monotonic()
        if self._last_purge != now.date():
            self._last_purge = now.date()
            self.store.purge(self.cfg.storage.retention_days, now.date())

    # -- sessions -------------------------------------------------------------
    def _collect_sessions(self, now_ts: float) -> list[SessionInfo]:
        if self.sessions is None:
            return []
        try:
            sessions = self.sessions.list_sessions()
        except Exception:  # noqa: BLE001
            log.exception("session enumeration failed")
            return list(self._last_sessions)
        stale_after = max(self.cfg.agent.heartbeat_seconds * 3, self.cfg.agent.poll_seconds * 5, 20.0)
        present = set()
        for s in sessions:
            present.add(s.session_id)
            report = self._reports.get(s.session_id)
            if report is not None and now_ts - report.ts <= stale_after:
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
        return sessions

    def _manage_agents(self, sessions: list[SessionInfo], now_ts: float) -> None:
        if self.launcher is None or self.agent_server is None or not self.cfg.agent.enabled:
            return
        active = {s.session_id: s for s in sessions if s.state in ("active", "connected")}
        for sid, slot in list(self._agents.items()):
            if slot.handle is not None and (sid not in active or not self.launcher.alive(slot.handle)):
                if sid in active:
                    log.info("agent for session %s exited; will relaunch", sid)
                self.launcher.release(slot.handle)
                slot.handle = None
                slot.connected = False
                if sid not in active:
                    del self._agents[sid]
                else:
                    slot.next_launch = now_ts + slot.backoff
                    slot.backoff = min(slot.backoff * 2, self.cfg.agent.relaunch_backoff_max_seconds)
            elif slot.handle is None and sid not in active:
                del self._agents[sid]
        for sid, session in active.items():
            slot = self._agents.get(sid)
            if slot is None:
                slot = _AgentSlot(backoff=self.cfg.agent.relaunch_backoff_seconds)
                self._agents[sid] = slot
            if slot.handle is not None or now_ts < slot.next_launch:
                continue
            slot.token = secrets.token_urlsafe(24)
            slot.handle = self.launcher.launch(sid, slot.token, session.user)
            slot.next_launch = now_ts + slot.backoff
            if slot.handle is None:
                slot.backoff = min(slot.backoff * 2, self.cfg.agent.relaunch_backoff_max_seconds)

    def _ensure_user(self, user: str) -> _UserView:
        view = self._users.get(user)
        if view is None:
            view = _UserView(user=user)
            self._users[user] = view
            slug = slugify(user)
            if slug in self._slug_to_user and self._slug_to_user[slug] != user:
                slug = f"{slug}_{len(self._slug_to_user)}"
            self._slug_to_user[slug] = user
        return view

    def _slug_for(self, user: str) -> str:
        for slug, u in self._slug_to_user.items():
            if u == user:
                return slug
        self._ensure_user(user)
        return self._slug_for(user)

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
            self._emit_user_event(action.user, "time_up", action="logoff", reason=action.reason)
        elif action.kind == "lock":
            for sid in targets:
                self._lock_session(sid)
            self._emit_user_event(action.user, "time_up", action="lock", reason=action.reason)
        elif action.kind == "shutdown":
            others = [s for u, ss in by_user.items() if u != action.user for s in ss if s.state == "active"]
            if others:
                log.warning("shutdown requested for %s but other users are active (%s); logging off instead",
                            action.user, sorted({s.user for s in others}))
                for sid in targets:
                    self._logoff_session(sid)
            else:
                self.power.shutdown(message="Computer time is over.", delay=5)
            self._emit_user_event(action.user, "time_up", action="shutdown", reason=action.reason)
        elif action.kind == "block":
            self._apply_block(action.user, True)
            self._emit_user_event(action.user, "login_blocked", reason=action.reason)
        elif action.kind == "unblock":
            self._apply_block(action.user, False)
            self._emit_user_event(action.user, "login_unblocked")

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
        if self.agent_server is not None and self.agent_server.send(session_id, {"type": "lock"}):
            return
        if self.sessions is not None:
            try:
                self.sessions.disconnect(session_id)
            except Exception as exc:  # noqa: BLE001
                log.error("lock (disconnect) of session %s failed: %s", session_id, exc)

    def notify_sessions(self, session_ids: list[int], title: str, message: str, level: str = "info") -> None:
        for sid in session_ids:
            delivered = False
            if self.agent_server is not None:
                delivered = self.agent_server.send(sid, {"type": "notify", "title": title, "message": message, "level": level})
            if not delivered and self.sessions is not None:
                self.sessions.send_message(sid, title, message, timeout=60, critical=level == "critical")

    def _send_agent(self, session_ids: list[int], message: dict[str, Any]) -> int:
        if self.agent_server is None:
            return 0
        return sum(1 for sid in session_ids if self.agent_server.send(sid, message))

    def _apply_block(self, user: str, blocked: bool) -> None:
        if self.blocker is None:
            return
        try:
            ok = self.blocker.block(user) if blocked else self.blocker.unblock(user)
        except Exception:  # noqa: BLE001
            log.exception("block change for %s failed", user)
            ok = False
        if ok:
            self._block_applied[user] = blocked
            self.store.log_event(user, "login_block" if blocked else "login_unblock", self.cfg.time_limits.block_method)

    def _reassert_blocks(self, now: datetime, now_ts: float) -> None:
        if self.blocker is None or now_ts - self._block_asserted_at < self.cfg.time_limits.reassert_block_seconds:
            return
        self._block_asserted_at = now_ts
        for user in self.timekeeper.managed_users():
            desired = self.timekeeper.status(user, now).block_desired
            try:
                actual = self.blocker.is_blocked(user)
            except Exception:  # noqa: BLE001
                actual = None
            if actual is None or actual != desired or self._block_applied.get(user) != desired:
                self._apply_block(user, desired)

    # -- activity ---------------------------------------------------------------
    def _pick_session(self, sessions: list[SessionInfo]) -> Optional[SessionInfo]:
        candidates = [s for s in sessions if s.state == "active"]
        if not candidates:
            return None
        candidates.sort(key=lambda s: (bool(s.locked), s.report is None, PRESENCE_ORDER.get(s.presence or "none", 9),
                                       s.idle_seconds if s.idle_seconds is not None else 1e9))
        return candidates[0]

    def _track_activity(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        today = now.date()
        now_ts = now.timestamp()
        for user, view in self._users.items():
            sessions = by_user.get(user, [])
            best = self._pick_session(sessions)
            view.best_session = best.session_id if best else None
            if best is None:
                activity = Activity(category="none")
            elif best.locked:
                activity = Activity(category="locked")
            elif best.presence == "away":
                activity = Activity(category="away")
            else:
                activity = self.classifier.classify(best.report, locked=best.locked)
            view.activity = activity
            last = view.last_activity_tick
            view.last_activity_tick = now_ts if sessions else None
            if not sessions or last is None:
                continue
            if not self.cfg.activity.track_unmanaged_users and not self.timekeeper.is_managed(user):
                continue
            dt = max(0.0, min(now_ts - last, 120.0))
            if dt <= 0 or not activity.is_active:
                continue
            app_label = (activity.app or "").rsplit(".exe", 1)[0] or activity.app or "unknown"
            if activity.category == "gaming":
                self.store.add_usage(user, today, gaming=dt)
                self.store.add_app(user, today, f"game:{app_label}", dt)
            else:
                self.store.add_app(user, today, f"app:{app_label}", dt)
            if activity.domain:
                self.store.add_site(user, today, activity.domain, dt)
            if activity.domain or activity.category == "browsing":
                if activity.category != "gaming":
                    self.store.add_usage(user, today, browsing=dt)
            policy = self.timekeeper.policy_for(user)
            if policy is not None and policy.rules:
                self.rules.account(user, policy, today, activity, dt)

    # -- per-app rules & focus mode ----------------------------------------------------
    def _enforce_rules(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        for user, sessions in by_user.items():
            policy = self.timekeeper.policy_for(user)
            if policy is None or policy.unlimited:
                continue
            view = self._users[user]
            best = self._pick_session(sessions)
            if best is None or best.report is None:
                continue
            st = self.timekeeper.status(user, now)
            focus_active = st.focus_active
            if not (policy.rules or focus_active):
                continue
            decisions = self.rules.tick(
                user, policy, now, view.activity, focus_active=focus_active,
                focus_extra_allow=self.timekeeper.focus_extra_allow(user) if focus_active else None,
                enforcing=st.enforcing,
            )
            for decision in decisions:
                try:
                    self._apply_decision(user, best, decision)
                except Exception:  # noqa: BLE001
                    log.exception("rule decision %s for %s failed", decision.slug, user)

    def _apply_decision(self, user: str, session: SessionInfo, decision: RuleDecision) -> None:
        fg = session.report.foreground if session.report else None
        if decision.notify:
            self.notify_sessions([session.session_id], decision.title, decision.message, decision.level)
            if decision.minutes_left == 0 or decision.source == "focus":
                self._emit_user_event(user, "app_limit_reached", rule=decision.slug, source=decision.source,
                                      app=decision.app, action=decision.kind)
        if decision.kind == "close" and fg is not None and fg.name and closable(fg.name):
            sent = self._send_agent([session.session_id], {
                "type": "close", "pid": fg.pid, "name": fg.name, "tab": decision.close_tab,
                "reason": decision.title,
            })
            if sent:
                self.store.log_event(user, "app_close", f"{fg.name} ({'tab' if decision.close_tab else 'process'}) rule={decision.slug}")
                self._emit_user_event(user, "app_closed", rule=decision.slug, app=fg.name, tab=decision.close_tab)
            else:
                log.info("cannot close %s for %s: no agent in session %s", fg.name, user, session.session_id)

    # -- break reminders -----------------------------------------------------------------
    def _wellbeing(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        now_ts = now.timestamp()
        for user, sessions in by_user.items():
            policy = self.timekeeper.policy_for(user)
            if policy is None:
                continue
            enabled = self.timekeeper.breaks_enabled(user, policy)
            engaged = any(s.presence in ("active", "passive") for s in sessions)
            due = self.breaks.tick(user, policy.breaks, now_ts, now.date(), engaged=engaged, enabled=enabled)
            if due:
                targets = [s.session_id for s in sessions if s.presence in ("active", "passive")]
                self.notify_sessions(targets, due["title"], due["message"], "info")
                self.store.log_event(user, "break_reminder", f"after {due['continuous_minutes']} min")
                self._emit_user_event(user, "break_due", continuous_minutes=due["continuous_minutes"])

    # -- transitions -> device trigger events ---------------------------------------------
    def _transitions(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        for user, view in self._users.items():
            sessions = by_user.get(user, [])
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
            if self.timekeeper.is_managed(user):
                policy = self.timekeeper.policy_for(user)
                focus_active, _until, source = self.timekeeper.focus_state(user, policy, now)
                if view.focus_active is not None and focus_active != view.focus_active:
                    self._emit_user_event(user, "focus_started" if focus_active else "focus_ended", source=source)
                    if not focus_active:
                        self.rules.reset(user)
                view.focus_active = focus_active

    def _presence_reason(self, sessions: list[SessionInfo]) -> str:
        best = self._pick_session(sessions)
        return best.presence_reason if best else ""

    # -- events ----------------------------------------------------------------------------
    def _emit_user_event(self, user: str, kind: str, **data: Any) -> None:
        self._stats["events"] += 1
        slug = self._slug_for(user)
        payload = {"ts": time.time(), "user": user, "event": kind, **{k: v for k, v in data.items() if v is not None}}
        log.debug("event %s for %s %s", kind, user, data)
        if self.mqtt is not None:
            self.mqtt.publish_event(f"user/{slug}", kind)
            self.mqtt.publish(f"{self.mqtt.base}/events", payload, retain=False)
        if self.api is not None:
            self.api.push({"type": "event", **payload})

    def _emit_pc_event(self, kind: str, **data: Any) -> None:
        self._stats["events"] += 1
        payload = {"ts": time.time(), "event": kind, **{k: v for k, v in data.items() if v is not None}}
        if self.mqtt is not None:
            self.mqtt.publish_event("pc", kind)
            self.mqtt.publish(f"{self.mqtt.base}/events", payload, retain=False)
        if self.api is not None:
            self.api.push({"type": "event", **payload})

    # -- agent status (tray tooltip) ---------------------------------------------
    def _push_agent_status(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        if self.agent_server is None:
            return
        for user, sessions in by_user.items():
            st = self.timekeeper.status(user, now)
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
            view = self._ensure_user(user)
            if view.last_agent_status == text:
                continue
            payload = {"type": "status", "text": text, "remaining_minutes": st.remaining_minutes,
                       "used_minutes": round(st.used_seconds / 60), "limit_minutes": st.limit_minutes,
                       "focus": st.focus_active}
            sent = False
            for s in sessions:
                if self.agent_server.send(s.session_id, payload):
                    sent = True
            if sent:
                view.last_agent_status = text

    # ------------------------------------------------------------------
    # Home Assistant publishing
    # ------------------------------------------------------------------
    def _publish_entities(self, entities: list[Entity], node_id: Optional[str] = None) -> None:
        assert self.mqtt is not None
        for ent in entities:
            key = f"{node_id or ''}{ent.component}/{ent.object_id}"
            if key in self._discovered:
                continue
            if node_id:
                self.mqtt.publish_discovery(ent.component, ent.object_id, ent.payload, node_id=node_id)
            else:
                self.mqtt.publish_discovery(ent.component, ent.object_id, ent.payload)
            self._discovered.add(key)

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

    def user_payload(self, user: str, now: datetime, sessions: list[SessionInfo]) -> dict[str, Any]:
        st: TimeStatus = self.timekeeper.status(user, now)
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
            "activity_details": {"app": act.app, "exe": act.exe, "title": act.title, "domain": act.domain, "url": act.url},
            "app": act.app,
            "domain": act.domain,
            "now_playing": (media.label() or None) if media and media.is_playing else None,
            "media": media.to_dict() if media else {},
            "volume": report.volume if report else None,
            "muted": report.muted if report else None,
            "used_minutes": round(totals.active / 60, 1),
            "gaming_minutes": round(totals.gaming / 60, 1),
            "browsing_minutes": round(totals.browsing / 60, 1),
            "weekly_used_minutes": round(st.weekly_used_seconds / 60, 1) if st.managed else round(self._week_used(user, today) / 60, 1),
            "top_games": games,
            "top_apps": dict(list(apps.items())[: self.cfg.activity.top_n]),
            "top_app": next(iter(apps), None),
            "top_sites": sites,
            "history": self.store.history(user, self.cfg.activity.history_days, today),
            "time_limits_enabled": self.timekeeper.global_enabled,
            "screenshot": view.screenshot,
        }
        if st.managed and policy is not None:
            weekday_default = policy.daily_limits.get(0)
            weekend_default = policy.daily_limits.get(5)
            focus_minutes = int(self.store.get_setting(user, "focus_minutes", policy.focus.default_minutes) or policy.focus.default_minutes)
            rule_statuses = self.rules.statuses(user, policy, now, act)
            rules = {}
            for rs in rule_statuses:
                d = rs.to_dict()
                d["state"] = rs.state
                d["limit_minutes"] = unlimited if rs.limit_minutes is None else rs.limit_minutes
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
                    "bonus_minutes": round(st.bonus_seconds / 60),
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
                        "until": datetime.fromtimestamp(st.focus_until).isoformat(timespec="seconds") if st.focus_until else None,
                        "pauses_limit": st.focus_pauses_limit,
                        "allow_apps": policy.focus.allow_apps, "allow_sites": policy.focus.allow_sites,
                        "allow_categories": policy.focus.allow_categories,
                        "extra_allow": self.timekeeper.focus_extra_allow(user) if st.focus_active else [],
                    },
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

    def _week_used(self, user: str, today: date) -> float:
        return self.store.usage_range(user, self.timekeeper.week_start(today), today).active

    def _publish_users(self, now: datetime, by_user: dict[str, list[SessionInfo]], now_ts: float) -> None:
        assert self.mqtt is not None and self.entities is not None
        for user, view in list(self._users.items()):
            managed = self.timekeeper.is_managed(user)
            if not managed and not self.cfg.activity.track_unmanaged_users and user not in by_user:
                continue
            slug = self._slug_for(user)
            if not view.discovered:
                policy = self.timekeeper.policy_for(user)
                self._publish_entities(self.entities.user_entities(user, policy, managed))
                self._publish_entities(self.entities.user_trigger_entities(user, policy))
                view.discovered = True
            payload = self.user_payload(user, now, by_user.get(user, []))
            encoded = json.dumps(payload, sort_keys=True, default=str)
            if encoded != view.last_payload or now_ts - view.last_publish >= 60:
                self.mqtt.publish(self.mqtt.user_state_topic(slug), payload, retain=True)
                view.last_payload = encoded
                view.last_publish = now_ts

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
        active = [s for s in sessions if s.state == "active"]
        snap["device_name"] = self.cfg.device.name
        snap["active_sessions"] = len(active)
        snap["sessions"] = [s.to_dict() for s in sessions]
        users = sorted({s.user for s in sessions if s.logged_in})
        snap["logged_in_users"] = ", ".join(users) if users else "none"
        snap["any_user_active"] = any(not s.locked for s in active)
        snap["anyone_present"] = any(s.presence in ("active", "passive") for s in active)
        snap["time_limits_enabled"] = self.timekeeper.global_enabled
        snap["parent_present"] = self.timekeeper.parent_present
        snap["parent_pin_enabled"] = self.pin.enabled
        snap["parent_unlocked"] = self.pin.unlocked()
        snap["parent_pin_state"] = ""
        snap["pin_last_refusal"] = self._pin_notice
        if self.fleet is not None:
            snap["fleet"] = {"pcs_online": len(self.fleet.online_peers()) + 1, "leader": self.fleet.is_leader(),
                             "peers": self.fleet.online_peers()}
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
            "managed_users": self.timekeeper.managed_users(),
            "device_name": self.cfg.device.name,
            "restart_needed": self._restart_needed,
            "api": f"http://{self.cfg.api.host}:{self.cfg.api.port}/" if self.api is not None else None,
            "pin": self.pin.status(),
        }
        self._last_pc_snapshot = snap
        return snap

    def _publish_pc(self, sessions: list[SessionInfo]) -> None:
        assert self.mqtt is not None and self.entities is not None
        snap = self.pc_payload(sessions)
        readings = self.hwsensors.latest() if self.hwsensors is not None else []
        signature = json.dumps({"disks": snap.get("disks"), "gpu": bool(snap.get("gpu_name")),
                                "battery": snap.get("battery_percent") is not None,
                                "hw": sorted(r.key for r in readings)}, sort_keys=True)
        if signature != self._pc_discovery_signature:
            self._publish_entities(self.entities.pc_entities(snap))
            self._publish_entities(self.entities.hw_entities(readings))
            self._publish_entities(self.entities.pc_trigger_entities())
            self._pc_discovery_signature = signature
        self.mqtt.publish(self.mqtt.state_topic, snap, retain=True)

    def _publish_fleet(self, now: datetime, by_user: dict[str, list[SessionInfo]], now_ts: float) -> None:
        if self.fleet is None or not self.cfg.fleet.publish_rollup or self.mqtt is None or self.entities is None:
            return
        if not self.fleet.is_leader():
            return
        if not (self.fleet.changed or now_ts - self._last_fleet_publish >= 60):
            return
        self.fleet.changed = False
        self._last_fleet_publish = now_ts
        own = {user: self.user_payload(user, now, by_user.get(user, [])) for user in self._users}
        rollup = self.fleet.rollup(own)
        signature = json.dumps(sorted(rollup["users"]))
        if signature != self._fleet_signature:
            self._publish_entities(
                self.entities.fleet_entities(rollup["users"], self.fleet.rollup_topic(), self.fleet.rollup_user_topic),
                node_id="fleet",
            )
            self._fleet_signature = signature
        self.mqtt.publish(self.fleet.rollup_topic(), {k: v for k, v in rollup.items() if k != "users"}, retain=True)
        for user, info in rollup["users"].items():
            self.mqtt.publish(self.fleet.rollup_user_topic(slugify(user)), info, retain=True)

    def _api_push(self, now: datetime, by_user: dict[str, list[SessionInfo]], now_ts: float) -> None:
        if self.api is None or self.api.client_count == 0:
            return
        if now_ts - self._last_api_push < self.cfg.api.push_interval_seconds:
            return
        self._last_api_push = now_ts
        self.api.push({"type": "state", "pc": self._last_pc_snapshot or self.pc_payload(self._last_sessions),
                       "users": {u: self.user_payload(u, now, by_user.get(u, [])) for u in self._users}})

    # ------------------------------------------------------------------
    # commands from Home Assistant / the API
    # ------------------------------------------------------------------
    def _drain_commands(self) -> None:
        while True:
            try:
                object_id, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if object_id == "_agent_message":
                    self._handle_agent_message(*payload)
                else:
                    self.dispatch(object_id, payload)
            except Exception:  # noqa: BLE001
                log.exception("command %s failed", object_id)

    def _refuse(self, user: Optional[str], action: str, reason: str) -> dict[str, Any]:
        notice = {"ts": time.time(), "user": user, "action": action, "reason": reason}
        self._pin_notice = notice
        if user and user in self._users:
            self._users[user].last_refusal = notice
            self._users[user].last_payload = None
        log.warning("refused %s for %s: %s", action, user or "-", reason)
        self.store.log_event(user, "refused", f"{action}: {reason}")
        return {"ok": False, "error": reason}

    def _gate(self, kind: str, action: str, user: Optional[str] = None, pin: Any = None) -> Optional[dict[str, Any]]:
        """Return a refusal dict when the parent PIN is required and missing."""
        allowed, reason = self.pin.authorize(kind, pin)
        if allowed:
            return None
        return self._refuse(user, action, reason)

    def dispatch(self, object_id: str, payload: Any) -> dict[str, Any]:
        if object_id == "cmd":
            return self._dispatch_json(payload if isinstance(payload, dict) else {})
        text = payload.strip() if isinstance(payload, str) else str(payload)
        log.info("HA command %s = %r", object_id, "***" if object_id == "parent_pin" else text[:100])
        now = self.clock()
        if object_id in ("shutdown", "reboot", "sleep", "hibernate", "cancel_shutdown"):
            self._power_action(object_id)
        elif object_id == "lock_all":
            for s in self._last_sessions:
                if s.state == "active":
                    self._lock_session(s.session_id)
        elif object_id == "time_limits_enabled":
            on = text.upper() == "ON"
            if not on and self._gate("disable", "time_limits_off"):
                return {"ok": False}
            self.timekeeper.set_global_enabled(on)
        elif object_id == "broadcast_message":
            if text:
                self.notify_sessions([s.session_id for s in self._last_sessions if s.state == "active"], "Message", text, "info")
        elif object_id == "parent_pin":
            self.pin.try_unlock(text)
            self._force_republish_all()
        elif object_id == "parent_present":
            self.timekeeper.set_parent_present(text.upper() == "ON")
            self._force_republish_all()
        elif object_id == "reload_config":
            self.reload_config()
        elif object_id == "check_updates":
            if self.winupdate is not None:
                self.winupdate.check_now()
        elif object_id == "install_updates":
            if self.winupdate is not None:
                self.winupdate.install()
        elif object_id.startswith("launch_"):
            self._launch_app(object_id[7:])
        elif object_id.startswith("close_"):
            self._close_app(object_id[6:])
        elif object_id.startswith("cmd_"):
            self._run_custom(object_id[4:])
        elif object_id.startswith("user_"):
            return self._dispatch_user(object_id[5:], text, now)
        else:
            log.warning("unknown command entity %s", object_id)
            return {"ok": False, "error": f"unknown command {object_id}"}
        return {"ok": True}

    def _force_republish_all(self) -> None:
        for view in self._users.values():
            view.last_payload = None
        self._last_pc_publish = 0.0

    def _dispatch_user(self, rest: str, text: str, now: datetime, pin: Any = None) -> dict[str, Any]:
        user = sub = None
        for slug in sorted(self._slug_to_user, key=len, reverse=True):
            if rest.startswith(slug + "_"):
                user, sub = self._slug_to_user[slug], rest[len(slug) + 1:]
                break
        if user is None or sub is None:
            log.warning("command for unknown user: %s", rest)
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
            minutes = int(number())
            current = tk.effective_limit_minutes(user, today)
            if (current is None or minutes > current) and self._gate("grant", "limit_today", user, pin):
                return {"ok": False}
            tk.set_limit_today(user, today, minutes)
        elif sub == "weekday_limit":
            if self._gate("grant", "weekday_limit", user, pin):
                return {"ok": False}
            tk.set_weekday_limit(user, int(number()))
        elif sub == "weekend_limit":
            if self._gate("grant", "weekend_limit", user, pin):
                return {"ok": False}
            tk.set_weekend_limit(user, int(number()))
        elif sub == "weekly_limit":
            if self._gate("grant", "weekly_limit", user, pin):
                return {"ok": False}
            minutes = int(number())
            tk.set_weekly_limit(user, None if minutes <= 0 else minutes)
        elif sub == "bonus_today":
            minutes = number()
            if minutes > tk.bonus_seconds(user, today) / 60 and self._gate("grant", "bonus_today", user, pin):
                return {"ok": False}
            tk.set_bonus_minutes(user, today, minutes)
        elif sub in ("add_15", "add_30", "add_minutes"):
            minutes = 15 if sub == "add_15" else 30 if sub == "add_30" else number()
            if minutes <= 0:
                return {"ok": True}
            if self._gate("grant", sub, user, pin):
                return {"ok": False}
            tk.add_bonus_minutes(user, today, minutes)
        elif sub.startswith("reward_"):
            if policy is None:
                return {"ok": False, "error": "no policy"}
            reward = next((r for r in policy.rewards if r.slug == sub[7:]), None)
            if reward is None:
                return {"ok": False, "error": f"unknown reward {sub[7:]}"}
            if self._gate("grant", "reward", user, pin):
                return {"ok": False}
            ok, why = tk.claim_reward(user, reward, today)
            if ok:
                self.notify_sessions(active, "Bonus time", f"{reward.name}: +{reward.minutes} minutes!", "info")
                self._emit_user_event(user, "reward_claimed", reward=reward.name, minutes=reward.minutes)
            result = {"ok": ok, "error": why or None}
        elif sub == "limit_enabled":
            if not on and self._gate("disable", "limit_enabled_off", user, pin):
                return {"ok": False}
            tk.set_limit_enabled(user, on)
        elif sub == "blocked":
            if not on and self._gate("unblock", "unblock", user, pin):
                return {"ok": False}
            tk.set_manually_blocked(user, on)
        elif sub == "bedtime_extension":
            minutes = int(number())
            if minutes > tk.bedtime_extension_minutes(user, today) and self._gate("bedtime", "bedtime_extension", user, pin):
                return {"ok": False}
            tk.set_bedtime_extension(user, today, minutes)
        elif sub == "extend_bedtime_30":
            if self._gate("bedtime", "extend_bedtime", user, pin):
                return {"ok": False}
            tk.extend_bedtime(user, today, 30)
        elif sub == "focus_mode":
            if on:
                minutes = int(self.store.get_setting(user, "focus_minutes", 0) or 0) or None
                tk.start_focus(user, now, minutes)
                self.notify_sessions(active, "Focus mode", policy.focus.message if policy else "Focus mode is on.", "info")
            else:
                if self._gate("focus_off", "focus_off", user, pin):
                    return {"ok": False}
                tk.stop_focus(user, now)
        elif sub == "focus_minutes":
            self.store.set_setting(user, "focus_minutes", int(number()))
        elif sub == "breaks_enabled":
            tk.set_breaks_enabled(user, on)
        elif sub.startswith("rule_") and policy is not None:
            return self._dispatch_rule(user, policy, sub[5:], text, today, pin)
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
        else:
            log.warning("unknown user command %s for %s", sub, user)
            return {"ok": False, "error": f"unknown user command {sub}"}
        self._users[user].last_payload = None  # force republish
        return result

    def _dispatch_rule(self, user: str, policy: UserPolicy, rest: str, text: str, today: date, pin: Any) -> dict[str, Any]:
        for rule in sorted(policy.rules, key=lambda r: len(r.slug), reverse=True):
            if rest == rule.slug + "_enabled":
                on = text.upper() == "ON"
                if not on and self._gate("disable", "rule_off", user, pin):
                    return {"ok": False}
                self.rules.set_rule_enabled(user, rule.slug, on)
                break
            if rest == rule.slug + "_limit_today":
                minutes = int(float(text))
                current = self.rules.rule_limit_today(user, rule, today)
                if (current is None or minutes > current) and self._gate("grant", "rule_limit", user, pin):
                    return {"ok": False}
                self.rules.set_rule_limit_today(user, rule.slug, today, None if minutes >= 1440 else minutes)
                break
        else:
            return {"ok": False, "error": f"unknown rule command {rest}"}
        self._users[user].last_payload = None
        return {"ok": True}

    def _dispatch_json(self, data: dict[str, Any]) -> dict[str, Any]:
        action = str(data.get("action", "")).lower()
        user = str(data.get("user", "")).lower() or None
        pin = data.get("pin")
        now = self.clock()
        today = now.date()
        log.info("JSON command: %s", {k: v for k, v in data.items() if k not in ("token", "pin")})
        sessions = [s.session_id for s in self._last_sessions if (user is None or s.user == user) and s.logged_in]
        active = [s.session_id for s in self._last_sessions if (user is None or s.user == user) and s.state == "active"]
        if "session" in data:
            sessions = active = [int(data["session"])]
        tk = self.timekeeper
        result: dict[str, Any] = {"ok": True}
        policy = tk.policy_for(user) if user else None

        def need_user() -> Optional[dict[str, Any]]:
            if not user:
                return {"ok": False, "error": "user required"}
            if user not in self._users and policy is None:
                return {"ok": False, "error": f"unknown user {user}"}
            return None

        if action in ("shutdown", "reboot", "sleep", "hibernate", "cancel_shutdown"):
            self._power_action(action, delay=data.get("delay"), message=data.get("message"))
        elif action == "logoff":
            for sid in sessions:
                self._logoff_session(sid)
        elif action == "lock":
            for sid in sessions:
                self._lock_session(sid)
        elif action == "notify":
            self.notify_sessions(sessions, str(data.get("title", "Message")), str(data.get("message", "")), str(data.get("level", "info")))
        elif action == "wol":
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
            if minutes > tk.bonus_seconds(user, today) / 60 and (ref := self._gate("grant", action, user, pin)):
                return ref
            tk.set_bonus_minutes(user, today, minutes)
        elif action == "set_limit":
            if (err := need_user()):
                return err
            minutes = data.get("minutes")
            minutes = None if minutes is None else int(minutes)
            scope = str(data.get("scope", "today"))
            current = tk.effective_limit_minutes(user, today)
            loosening = minutes is None or current is None or minutes > current
            if loosening and (ref := self._gate("grant", action, user, pin)):
                return ref
            if scope == "today":
                tk.set_limit_today(user, today, minutes)
            elif scope == "weekday":
                tk.set_weekday_limit(user, minutes)
            elif scope == "weekend":
                tk.set_weekend_limit(user, minutes)
            elif scope == "weekly":
                tk.set_weekly_limit(user, minutes)
            else:
                return {"ok": False, "error": f"unknown scope {scope}"}
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
            tk.set_bedtime_extension(user, today, 0)
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
            until = tk.start_focus(user, now, data.get("minutes"), allow if isinstance(allow, list) else None)
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
            minutes = int(data.get("minutes", 30))
            if action == "extend_bedtime":
                tk.extend_bedtime(user, today, minutes)
            else:
                tk.set_bedtime_extension(user, today, minutes)
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
            current = self.rules.rule_limit_today(user, rule, today)
            if (minutes is None or current is None or minutes > current) and (ref := self._gate("grant", action, user, pin)):
                return ref
            self.rules.set_rule_limit_today(user, slug, today, minutes)
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
            tk.set_parent_present(bool(data.get("present", True)))
            self._force_republish_all()
        elif action == "pin":
            ok = self.pin.try_unlock(data.get("pin", data.get("value")))
            self._force_republish_all()
            result = {"ok": ok, "error": None if ok else "wrong pin"}
        elif action == "pin_lock":
            self.pin.lock()
            self._force_republish_all()
        elif action == "check_updates":
            if self.winupdate is not None:
                self.winupdate.check_now()
        elif action == "install_updates":
            ok = bool(self.winupdate is not None and self.winupdate.install())
            result = {"ok": ok, "error": None if ok else "install not allowed or unavailable"}
        elif action == "run":
            self._run_custom(slugify(data.get("name", "")))
        elif action == "reload":
            result = self.reload_config()
        elif action == "refresh":
            self.on_mqtt_connected()
        else:
            log.warning("unknown JSON command: %s", data)
            return {"ok": False, "error": f"unknown action {action!r}"}
        if user and user in self._users:
            self._users[user].last_payload = None
        return result

    def _power_action(self, action: str, delay=None, message=None) -> None:
        p = self.cfg.power
        allowed = {"shutdown": p.allow_shutdown, "reboot": p.allow_reboot, "sleep": p.allow_sleep,
                   "hibernate": p.allow_hibernate, "cancel_shutdown": True}
        if not allowed.get(action):
            log.warning("power action %s is disabled in config", action)
            return
        active = [s.session_id for s in self._last_sessions if s.state == "active"]
        if action in ("shutdown", "reboot"):
            d = p.shutdown_delay_seconds if delay is None else int(delay)
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
            if self.mqtt is not None:
                self.mqtt.publish_availability(False)
            ok = self.power.sleep() if action == "sleep" else self.power.hibernate()
            if self.mqtt is not None:
                self.mqtt.publish_availability(True)  # we are back when this runs
            if not ok:
                log.error("%s failed", action)

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
        self._pending_screenshots[req_id] = {"user": user, "session": best.session_id, "ts": time.time()}
        # drop stale requests
        for rid in [r for r, info in self._pending_screenshots.items() if time.time() - info["ts"] > 120]:
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
                log.warning("screenshot for %s failed: %s", user, message["error"])
                self._users[user].last_refusal = {"ts": time.time(), "action": "screenshot", "reason": str(message["error"])}
                return
            try:
                data = base64.b64decode(message.get("data", ""))
            except (ValueError, TypeError):
                log.warning("bad screenshot payload from session %s", session_id)
                return
            fmt = str(message.get("format", self.cfg.screenshots.format))
            meta = {"ts": datetime.now().isoformat(timespec="seconds"), "session": session_id, "format": fmt,
                    "width": message.get("width"), "height": message.get("height"), "bytes": len(data)}
            view = self._ensure_user(user)
            view.screenshot = meta
            view.last_payload = None
            if self.mqtt is not None and self.entities is not None:
                self.mqtt.publish(self.entities.screenshot_topic(self._slug_for(user)), data, retain=self.cfg.screenshots.retain)
            if self.cfg.screenshots.save_to_disk:
                self._save_screenshot(user, data, fmt)
            self.store.log_event(user, "screenshot", f"{meta['width']}x{meta['height']} {len(data)} bytes")
            self._emit_user_event(user, "screenshot_taken", bytes=len(data))
        elif kind == "closed":
            log.info("agent in session %s closed %s (%s)", session_id, message.get("name"), message.get("result"))
        else:
            log.debug("agent message %s from session %s ignored", kind, session_id)

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

    def _check_config_reload(self, now_ts: float) -> None:
        if not self.cfg.source_path or now_ts - self._config_checked < CONFIG_POLL_SECONDS:
            return
        self._config_checked = now_ts
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
            except (OSError, ConfigError, yaml.YAMLError) as exc:
                log.error("config reload failed: %s", exc)
                self.store.log_event(None, "config_reload_failed", str(exc)[:300])
                self._config_mtime = self._stat_config()
                return {"ok": False, "error": str(exc)}
        old = self.cfg
        restart = _restart_sections(old, new_cfg)
        with self._lock:
            self.cfg = new_cfg
            self.timekeeper.update_config(new_cfg.time_limits, new_cfg.build_default_policy)
            self.classifier = ActivityClassifier(new_cfg.activity)
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
            if self.fleet is not None:
                self.fleet.cfg = new_cfg.fleet
            self.timekeeper.external_usage = self.fleet.external_usage if self.fleet else None
            for policy in new_cfg.time_limits.users:
                if policy.key not in self._users:
                    self._slug_to_user[policy.slug] = policy.key
                    self._users[policy.key] = _UserView(user=policy.key)
            if self.launcher is not None and agent_settings(old) != agent_settings(new_cfg):
                self.launcher.agent_settings = agent_settings(new_cfg)
                if self.agent_server is not None:
                    self.agent_server.broadcast({"type": "exit"})  # relaunched with the new settings
            self._restart_needed = sorted(set(self._restart_needed) | set(restart))
            self._config_mtime = self._stat_config()
            self.on_mqtt_connected()  # republish discovery with the new entity set
        log.info("configuration reloaded%s", f" (restart needed for: {', '.join(restart)})" if restart else "")
        self.store.log_event(None, "config_reloaded", ", ".join(restart))
        self._emit_pc_event("config_reloaded", restart_needed=restart or None)
        return {"ok": True, "restart_needed": restart}

    # ------------------------------------------------------------------
    # local API backend
    # ------------------------------------------------------------------
    def api_health(self) -> dict[str, Any]:
        return {"ok": True, "version": __version__, "device": self.cfg.device.id, "uptime_seconds": round(time.time() - self._started_at),
                "ticks": self._stats["ticks"], "mqtt_connected": bool(getattr(self.mqtt, "connected", False)),
                "restart_needed": self._restart_needed}

    def api_status(self) -> dict[str, Any]:
        with self._lock:
            if self._last_pc_snapshot is not None and time.time() - self._last_pc_publish < 5:
                return dict(self._last_pc_snapshot)
            return self.pc_payload(self._last_sessions)

    def api_users(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            now = self.clock()
            by_user: dict[str, list[SessionInfo]] = {}
            for s in self._last_sessions:
                by_user.setdefault(s.user, []).append(s)
            return {u: self.user_payload(u, now, by_user.get(u, [])) for u in self._users}

    def api_user(self, user: str) -> Optional[dict[str, Any]]:
        user = user.lower()
        if user not in self._users and self.timekeeper.policy_for(user) is None:
            return None
        return self.api_users().get(user) or self.user_payload(user, self.clock(), [])

    def api_events(self, n: int, user: Optional[str] = None) -> list[dict[str, Any]]:
        return self.store.recent_events(n, user=user)

    def api_command(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            result = self._dispatch_json(data)
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
            raw = yaml.safe_load(text) or {}
            if not isinstance(raw, dict):
                raise ConfigError("top level must be a mapping")
            new_cfg = Config.from_dict(raw)
        except (yaml.YAMLError, ConfigError, ValueError, TypeError) as exc:
            return {"ok": False, "error": f"invalid configuration: {exc}"}
        new_cfg.source_path = path
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except OSError as exc:
            return {"ok": False, "error": f"could not write {path}: {exc}"}
        return self.reload_config(new_cfg)

    def api_reload(self) -> dict[str, Any]:
        return self.reload_config()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _hhmm(minute: Optional[int]) -> Optional[str]:
    if minute is None:
        return None
    minute = int(minute) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _restart_sections(old: Config, new: Config) -> list[str]:
    """Which changed settings need a service restart to take effect."""
    changed: list[str] = []
    for section in ("mqtt", "storage", "logging"):
        if asdict(getattr(old, section)) != asdict(getattr(new, section)):
            changed.append(section)
    for path in ("agent.ipc_host", "agent.ipc_port", "agent.enabled", "api.host", "api.port", "api.enabled", "api.token"):
        sec, key = path.split(".")
        if getattr(getattr(old, sec), key) != getattr(getattr(new, sec), key):
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
    }


def build_orchestrator(cfg: Config) -> Orchestrator:
    """Wire the real backends (Windows) or graceful no-ops (elsewhere)."""
    from .devices import DeviceMonitor
    from .ha import MqttBridge
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
    fleet_topics = orch.fleet.topics() if orch.fleet is not None else []
    orch.mqtt = MqttBridge(cfg.mqtt, cfg.device.id, on_command=orch.on_command, on_connected=orch.on_mqtt_connected,
                           extra_topics=fleet_topics, on_peer=orch.on_peer_message if orch.fleet else None)
    orch.entities = EntityFactory(cfg, base_topic=orch.mqtt.base, availability_topic=orch.mqtt.availability_topic,
                                  state_topic=orch.mqtt.state_topic, discovery_prefix=orch.mqtt.discovery_prefix)
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
    return orch
