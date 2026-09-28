"""End-to-end-ish tests of the orchestrator using in-memory fakes for all the
Windows backends, so the full tick / enforcement / publish path runs anywhere.
"""
import json
from datetime import datetime

import yaml

from pclink.config import Config
from pclink.core import Orchestrator
from pclink.models import SessionInfo
from pclink.store import Store


class FakeSessions:
    def __init__(self):
        self.sessions = []
        self.logged_off = []
        self.disconnected = []
        self.messages = []

    def list_sessions(self):
        return list(self.sessions)

    def logoff(self, sid, wait=False):
        self.logged_off.append(sid)
        self.sessions = [s for s in self.sessions if s.session_id != sid]

    def disconnect(self, sid):
        self.disconnected.append(sid)

    def send_message(self, sid, title, text, timeout=30, critical=False):
        self.messages.append((sid, title, text))


class FakeBlocker:
    method = "logon_hours"

    def __init__(self):
        self.state = {}

    def block(self, user):
        self.state[user] = True
        return True

    def unblock(self, user):
        self.state[user] = False
        return True

    def is_blocked(self, user):
        return self.state.get(user)


class FakeMqtt:
    def __init__(self):
        self.base = "pclink/testpc"
        self.availability_topic = "pclink/testpc/availability"
        self.state_topic = "pclink/testpc/state"
        self.discovery_prefix = "homeassistant"
        self.published = {}
        self.discovery = {}

    def user_state_topic(self, slug):
        return f"{self.base}/user/{slug}/state"

    def publish(self, topic, payload, retain=False, qos=None):
        self.published[topic] = payload

    def publish_discovery(self, component, object_id, payload):
        self.discovery[f"{component}/{object_id}"] = payload

    def publish_availability(self, online=True):
        self.published[self.availability_topic] = "online" if online else "offline"

    def start(self):
        pass

    def stop(self):
        pass


def build(clock_value, yaml_text=None):
    cfg = Config.from_dict(yaml.safe_load(yaml_text or """
        device: {name: TestPC}
        telemetry: {interval_seconds: 0}
        time_limits:
          block_method: logon_hours
          users:
            - name: kevin
              daily_limit_minutes: 60
              action: logoff
              grace_seconds: 60
              count_idle: false
            - name: dad
              unlimited: true
    """))
    holder = {"now": clock_value}
    sessions = FakeSessions()
    blocker = FakeBlocker()
    mqtt = FakeMqtt()
    orch = Orchestrator(cfg, store=Store(":memory:"), sessions=sessions, blocker=blocker, mqtt=mqtt,
                        clock=lambda: holder["now"])
    orch._holder = holder
    return orch, sessions, blocker, mqtt


def session(user="kevin", sid=2, **kw):
    return SessionInfo(session_id=sid, user=user, state=kw.get("state", "active"),
                       locked=kw.get("locked", False), idle_seconds=kw.get("idle", 0.0))


def test_publishes_pc_and_user_state():
    orch, sessions, blocker, mqtt = build(datetime(2026, 9, 28, 12, 0, 0))
    sessions.sessions = [session()]
    orch.tick()
    assert mqtt.state_topic in mqtt.published
    assert "sensor/cpu_percent" in mqtt.discovery
    user_topic = mqtt.user_state_topic("kevin")
    assert user_topic in mqtt.published
    payload = mqtt.published[user_topic]
    assert payload["managed"] is True
    assert payload["status"] == "active"


def test_out_of_time_blocks_and_logs_off():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    orch.store.add_usage("kevin", now.date(), active=61 * 60)
    sessions.sessions = [session()]
    orch.tick()  # should request a block and start grace (warn)
    assert blocker.state.get("kevin") is True
    # advance past grace
    orch._holder["now"] = now.replace(minute=2)
    orch.tick()
    assert 2 in sessions.logged_off


def test_command_add_minutes_via_json():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    sessions.sessions = [session()]
    orch.tick()
    orch.on_command("cmd", {"action": "add_minutes", "user": "kevin", "minutes": 30})
    orch._drain_commands()
    assert orch.timekeeper.bonus_seconds("kevin", now.date()) == 30 * 60


def test_command_set_limit_and_block_toggle():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    sessions.sessions = [session()]
    orch.tick()
    orch.dispatch("user_kevin_limit_today", "200")
    assert orch.timekeeper.effective_limit_minutes("kevin", now.date()) == 200
    orch.dispatch("user_kevin_blocked", "ON")
    assert orch.timekeeper.manually_blocked("kevin")


def test_shutdown_disabled_config_is_ignored():
    orch, sessions, blocker, mqtt = build(datetime(2026, 9, 28, 12, 0, 0), yaml_text="""
        device: {name: T}
        power: {allow_shutdown: false}
        time_limits: {users: [{name: kevin, daily_limit_minutes: 60}]}
    """)
    called = []
    orch.power.shutdown = lambda *a, **k: called.append(1)
    sessions.sessions = [session()]
    orch.tick()
    orch.dispatch("shutdown", "PRESS")
    assert not called


def test_activity_tracking_accumulates_gaming(monkeypatch):
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    from pclink.models import AgentReport, ForegroundInfo
    rep = AgentReport(session_id=2, ts=now.timestamp(),
                      foreground=ForegroundInfo(name="game.exe", exe="C:\\x\\steamapps\\common\\g\\game.exe"))
    orch.on_agent_report(2, rep)
    s = session()
    s.report = rep
    sessions.sessions = [s]
    orch.tick()
    orch._holder["now"] = now.replace(second=30)
    sessions.sessions = [s]
    s.report = AgentReport(session_id=2, ts=orch._holder["now"].timestamp(),
                           foreground=ForegroundInfo(name="game.exe", exe="C:\\x\\steamapps\\common\\g\\game.exe"))
    orch.on_agent_report(2, s.report)
    orch.tick()
    assert orch.store.usage("kevin", now.date()).gaming > 0
