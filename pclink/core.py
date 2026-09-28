"""The orchestrator: main loop of the service.

Every tick it enumerates sessions, merges agent reports, lets the TimeKeeper
account time and decide on actions, executes them, attributes activity
(gaming / browsing / sites), publishes state to Home Assistant and keeps the
per-session agents alive.  All Windows specific backends are injected so the
loop can be exercised with fakes on any platform.
"""
from __future__ import annotations

import json
import logging
import queue
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Callable, Optional

from . import __version__
from .activity import ActivityClassifier
from .config import Config, CustomCommand, UserPolicy
from .entities import Entity, EntityFactory
from .models import Activity, AgentReport, SessionInfo
from .power import PowerControl, send_wol
from .store import Store
from .timekeeper import Action, TimeKeeper, TimeStatus
from .util import human_minutes, slugify
from .winsessions import SESSION_EVENT_NAMES, WTS_SESSION_LOGOFF

log = logging.getLogger(__name__)

STATUS_ORDER = {"active": 0, "idle": 1, "locked": 2, "disconnected": 3, "logged_off": 4}
FRESH_LOGON_GRACE = 15


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


class Orchestrator:
    def __init__(self, cfg: Config, *, store: Optional[Store] = None, sessions=None, blocker=None, launcher=None,
                 power: Optional[PowerControl] = None, mqtt=None, telemetry=None, hwsensors=None,
                 agent_server=None, clock: Optional[Callable[[], datetime]] = None):
        self.cfg = cfg
        self.store = store or Store(cfg.storage.path)
        self.timekeeper = TimeKeeper(cfg.time_limits, self.store, cfg.build_default_policy)
        self.classifier = ActivityClassifier(cfg.activity)
        self.sessions = sessions
        self.blocker = blocker
        self.launcher = launcher
        self.power = power or PowerControl(cfg.power.shutdown_delay_seconds)
        self.mqtt = mqtt
        self.telemetry = telemetry
        self.hwsensors = hwsensors
        self.agent_server = agent_server
        self.clock = clock or datetime.now
        self.entities: Optional[EntityFactory] = None
        if mqtt is not None:
            self.entities = EntityFactory(
                cfg, base_topic=mqtt.base, availability_topic=mqtt.availability_topic,
                state_topic=mqtt.state_topic, discovery_prefix=mqtt.discovery_prefix,
            )
        # runtime state -----------------------------------------------------
        self._reports: dict[int, AgentReport] = {}
        self._agents: dict[int, _AgentSlot] = {}
        self._users: dict[str, _UserView] = {}
        self._slug_to_user: dict[str, str] = {}
        self._block_applied: dict[str, bool] = {}
        self._block_asserted_at: float = 0.0
        self._commands: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_pc_publish = 0.0
        self._last_flush = time.monotonic()
        self._last_purge: Optional[date] = None
        self._discovered: set[str] = set()
        self._pc_discovery_signature: Optional[str] = None
        self._last_sessions: list[SessionInfo] = []
        self._started_at = time.time()
        self._stats = {"ticks": 0, "actions": 0}
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
        if self.hwsensors is not None:
            try:
                self.hwsensors.start()
            except Exception:  # noqa: BLE001
                log.exception("hardware sensors failed to start")
        if self.agent_server is not None:
            try:
                self.agent_server.start()
            except Exception:  # noqa: BLE001
                log.exception("agent IPC server failed to start; agents disabled")
                self.agent_server = None
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
        if self.hwsensors is not None:
            self.hwsensors.stop()
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

    def on_mqtt_connected(self) -> None:
        self._discovered.clear()
        self._pc_discovery_signature = None
        for view in self._users.values():
            view.discovered = False
            view.last_payload = None
        self._last_pc_publish = 0.0
        self._wake.set()

    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        now_ts = time.time()
        self._stats["ticks"] += 1
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
        self._reassert_blocks(now, now_ts)
        self._push_agent_status(now, by_user)
        if self.mqtt is not None:
            self._publish_users(now, by_user, now_ts)
            if now_ts - self._last_pc_publish >= self.cfg.telemetry.interval_seconds:
                self._publish_pc(sessions)
                self._last_pc_publish = now_ts
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
        for sid in list(self._reports):
            if sid not in present:
                self._reports.pop(sid, None)
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

    # -- actions ----------------------------------------------------------------
    def _execute(self, action: Action, by_user: dict[str, list[SessionInfo]]) -> None:
        sessions = by_user.get(action.user, [])
        targets = action.sessions or [s.session_id for s in sessions if s.logged_in]
        log.info("action %s for %s (sessions %s) %s", action.kind, action.user, targets, action.title)
        if action.kind == "warn":
            self.notify_sessions(targets, action.title, action.message, action.level)
        elif action.kind == "logoff":
            self.notify_sessions(targets, action.title, action.message, "critical")
            for sid in targets:
                self._logoff_session(sid)
        elif action.kind == "lock":
            for sid in targets:
                self._lock_session(sid)
        elif action.kind == "shutdown":
            others = [s for u, ss in by_user.items() if u != action.user for s in ss if s.state == "active"]
            if others:
                log.warning("shutdown requested for %s but other users are active (%s); logging off instead",
                            action.user, sorted({s.user for s in others}))
                for sid in targets:
                    self._logoff_session(sid)
            else:
                self.power.shutdown(message="Computer time is over.", delay=5)
        elif action.kind == "block":
            self._apply_block(action.user, True)
        elif action.kind == "unblock":
            self._apply_block(action.user, False)

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
        candidates.sort(key=lambda s: (bool(s.locked), s.report is None, s.idle_seconds if s.idle_seconds is not None else 1e9))
        return candidates[0]

    def _track_activity(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        today = now.date()
        now_ts = now.timestamp()
        for user, view in self._users.items():
            sessions = by_user.get(user, [])
            best = self._pick_session(sessions)
            if best is None:
                activity = Activity(category="none")
            elif best.locked:
                activity = Activity(category="locked")
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

    # -- agent status (tray tooltip) ---------------------------------------------
    def _push_agent_status(self, now: datetime, by_user: dict[str, list[SessionInfo]]) -> None:
        if self.agent_server is None:
            return
        for user, sessions in by_user.items():
            st = self.timekeeper.status(user, now)
            if st.managed and st.enforcing:
                if st.remaining_seconds is None and st.window_remaining_seconds is None:
                    text = "No time limit today"
                else:
                    text = f"{human_minutes(st.effective_remaining_seconds)} left today"
                if st.manually_blocked:
                    text = "Computer access paused"
                elif not st.in_window and st.window_remaining_seconds is None and st.out_of_time:
                    text = "Outside allowed hours"
            else:
                text = "No time limit"
            view = self._ensure_user(user)
            if view.last_agent_status == text:
                continue
            payload = {"type": "status", "text": text, "remaining_minutes": st.remaining_minutes,
                       "used_minutes": round(st.used_seconds / 60), "limit_minutes": st.limit_minutes}
            sent = False
            for s in sessions:
                if self.agent_server.send(s.session_id, payload):
                    sent = True
            if sent:
                view.last_agent_status = text

    # ------------------------------------------------------------------
    # Home Assistant publishing
    # ------------------------------------------------------------------
    def _publish_entities(self, entities: list[Entity]) -> None:
        assert self.mqtt is not None
        for ent in entities:
            key = f"{ent.component}/{ent.object_id}"
            if key in self._discovered:
                continue
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
        for s in unlocked:
            if s.idle_seconds is None or s.idle_seconds < self.cfg.activity.idle_seconds:
                return "active"
            if s.report is not None and s.report.audio_active and self.cfg.activity.audio_keeps_active:
                return "active"
        return "idle"

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
        unlimited = 1440
        payload: dict[str, Any] = {
            "user": user,
            "name": policy.label if policy else user,
            "managed": st.managed,
            "status": self.user_status(sessions),
            "logged_in": any(s.logged_in for s in sessions),
            "sessions": [s.to_dict() for s in sessions],
            "activity": act.category,
            "activity_details": {"app": act.app, "exe": act.exe, "title": act.title, "domain": act.domain, "url": act.url},
            "app": act.app,
            "domain": act.domain,
            "used_minutes": round(totals.active / 60, 1),
            "gaming_minutes": round(totals.gaming / 60, 1),
            "browsing_minutes": round(totals.browsing / 60, 1),
            "top_games": games,
            "top_apps": dict(list(apps.items())[: self.cfg.activity.top_n]),
            "top_app": next(iter(apps), None),
            "top_sites": sites,
            "history": self.store.history(user, self.cfg.activity.history_days, today),
            "time_limits_enabled": self.timekeeper.global_enabled,
        }
        if st.managed and policy is not None:
            weekday_default = policy.daily_limits.get(0)
            weekend_default = policy.daily_limits.get(5)
            payload.update(
                {
                    "remaining_minutes": st.remaining_minutes,
                    "limit_minutes": st.limit_minutes,
                    "limit_today": unlimited if st.limit_minutes is None else st.limit_minutes,
                    "weekday_limit": st.weekday_limit if st.weekday_limit is not None else (unlimited if weekday_default is None else weekday_default),
                    "weekend_limit": st.weekend_limit if st.weekend_limit is not None else (unlimited if weekend_default is None else weekend_default),
                    "bonus_minutes": round(st.bonus_seconds / 60),
                    "limit_enabled": st.limit_enabled,
                    "blocked": st.manually_blocked,
                    "login_blocked": st.block_desired,
                    "in_allowed_hours": st.in_window,
                    "grace_active": st.grace_active,
                    "limit_details": {
                        "limit_minutes": st.limit_minutes,
                        "bonus_minutes": round(st.bonus_seconds / 60),
                        "used_minutes": round(st.used_seconds / 60, 1),
                        "window_remaining_minutes": None if st.window_remaining_seconds is None else round(st.window_remaining_seconds / 60),
                        "limit_today_override": st.limit_today_override,
                        "warn_at_minutes": policy.warn_at_minutes,
                        "action": policy.action,
                        "schedule": {day: policy.daily_limits.get(i) for i, day in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])},
                        "allowed_hours": {
                            day: ", ".join(f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in policy.allowed_windows.get(i, [])) or "any"
                            for i, day in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
                        },
                        "block_method": self.cfg.time_limits.block_method,
                    },
                }
            )
        return payload

    def _publish_users(self, now: datetime, by_user: dict[str, list[SessionInfo]], now_ts: float) -> None:
        assert self.mqtt is not None and self.entities is not None
        for user, view in list(self._users.items()):
            managed = self.timekeeper.is_managed(user)
            if not managed and not self.cfg.activity.track_unmanaged_users and user not in by_user:
                continue
            slug = self._slug_for(user)
            if not view.discovered:
                self._publish_entities(self.entities.user_entities(user, self.timekeeper.policy_for(user), managed))
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
        active = [s for s in sessions if s.state == "active"]
        snap["active_sessions"] = len(active)
        snap["sessions"] = [s.to_dict() for s in sessions]
        users = sorted({s.user for s in sessions if s.logged_in})
        snap["logged_in_users"] = ", ".join(users) if users else "none"
        snap["any_user_active"] = any(not s.locked for s in active)
        snap["time_limits_enabled"] = self.timekeeper.global_enabled
        snap["service_status"] = "running"
        snap["service_info"] = {
            "version": __version__,
            "python": sys.version.split()[0],
            "started": datetime.fromtimestamp(self._started_at).isoformat(timespec="seconds"),
            "ticks": self._stats["ticks"],
            "agents_connected": sorted(self.agent_server.connected_sessions()) if self.agent_server else [],
            "hw_backend": getattr(getattr(self.hwsensors, "backend", None), "name", None),
            "gpu_backend": getattr(getattr(self.telemetry, "gpu", None), "name", None),
            "config": self.cfg.source_path,
            "block_method": self.cfg.time_limits.block_method,
            "managed_users": self.timekeeper.managed_users(),
        }
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
            self._pc_discovery_signature = signature
        self.mqtt.publish(self.mqtt.state_topic, snap, retain=True)

    # ------------------------------------------------------------------
    # commands from Home Assistant
    # ------------------------------------------------------------------
    def _drain_commands(self) -> None:
        while True:
            try:
                object_id, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self.dispatch(object_id, payload)
            except Exception:  # noqa: BLE001
                log.exception("command %s failed", object_id)

    def dispatch(self, object_id: str, payload: Any) -> None:
        if object_id == "cmd":
            self._dispatch_json(payload if isinstance(payload, dict) else {})
            return
        text = payload.strip() if isinstance(payload, str) else str(payload)
        log.info("HA command %s = %r", object_id, text[:100])
        now = self.clock()
        if object_id in ("shutdown", "reboot", "sleep", "hibernate", "cancel_shutdown"):
            self._power_action(object_id)
        elif object_id == "lock_all":
            for s in self._last_sessions:
                if s.state == "active":
                    self._lock_session(s.session_id)
        elif object_id == "time_limits_enabled":
            self.timekeeper.set_global_enabled(text.upper() == "ON")
        elif object_id == "broadcast_message":
            if text:
                self.notify_sessions([s.session_id for s in self._last_sessions if s.state == "active"], "Message", text, "info")
        elif object_id.startswith("cmd_"):
            self._run_custom(object_id[4:])
        elif object_id.startswith("user_"):
            self._dispatch_user(object_id[5:], text, now)
        else:
            log.warning("unknown command entity %s", object_id)

    def _dispatch_user(self, rest: str, text: str, now: datetime) -> None:
        user = sub = None
        for slug in sorted(self._slug_to_user, key=len, reverse=True):
            if rest.startswith(slug + "_"):
                user, sub = self._slug_to_user[slug], rest[len(slug) + 1:]
                break
        if user is None:
            log.warning("command for unknown user: %s", rest)
            return
        today = now.date()
        tk = self.timekeeper
        sessions = [s.session_id for s in self._last_sessions if s.user == user and s.logged_in]
        if sub == "limit_today":
            tk.set_limit_today(user, today, int(float(text)))
        elif sub == "weekday_limit":
            tk.set_weekday_limit(user, int(float(text)))
        elif sub == "weekend_limit":
            tk.set_weekend_limit(user, int(float(text)))
        elif sub == "bonus_today":
            tk.set_bonus_minutes(user, today, float(text))
        elif sub == "add_15":
            tk.add_bonus_minutes(user, today, 15)
        elif sub == "add_30":
            tk.add_bonus_minutes(user, today, 30)
        elif sub == "limit_enabled":
            tk.set_limit_enabled(user, text.upper() == "ON")
        elif sub == "blocked":
            tk.set_manually_blocked(user, text.upper() == "ON")
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
        self._users[user].last_payload = None  # force republish

    def _dispatch_json(self, data: dict[str, Any]) -> None:
        action = str(data.get("action", "")).lower()
        user = str(data.get("user", "")).lower() or None
        now = self.clock()
        today = now.date()
        log.info("HA JSON command: %s", {k: v for k, v in data.items() if k != "token"})
        sessions = [s.session_id for s in self._last_sessions if (user is None or s.user == user) and s.logged_in]
        if "session" in data:
            sessions = [int(data["session"])]
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
        elif action == "add_minutes" and user:
            self.timekeeper.add_bonus_minutes(user, today, float(data.get("minutes", 15)))
        elif action == "set_bonus" and user:
            self.timekeeper.set_bonus_minutes(user, today, float(data.get("minutes", 0)))
        elif action == "set_limit" and user:
            minutes = data.get("minutes")
            minutes = None if minutes is None else int(minutes)
            scope = str(data.get("scope", "today"))
            if scope == "today":
                self.timekeeper.set_limit_today(user, today, minutes)
            elif scope == "weekday":
                self.timekeeper.set_weekday_limit(user, minutes)
            elif scope == "weekend":
                self.timekeeper.set_weekend_limit(user, minutes)
        elif action == "clear_overrides" and user:
            self.timekeeper.set_limit_today(user, today, None)
            self.timekeeper.set_weekday_limit(user, None)
            self.timekeeper.set_weekend_limit(user, None)
            self.timekeeper.set_bonus_minutes(user, today, 0)
        elif action in ("block", "unblock") and user:
            self.timekeeper.set_manually_blocked(user, action == "block")
        elif action == "limit_enabled" and user:
            self.timekeeper.set_limit_enabled(user, bool(data.get("enabled", True)))
        elif action == "limits":
            self.timekeeper.set_global_enabled(bool(data.get("enabled", True)))
        elif action == "run":
            self._run_custom(slugify(data.get("name", "")))
        elif action == "refresh":
            self.on_mqtt_connected()
        else:
            log.warning("unknown JSON command: %s", data)
        if user and user in self._users:
            self._users[user].last_payload = None

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


# ----------------------------------------------------------------------
# factory
# ----------------------------------------------------------------------

def agent_settings(cfg: Config) -> dict[str, Any]:
    a = cfg.activity
    return {
        "poll_seconds": cfg.agent.poll_seconds,
        "heartbeat_seconds": cfg.agent.heartbeat_seconds,
        "audio_poll_seconds": cfg.agent.audio_poll_seconds,
        "tray_icon": cfg.agent.tray_icon,
        "browsers": a.browsers,
        "url_method": a.url_method,
        "group_sites_by": a.group_sites_by,
        "audio": a.audio_keeps_active,
        "log_level": cfg.agent.log_level,
        "device_name": cfg.device.name,
    }


def build_orchestrator(cfg: Config) -> Orchestrator:
    """Wire the real backends (Windows) or graceful no-ops (elsewhere)."""
    from .ha import MqttBridge
    from .hwsensors import HardwareSensors
    from .ipc import AgentServer
    from .logonblock import make_blocker
    from .telemetry import Telemetry

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
    orch.mqtt = MqttBridge(cfg.mqtt, cfg.device.id, on_command=orch.on_command, on_connected=orch.on_mqtt_connected)
    orch.entities = EntityFactory(cfg, base_topic=orch.mqtt.base, availability_topic=orch.mqtt.availability_topic,
                                  state_topic=orch.mqtt.state_topic, discovery_prefix=orch.mqtt.discovery_prefix)
    if cfg.agent.enabled and launcher is not None:
        orch.agent_server = AgentServer(
            cfg.agent.ipc_host, cfg.agent.ipc_port, on_report=orch.on_agent_report,
            validate=orch.validate_agent_token, on_connect=orch.on_agent_connected,
            on_disconnect=orch.on_agent_disconnected,
        )
    return orch
