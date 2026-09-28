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
        self.events = []
        self.connected = True

    def user_state_topic(self, slug):
        return f"{self.base}/user/{slug}/state"

    def publish(self, topic, payload, retain=False, qos=None):
        self.published[topic] = payload

    def publish_discovery(self, component, object_id, payload, node_id=None):
        self.discovery[f"{node_id + ':' if node_id else ''}{component}/{object_id}"] = payload

    def publish_event(self, path, event):
        self.events.append((path, event))

    def publish_availability(self, online=True):
        self.published[self.availability_topic] = "online" if online else "offline"

    def start(self):
        pass

    def stop(self):
        pass


class FakeAgentServer:
    """Records what the service sends to each session's agent."""

    def __init__(self):
        self.sent = []
        self.connected = set()

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, sid, message):
        if sid not in self.connected:
            return False
        self.sent.append((sid, message))
        return True

    def broadcast(self, message):
        for sid in self.connected:
            self.sent.append((sid, message))
        return len(self.connected)

    def connected_sessions(self):
        return {sid: {} for sid in self.connected}

    def messages(self, kind):
        return [(sid, m) for sid, m in self.sent if m.get("type") == kind]


def build(clock_value, yaml_text=None, agent=False):
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
    agent_server = FakeAgentServer() if agent else None
    orch = Orchestrator(cfg, store=Store(":memory:"), sessions=sessions, blocker=blocker, mqtt=mqtt,
                        clock=lambda: holder["now"], agent_server=agent_server)
    orch._holder = holder
    orch._agent = agent_server
    return orch, sessions, blocker, mqtt


def session(user="kevin", sid=2, **kw):
    return SessionInfo(session_id=sid, user=user, state=kw.get("state", "active"),
                       locked=kw.get("locked", False), idle_seconds=kw.get("idle", 0.0))


def report(sid=2, name="chrome.exe", domain=None, category=None, idle=0.0, ts=None, **kw):
    from pclink.models import AgentReport, ForegroundInfo
    fg = ForegroundInfo(pid=100, name=name, exe=kw.pop("exe", ""), title=kw.pop("title", ""), fullscreen=kw.pop("fullscreen", False)) if name else None
    return AgentReport(session_id=sid, ts=ts if ts is not None else 0.0, idle_seconds=idle, foreground=fg, domain=domain, **kw)


def attach(orch, sessions, sid, rep):
    """Register an agent report for a session, timestamped 'now' so it is fresh."""
    import time as _t
    rep.ts = _t.time()
    orch.on_agent_report(sid, rep)


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


# ---------------------------------------------------------------------------
# presence, rules, focus, breaks, PIN, rewards, events, reload, screenshots, fleet
# ---------------------------------------------------------------------------

def test_user_payload_carries_presence_and_events_on_transitions():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    s = session()
    attach(orch, sessions, 2, report(idle=5))
    sessions.sessions = [s]
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["presence"] == "active" and payload["status"] == "active"
    assert payload["presence_details"]["reason"] == "recent input"
    assert ("user/kevin", "logged_in") not in mqtt.events  # first sight is not a transition
    # monitor goes to sleep -> away, status 'away', event emitted
    attach(orch, sessions, 2, report(idle=100, display_state="off"))
    orch._holder["now"] = now.replace(second=10)
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["presence"] == "away" and payload["status"] == "away"
    assert ("user/kevin", "presence_away") in mqtt.events
    assert mqtt.published[mqtt.state_topic]["anyone_present"] is False
    # user comes back
    attach(orch, sessions, 2, report(idle=1))
    orch._holder["now"] = now.replace(second=20)
    orch.tick()
    assert ("user/kevin", "presence_back") in mqtt.events
    # log off -> event
    sessions.sessions = []
    orch._holder["now"] = now.replace(second=30)
    orch.tick()
    assert ("user/kevin", "logged_off") in mqtt.events


def test_monitor_off_stops_the_clock_for_count_idle_false():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    s = session()
    sessions.sessions = [s]
    attach(orch, sessions, 2, report(idle=60, display_state="off"))
    orch.tick()
    orch._holder["now"] = now.replace(minute=1)
    attach(orch, sessions, 2, report(idle=120, display_state="off"))
    orch.tick()
    assert orch.store.usage("kevin", now.date()).active == 0
    # audio playing with no input counts (passive)
    attach(orch, sessions, 2, report(idle=700, audio_active=True))
    orch._holder["now"] = now.replace(minute=2)
    orch.tick()
    assert orch.store.usage("kevin", now.date()).active > 0


RULES_YAML = """
device: {name: TestPC}
telemetry: {interval_seconds: 0}
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 600
      rules:
        - name: YouTube
          sites: [youtube.com]
          daily_limit_minutes: 1
        - name: Roblox
          apps: [robloxplayerbeta.exe]
          blocked: true
      focus:
        allow_apps: [winword.exe]
        default_minutes: 30
      breaks: {every_minutes: 1, duration_seconds: 20}
      rewards:
        - {name: Homework, minutes: 20, daily_max: 1}
"""


def test_blocked_app_is_closed_via_agent_and_events_fire():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, RULES_YAML, agent=True)
    orch._agent.connected.add(2)
    sessions.sessions = [session()]
    attach(orch, sessions, 2, report(name="RobloxPlayerBeta.exe"))
    orch.tick()
    closes = orch._agent.messages("close")
    assert closes and closes[0][1]["name"] == "RobloxPlayerBeta.exe" and closes[0][1]["tab"] is False
    assert orch._agent.messages("notify")
    assert ("user/kevin", "app_limit_reached") in mqtt.events and ("user/kevin", "app_closed") in mqtt.events
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["rules"]["roblox"]["state"] == "blocked" and payload["rules"]["youtube"]["state"] == "ok"


def test_site_limit_closes_tab_after_usage_and_can_be_disabled_from_ha():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, RULES_YAML, agent=True)
    orch._agent.connected.add(2)
    sessions.sessions = [session()]
    attach(orch, sessions, 2, report(name="chrome.exe", domain="youtube.com"))
    orch.tick()
    orch._holder["now"] = now.replace(minute=1, second=30)  # 90 s of YouTube > 1 min limit
    orch.tick()
    closes = orch._agent.messages("close")
    assert closes and closes[-1][1]["tab"] is True
    assert orch.store.rule_seconds("kevin", now.date(), "youtube") >= 60
    # disable the rule from HA -> no more closes
    orch._agent.sent.clear()
    orch.dispatch("user_kevin_rule_youtube_enabled", "OFF")
    orch._holder["now"] = now.replace(minute=2)
    orch.tick()
    assert not orch._agent.messages("close")
    assert mqtt.published[mqtt.user_state_topic("kevin")]["rules"]["youtube"]["state"] == "off"
    # and raise its limit for today via the number entity
    orch.dispatch("user_kevin_rule_youtube_enabled", "ON")
    orch.dispatch("user_kevin_rule_youtube_limit_today", "120")
    orch._holder["now"] = now.replace(minute=3)
    orch.tick()
    assert mqtt.published[mqtt.user_state_topic("kevin")]["rules"]["youtube"]["limit_minutes"] == 120


def test_focus_mode_switch_closes_disallowed_apps_and_pauses_budget():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, RULES_YAML, agent=True)
    orch._agent.connected.add(2)
    sessions.sessions = [session()]
    attach(orch, sessions, 2, report(name="game.exe", exe="C:\\x\\steamapps\\common\\g\\game.exe"))
    orch.tick()
    orch.dispatch("user_kevin_focus_mode", "ON")
    orch._holder["now"] = now.replace(second=10)
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["focus_active"] is True and payload["focus_remaining_minutes"] == 30
    assert orch._agent.messages("close")
    assert ("user/kevin", "focus_started") in mqtt.events
    used_before = orch.store.usage("kevin", now.date()).active
    orch._holder["now"] = now.replace(second=40)
    orch.tick()
    assert orch.store.usage("kevin", now.date()).active == used_before  # focus pauses the budget
    orch.dispatch("user_kevin_focus_mode", "OFF")
    orch._holder["now"] = now.replace(second=50)
    orch.tick()
    assert ("user/kevin", "focus_ended") in mqtt.events
    assert mqtt.published[mqtt.user_state_topic("kevin")]["focus_active"] is False


def test_break_reminder_balloon_and_event():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, RULES_YAML, agent=True)
    orch._agent.connected.add(2)
    sessions.sessions = [session()]
    attach(orch, sessions, 2, report(name="notepad.exe", idle=1))
    orch.tick()
    orch._holder["now"] = now.replace(minute=1, second=5)
    orch.tick()
    notifies = [m for _, m in orch._agent.messages("notify") if "break" in m["title"].lower()]
    assert notifies and ("user/kevin", "break_due") in mqtt.events
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["breaks_enabled"] is True and payload["break_details"]["reminders_today"] == 1
    orch.dispatch("user_kevin_breaks_enabled", "OFF")
    orch._holder["now"] = now.replace(minute=3)
    orch.tick()
    assert mqtt.published[mqtt.user_state_topic("kevin")]["breaks_enabled"] is False


def test_reward_button_grants_minutes_once_per_day():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, RULES_YAML)
    sessions.sessions = [session()]
    orch.tick()
    assert orch.dispatch("user_kevin_reward_homework", "PRESS")["ok"]
    assert orch.timekeeper.bonus_seconds("kevin", now.date()) == 20 * 60
    r = orch.dispatch("user_kevin_reward_homework", "PRESS")
    assert not r["ok"] and "already" in r["error"]
    assert ("user/kevin", "reward_claimed") in mqtt.events
    orch.dispatch("user_kevin_add_minutes", "7")
    assert orch.timekeeper.bonus_seconds("kevin", now.date()) == 27 * 60
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["earned_minutes"] == 20 and payload["add_minutes"] == 0


PIN_YAML = """
device: {name: TestPC}
telemetry: {interval_seconds: 0}
time_limits:
  parent_pin: "2468"
  pin_timeout_seconds: 300
  users:
    - name: kevin
      daily_limit_minutes: 60
      bedtime: "21:00"
"""


def test_parent_pin_gates_grants_but_not_tightening():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, PIN_YAML)
    sessions.sessions = [session()]
    orch.tick()
    # loosening without PIN is refused and recorded
    r = orch.dispatch("user_kevin_add_15", "PRESS")
    assert not r["ok"] and orch.timekeeper.bonus_seconds("kevin", now.date()) == 0
    assert ("pc", "pin_wrong") not in mqtt.events
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["limit_details"]["last_refusal"]["reason"] == "parent pin required"
    assert mqtt.published[mqtt.state_topic]["parent_unlocked"] is False
    # tightening never needs the PIN
    assert orch.dispatch("user_kevin_blocked", "ON")["ok"] and orch.timekeeper.manually_blocked("kevin")
    assert not orch.dispatch("user_kevin_blocked", "OFF")["ok"]
    # wrong PIN, then the right one via the text entity
    orch.dispatch("parent_pin", "0000")
    assert ("pc", "pin_wrong") in mqtt.events
    orch.dispatch("parent_pin", "2468")
    assert ("pc", "parent_unlocked") in mqtt.events
    assert orch.dispatch("user_kevin_add_15", "PRESS")["ok"]
    assert orch.dispatch("user_kevin_blocked", "OFF")["ok"]
    orch.tick()
    assert mqtt.published[mqtt.state_topic]["parent_unlocked"] is True
    assert mqtt.published[mqtt.state_topic]["parent_pin_state"] == ""
    # inline PIN in a JSON command works even after the unlock window
    orch.pin.lock()
    assert not orch.dispatch("cmd", {"action": "add_minutes", "user": "kevin", "minutes": 5})["ok"]
    assert orch.dispatch("cmd", {"action": "add_minutes", "user": "kevin", "minutes": 5, "pin": "2468"})["ok"]
    # bedtime extension is PIN gated, parent presence is not
    orch.pin.lock()
    assert not orch.dispatch("user_kevin_extend_bedtime_30", "PRESS")["ok"]
    assert orch.dispatch("parent_present", "ON")["ok"] and orch.timekeeper.parent_present


def test_bedtime_payload_and_extension():
    night = datetime(2026, 9, 28, 21, 30, 0)
    orch, sessions, blocker, mqtt = build(night, PIN_YAML)
    orch.pin.try_unlock("2468")
    sessions.sessions = [session()]
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["past_bedtime"] is True and payload["bedtime"] == "21:00" and payload["limiting_factor"] == "bedtime"
    assert blocker.state.get("kevin") is True
    assert ("user/kevin", "bedtime") in mqtt.events
    orch.dispatch("user_kevin_bedtime_extension", "60")
    orch._holder["now"] = night.replace(second=5)
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["past_bedtime"] is False and payload["bedtime"] == "22:00" and payload["bedtime_extension_minutes"] == 60
    assert blocker.state.get("kevin") is False


def test_weekly_limit_number_entity_and_payload():
    now = datetime(2026, 9, 30, 12, 0, 0)  # Wednesday
    orch, sessions, blocker, mqtt = build(now)
    orch.store.add_usage("kevin", datetime(2026, 9, 28).date(), active=50 * 60)
    orch.store.add_usage("kevin", datetime(2026, 9, 29).date(), active=50 * 60)
    sessions.sessions = [session()]
    orch.dispatch("user_kevin_weekly_limit", "120")
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["weekly_limit"] == 120 and payload["weekly_used_minutes"] >= 100
    assert payload["weekly_remaining_minutes"] <= 20
    orch.dispatch("user_kevin_weekly_limit", "0")
    orch.tick()
    assert mqtt.published[mqtt.user_state_topic("kevin")]["weekly_limit"] == 0


def test_screenshot_flow_with_fake_agent(tmp_path):
    import base64
    now = datetime(2026, 9, 28, 12, 0, 0)
    yaml_text = """
        device: {name: TestPC}
        telemetry: {interval_seconds: 0}
        storage: {path: "%s"}
        screenshots: {enabled: true, require_pin: false, save_to_disk: true, keep: 1}
        time_limits:
          users:
            - {name: kevin, daily_limit_minutes: 60}
            - {name: dad, unlimited: true}
    """ % str(tmp_path / "db.sqlite").replace("\\", "/")
    orch, sessions, blocker, mqtt = build(now, yaml_text, agent=True)
    orch._agent.connected.add(2)
    sessions.sessions = [session(), session(user="dad", sid=3)]
    orch.tick()
    assert "button/user_kevin_screenshot" in mqtt.discovery and "camera/user_kevin_screenshot_image" in mqtt.discovery
    assert "camera/user_dad_screenshot_image" not in mqtt.discovery  # parents are never captured by default
    assert not orch.dispatch("user_dad_screenshot", "PRESS")["ok"]
    r = orch.dispatch("user_kevin_screenshot", "PRESS")
    assert r["ok"]
    req = orch._agent.messages("screenshot")[0][1]
    assert req["id"] == r["id"] and req["format"] == "jpeg"
    # the agent answers
    orch.on_agent_message(2, {"type": "screenshot", "id": req["id"], "format": "jpeg", "width": 640, "height": 360,
                              "data": base64.b64encode(b"JPEGDATA").decode()})
    orch.tick()
    assert mqtt.published["pclink/testpc/user/kevin/screenshot"] == b"JPEGDATA"
    assert ("user/kevin", "screenshot_taken") in mqtt.events
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["screenshot"]["width"] == 640 and payload["screenshot"]["bytes"] == 8
    saved = list((tmp_path / "screenshots").iterdir())
    assert len(saved) == 1 and saved[0].name.startswith("kevin-")
    # an unsolicited answer is ignored
    orch.on_agent_message(3, {"type": "screenshot", "id": "nope", "data": ""})
    orch.tick()


def test_volume_mute_launch_close_commands_reach_agents():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, """
        device: {name: TestPC}
        telemetry: {interval_seconds: 0}
        time_limits: {users: [{name: kevin, daily_limit_minutes: 60}]}
        apps:
          - {name: Minecraft, launch: 'C:\\Games\\MinecraftLauncher.exe', process: javaw.exe}
    """, agent=True)
    orch._agent.connected.add(2)
    sessions.sessions = [session()]
    attach(orch, sessions, 2, report(name="notepad.exe", volume=40, muted=False))
    orch.tick()
    assert "button/launch_minecraft" in mqtt.discovery and "button/close_minecraft" in mqtt.discovery
    assert "number/user_kevin_volume" in mqtt.discovery
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["volume"] == 40 and payload["muted"] is False
    orch.dispatch("user_kevin_volume", "70")
    orch.dispatch("user_kevin_muted", "ON")
    orch.dispatch("launch_minecraft", "PRESS")
    orch.dispatch("close_minecraft", "PRESS")
    kinds = [m["type"] for _, m in orch._agent.sent]
    assert "volume" in kinds and "mute" in kinds and "run" in kinds and "close" in kinds
    assert orch._agent.messages("volume")[0][1]["level"] == 70
    assert orch._agent.messages("close")[0][1]["name"] == "javaw.exe"
    assert not orch.dispatch("cmd", {"action": "close_app", "process": "explorer.exe"})["ok"]


def test_device_triggers_and_new_entities_are_discovered():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, RULES_YAML)
    sessions.sessions = [session()]
    orch.tick()
    assert "device_automation/trigger_kevin_time_up" in mqtt.discovery
    trig = mqtt.discovery["device_automation/trigger_kevin_time_up"]
    assert trig["automation_type"] == "trigger" and trig["topic"].endswith("/event/user/kevin") and trig["payload"] == "time_up"
    assert "device_automation/trigger_pc_started" in mqtt.discovery
    assert ("pc", "started") in mqtt.events
    for oid in ("sensor/user_kevin_presence", "sensor/user_kevin_rule_youtube", "switch/user_kevin_rule_roblox_enabled",
                "number/user_kevin_rule_youtube_limit_today", "switch/user_kevin_focus_mode", "switch/user_kevin_breaks_enabled",
                "button/user_kevin_reward_homework", "number/user_kevin_add_minutes", "number/user_kevin_weekly_limit",
                "switch/parent_present", "button/reload_config", "binary_sensor/anyone_present"):
        assert oid in mqtt.discovery, oid
    tpl = mqtt.discovery["sensor/user_kevin_rule_youtube"]["value_template"]
    assert "value_json['rules']['youtube']['state']" in tpl


def test_hot_reload_applies_new_limits_and_flags_restart_sections(tmp_path):
    now = datetime(2026, 9, 28, 12, 0, 0)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("""
device: {name: TestPC}
telemetry: {interval_seconds: 0}
time_limits:
  users:
    - {name: kevin, daily_limit_minutes: 60}
""")
    orch, sessions, blocker, mqtt = build(now)
    orch.cfg.source_path = str(cfg_path)
    orch._config_mtime = orch._stat_config()
    sessions.sessions = [session()]
    orch.tick()
    assert orch.timekeeper.status("kevin", now).limit_minutes == 60
    cfg_path.write_text("""
device: {name: TestPC}
telemetry: {interval_seconds: 0}
mqtt: {host: other-broker}
time_limits:
  users:
    - {name: kevin, daily_limit_minutes: 30}
    - {name: sarah, daily_limit_minutes: 45}
""")
    result = orch.reload_config()
    assert result["ok"] and result["restart_needed"] == ["mqtt"]
    assert orch.timekeeper.status("kevin", now).limit_minutes == 30
    assert "sarah" in orch._users
    orch.tick()
    assert mqtt.user_state_topic("sarah") in mqtt.published
    assert ("pc", "config_reloaded") in mqtt.events
    assert mqtt.published[mqtt.state_topic]["service_info"]["restart_needed"] == ["mqtt"]
    # invalid file is rejected and the old config stays
    cfg_path.write_text("time_limits: {block_method: nuke}")
    result = orch.reload_config()
    assert not result["ok"] and orch.timekeeper.status("kevin", now).limit_minutes == 30
    # writing through the API validates first
    bad = orch.api_config_write("time_limits: {users: [{name: x, action: explode}]}")
    assert not bad["ok"] and "invalid" in bad["error"]
    good = orch.api_config_write("device: {name: TestPC}\ntime_limits: {users: [{name: kevin, daily_limit_minutes: 90}]}\n")
    assert good["ok"] and orch.timekeeper.status("kevin", now).limit_minutes == 90
    assert "90" in cfg_path.read_text()


def test_api_backend_methods_and_json_command_results():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    sessions.sessions = [session()]
    orch.tick()
    assert orch.api_health()["ok"]
    assert "cpu_percent" in orch.api_status() or "active_sessions" in orch.api_status()
    users = orch.api_users()
    assert users["kevin"]["managed"] and users["dad"]["managed"] is False
    assert orch.api_user("KEVIN")["user"] == "kevin" and orch.api_user("nobody") is None
    assert orch.api_command({"action": "add_minutes", "user": "kevin", "minutes": 10}) == {"ok": True}
    r = orch.api_command({"action": "bogus"})
    assert not r["ok"] and "unknown action" in r["error"]
    r = orch.api_command({"action": "add_minutes"})
    assert not r["ok"] and r["error"] == "user required"
    assert orch.api_command({"action": "focus", "user": "kevin", "minutes": 10})["ok"]
    assert orch.api_command({"action": "focus_off", "user": "kevin"})["ok"]
    assert any(e["kind"] == "bonus" for e in orch.api_events(20, "kevin"))


def test_fleet_rollup_published_by_leader_and_shared_budget():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, """
        device: {name: aaa-pc}
        telemetry: {interval_seconds: 0}
        time_limits:
          shared_budget: true
          users: [{name: kevin, daily_limit_minutes: 60}]
    """)
    assert orch.fleet is not None
    orch.on_peer_message("pclink/zzz-laptop/availability", b"online")
    orch.on_peer_message("pclink/zzz-laptop/user/kevin/state", json.dumps({
        "user": "kevin", "name": "Kevin", "date": now.date().isoformat(), "used_minutes": 45, "status": "active",
        "presence": "active", "logged_in": True, "managed": True, "remaining_minutes": 15,
        "history": [{"date": now.date().isoformat(), "active_minutes": 45}],
    }).encode())
    sessions.sessions = [session()]
    orch.store.add_usage("kevin", now.date(), active=10 * 60)
    orch.tick()
    payload = mqtt.published[mqtt.user_state_topic("kevin")]
    assert payload["remaining_minutes"] == 5  # 60 - 10 local - 45 on the laptop
    assert payload["weekly_details"]["other_pcs_today_minutes"] == 45
    # aaa-pc sorts before zzz-laptop -> we are leader and publish the roll-up
    assert orch.fleet.is_leader()
    rollup = mqtt.published["pclink/fleet/state"]
    assert rollup["pcs_online"] == 2 and rollup["any_pc_in_use"]
    user_rollup = mqtt.published["pclink/fleet/user/kevin/state"]
    assert user_rollup["total_minutes_today"] == 55 and sorted(user_rollup["active_on"]) == ["aaa_pc", "zzz-laptop"]
    assert "fleet:sensor/fleet_user_kevin_total_today" in mqtt.discovery
    assert mqtt.discovery["fleet:sensor/fleet_user_kevin_total_today"]["device"]["identifiers"] == ["pclink_fleet"]
    # a peer with a smaller id takes over leadership
    orch.on_peer_message("pclink/000-first/availability", b"online")
    assert not orch.fleet.is_leader()


# ---------------------------------------------------------------------------
# local API backend: entity catalog, "set" action, screenshots, running without MQTT
# ---------------------------------------------------------------------------

def test_api_entities_catalog_and_set_action():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now)
    sessions.sessions = [session()]
    orch.tick()
    cat = orch.api_entities()
    assert cat["device"]["identifiers"] == ["pclink_testpc"] and cat["version"]
    assert cat["triggers"]["user"]["time_up"] == "ran out of time" and "started" in cat["triggers"]["pc"]
    assert cat["users"] == {"kevin": {"name": "kevin", "managed": True}, "dad": {"name": "dad", "managed": False}}
    keys = {e["key"] for e in cat["entities"]}
    assert {"cpu_percent", "time_limits_enabled", "user_kevin_blocked", "user_kevin_time_remaining",
            "user_kevin_limit_today", "user_dad_time_used_today"} <= keys
    assert "user_dad_limit_today" not in keys and "online" not in keys
    assert len(keys) == len(cat["entities"])  # keys are unique
    # entity commands through the JSON "set" action (what the native integration sends)
    assert orch.api_command({"action": "set", "entity": "user_kevin_blocked", "value": "ON"})["ok"]
    assert orch.timekeeper.manually_blocked("kevin")
    assert orch.api_command({"action": "set", "entity": "user_kevin_blocked", "value": False})["ok"]
    assert not orch.timekeeper.manually_blocked("kevin")
    assert orch.api_command({"action": "set", "entity": "user_kevin_limit_today", "value": 200})["ok"]
    assert orch.timekeeper.effective_limit_minutes("kevin", now.date()) == 200
    assert orch.api_command({"action": "set", "entity": "time_limits_enabled", "value": "OFF"})["ok"]
    assert not orch.timekeeper.global_enabled
    assert orch.api_command({"action": "set", "entity": "user_kevin_add_15", "value": "PRESS"})["ok"]
    assert orch.timekeeper.bonus_seconds("kevin", now.date()) == 15 * 60
    assert not orch.api_command({"action": "set"})["ok"]
    assert not orch.api_command({"action": "set", "entity": "cmd", "value": {}})["ok"]
    assert "unknown" in orch.api_command({"action": "set", "entity": "nonsense", "value": "x"})["error"]
    assert orch._last_api_push == 0.0  # a command forces the next state push


def test_set_action_is_pin_gated_and_reports_the_reason():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, PIN_YAML)
    sessions.sessions = [session()]
    orch.tick()
    r = orch.api_command({"action": "set", "entity": "user_kevin_add_15", "value": "PRESS"})
    assert r == {"ok": False, "error": "parent pin required"}
    assert orch.api_command({"action": "set", "entity": "user_kevin_add_15", "value": "PRESS", "pin": "2468"})["ok"]
    assert orch.timekeeper.bonus_seconds("kevin", now.date()) == 15 * 60
    orch.pin.lock()  # the inline PIN unlocked the session for pin_timeout_seconds
    r = orch.api_command({"action": "set", "entity": "time_limits_enabled", "value": "OFF"})
    assert r == {"ok": False, "error": "parent pin required"} and orch.timekeeper.global_enabled
    assert orch.api_command({"action": "set", "entity": "time_limits_enabled", "value": "OFF", "pin": "2468"})["ok"]


def test_api_screenshot_bytes_are_kept():
    import base64
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, """
        device: {name: TestPC}
        telemetry: {interval_seconds: 0}
        screenshots: {enabled: true, require_pin: false, format: png}
        time_limits: {users: [{name: kevin, daily_limit_minutes: 60}]}
    """, agent=True)
    orch._agent.connected.add(2)
    sessions.sessions = [session()]
    orch.tick()
    assert orch.api_screenshot("kevin") is None
    r = orch.api_command({"action": "screenshot", "user": "kevin"})
    assert r["ok"]
    orch.on_agent_message(2, {"type": "screenshot", "id": r["id"], "format": "png", "width": 8, "height": 8,
                              "data": base64.b64encode(b"PNGDATA").decode()})
    orch.tick()
    assert orch.api_screenshot("KEVIN") == (b"PNGDATA", "png")
    assert orch.api_screenshot("nobody") is None


def test_runs_without_mqtt_and_still_serves_the_catalog():
    import yaml
    now = datetime(2026, 9, 28, 12, 0, 0)
    cfg = Config.from_dict(yaml.safe_load("""
        device: {name: TestPC}
        mqtt: {enabled: false}
        telemetry: {interval_seconds: 0}
        time_limits: {users: [{name: kevin, daily_limit_minutes: 60}]}
    """))
    sessions = FakeSessions()
    orch = Orchestrator(cfg, store=Store(":memory:"), sessions=sessions, blocker=FakeBlocker(), mqtt=None,
                        clock=lambda: now)
    assert orch.fleet is None
    sessions.sessions = [session()]
    orch.tick()
    orch.tick()
    cat = orch.api_entities()
    assert "user_kevin_time_remaining" in {e["key"] for e in cat["entities"]}
    assert orch.api_users()["kevin"]["status"] == "active"
    assert orch.api_health()["name"] == "TestPC" and orch.api_health()["mqtt_connected"] is False
    assert orch.api_command({"action": "set", "entity": "user_kevin_blocked", "value": "ON"})["ok"]


def test_mqtt_discovery_can_be_turned_off():
    now = datetime(2026, 9, 28, 12, 0, 0)
    orch, sessions, blocker, mqtt = build(now, """
        device: {name: TestPC}
        mqtt: {discovery: false}
        telemetry: {interval_seconds: 0}
        time_limits: {users: [{name: kevin, daily_limit_minutes: 60}]}
    """)
    sessions.sessions = [session()]
    orch.tick()
    assert mqtt.discovery == {}                       # no HA discovery payloads
    assert mqtt.state_topic in mqtt.published         # state still flows for other PCs (fleet)
    assert mqtt.user_state_topic("kevin") in mqtt.published
