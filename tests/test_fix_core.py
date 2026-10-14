"""Tests for the fixes of the core review: forged agent reports, isolated tick steps, the agent
watchdog, tamper detection, slug collisions, edge-triggered events, monotonic timers, hot reload."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

import pytest
import yaml

from pclink import core as core_module
from pclink import logonblock, power
from pclink.config import Config
from pclink.core import MAX_NAMES_PER_DAY, NEVER, Orchestrator
from pclink.entities import PC_TRIGGERS, USER_TRIGGERS, EntityFactory, SlugRegistry
from pclink.logonblock import LogonBlocker, NullBlocker, valid_account_name
from pclink.models import AgentReport, ForegroundInfo, SessionInfo
from pclink.store import Store

NOW = datetime(2026, 9, 28, 12, 0, 0)   # a Monday
BASE = """
device: {name: TestPC}
telemetry: {interval_seconds: 0}
storage: {flush_seconds: 0}
time_limits:
  block_method: logon_hours
  users:
    - name: kevin
      daily_limit_minutes: 60
      weekly_limit_minutes: 300
      grace_seconds: 0
      rules:
        - {name: Roblox, apps: [roblox.exe], blocked: true}
        - {name: YouTube, sites: [youtube.com], daily_limit_minutes: 30}
    - name: dad
      unlimited: true
"""


class FakeSessions:
    def __init__(self):
        self.sessions, self.logged_off, self.disconnected, self.messages = [], [], [], []
        self.fail = False

    def list_sessions(self):
        if self.fail:
            raise RuntimeError("WTSEnumerateSessions failed")
        return [SessionInfo(session_id=s.session_id, user=s.user, state=s.state, locked=s.locked,
                            idle_seconds=s.idle_seconds) for s in self.sessions]

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
        self.state, self.calls, self.fail = {}, [], set()

    def block(self, user):
        self.calls.append(("block", user))
        if user in self.fail:
            return False
        self.state[user] = True
        return True

    def unblock(self, user):
        self.calls.append(("unblock", user))
        if user in self.fail:
            return False
        self.state[user] = False
        return True

    def is_blocked(self, user):
        return self.state.get(user)


class FakeApi:
    """Stands in for the local API server with the Home Assistant integration connected:
    records the state and the events the service pushes over the WebSocket."""

    def __init__(self):
        self.pc, self.users = None, {}       # the last state push
        self.pushes, self.messages = 0, []   # state pushes so far; every event message
        self.events = []                     # (path, kind): "pc" or "user/<name>", and the event
        self.client_count = 1

    def user_state(self, user="kevin"):
        return self.users[user]

    def push(self, message):
        if message.get("type") == "state":
            self.pushes += 1
            self.pc, self.users = message["pc"], message["users"]
        elif message.get("type") == "event":
            self.messages.append(message)
            self.events.append((f"user/{message['user']}" if message.get("user") else "pc", message["event"]))
        return self.client_count

    def start(self):
        pass

    def stop(self):
        pass


class FakeAgents:
    def __init__(self):
        self.sent, self.connected = [], set()

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
        return sum(1 for sid in self.connected if self.send(sid, message))

    def connected_sessions(self):
        return {sid: {} for sid in self.connected}

    def of(self, kind):
        return [m for _sid, m in self.sent if m.get("type") == kind]


class FakeHandle:
    def __init__(self, sid, pid):
        self.session_id, self.pid, self.alive = sid, pid, True


class FakeLauncher:
    agent_settings: dict = {}

    def __init__(self):
        self.launched, self.terminated, self.released = [], [], []

    def launch(self, sid, token, user):
        handle = FakeHandle(sid, 1000 + len(self.launched))
        self.launched.append(handle)
        return handle

    def alive(self, handle):
        return handle.alive

    def release(self, handle):
        self.released.append(handle)

    def terminate(self, handle):
        handle.alive = False
        self.terminated.append(handle)


class Clocks:
    """The service's three clocks, moved together unless a test moves one alone."""

    def __init__(self, now=NOW):
        self.now, self.wall, self.mono = now, 1_800_000_000.0, 50_000.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.wall += seconds
        self.mono += seconds


def build(text=BASE, agent=True, launcher=False, blocker=None, store=None, api=None):
    """``api``: the fake API server (default: a FakeApi, whose pushes the tests read)."""
    cfg = Config.from_dict(yaml.safe_load(text))
    cfg.api.enabled, cfg.api.push_interval_seconds = True, 0   # every tick pushes the state
    clocks = Clocks()
    sessions, api = FakeSessions(), api if api is not None else FakeApi()
    agents = FakeAgents() if agent else None
    orch = Orchestrator(cfg, store=store or Store(":memory:"), sessions=sessions,
                        blocker=blocker if blocker is not None else FakeBlocker(),
                        clock=lambda: clocks.now, agent_server=agents,
                        launcher=FakeLauncher() if launcher else None, api=api)
    orch._wall, orch._mono = (lambda: clocks.wall), (lambda: clocks.mono)
    orch._zone_probe = lambda: (7200.0, "Central Europe Standard Time")
    orch._clocks = clocks
    return orch, sessions, api, agents


def session(user="kevin", sid=2, **kw):
    return SessionInfo(session_id=sid, user=user, state=kw.get("state", "active"), locked=kw.get("locked", False),
                       idle_seconds=kw.get("idle", 0.0))


def report(orch, sid=2, name="chrome.exe", domain=None, pid=100, **kw):
    fg = ForegroundInfo(pid=pid, name=name, title=kw.pop("title", ""), created=kw.pop("created", 0.0)) if name else None
    orch.on_agent_report(sid, AgentReport(session_id=sid, ts=0.0, idle_seconds=kw.pop("idle", 0.0), foreground=fg,
                                          domain=domain, **kw))


def tick(orch, seconds=5.0, strict=True):
    orch._clocks.advance(seconds)
    orch.tick()
    if strict:
        assert not orch._failing_steps, orch._failing_steps


def user_state(api, user="kevin"):
    return api.users[user]


def events(api, kind):
    return [e for e in api.events if e[1] == kind]


# -- 1 / C5: reports from the agent are validated ---------------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"session_id": 2, "idle_seconds": 1, "input_idle_seconds": NaN}',
    '{"session_id": 2, "idle_seconds": Infinity}',
    '{"session_id": 2, "idle_seconds": "soon"}',
    '{"session_id": 2, "domain": ["a"]}',
    '{"session_id": 2, "url": {"a": 1}}',
    '{"session_id": 2, "foreground": "chrome.exe"}',
    '{"session_id": 2, "foreground": {"name": 5}}',
    '{"session_id": 2, "media": {"title": []}}',
    '{"session_id": 2, "volume": "loud"}',
    '[1, 2]',
])
def test_a_report_with_a_bad_field_is_invalid(raw):
    assert AgentReport.from_dict(json.loads(raw)).invalid


def test_report_fields_are_clamped_and_cut():
    rep = AgentReport.from_dict({"session_id": 2, "ts": 9e99, "idle_seconds": -5, "volume": 400, "domain": " YouTube.COM ",
                                 "url": "x" * 5000, "host": "WWW.YouTube.com", "agent_pid": 4242,
                                 "foreground": {"pid": 7, "name": "a" * 999 + ".exe", "title": "one\ntwo\x00", "created": 12.5}})
    assert rep.invalid is None and rep.idle_seconds == 0.0 and rep.volume == 100
    assert rep.domain == "youtube.com" and rep.host == "www.youtube.com" and rep.agent_pid == 4242
    assert len(rep.url) == 2048 and len(rep.foreground.name) == 260 and rep.foreground.created == 12.5
    assert rep.foreground.title == "one two "
    assert AgentReport.from_dict(rep.to_dict()).invalid is None   # what the agent sends round-trips


def test_a_forged_report_neither_stops_the_tick_nor_the_clock():
    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    orch.on_agent_report(2, AgentReport.from_dict(json.loads('{"session_id": 2, "input_idle_seconds": NaN}')))
    assert 2 not in orch._reports
    tick(orch, 60)
    assert orch.store.usage("kevin", NOW.date()).active == pytest.approx(60)   # time is still counted
    state = user_state(api)
    assert state["tamper"] is True and state["tamper_reason"] == "report_rejected" and state["tamper_at"]
    assert events(api, "tamper") == [("user/kevin", "tamper")]


def test_a_report_is_dated_by_its_arrival_not_by_what_it_says():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    orch.on_agent_report(2, AgentReport(session_id=2, ts=9e10, idle_seconds=0.0))   # "from the future"
    tick(orch)
    assert orch._last_sessions[0].report is not None
    tick(orch, 100)   # nothing arrived since: stale, whatever ``ts`` claims
    assert orch._last_sessions[0].report is None


# -- 2: one failing step does not take the rest of the tick with it ---------------------------------

def test_a_failing_step_is_isolated_counted_and_reported(monkeypatch):
    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    flushed = []
    monkeypatch.setattr(orch.store, "flush", lambda: flushed.append(1))
    monkeypatch.setattr(orch, "_check_notes", lambda *a: 1 / 0)
    for _ in range(4):
        tick(orch, 5, strict=False)
    assert orch.store.usage("kevin", NOW.date()).active == pytest.approx(20)   # counted
    assert len(flushed) == 4                                                     # and written, every tick
    assert orch._stats["tick_failures"] == 4 and "notifications" in orch._failing_steps
    pc = api.pc   # after three failed ticks in a row the PC state says so
    assert pc["degraded"] is True and pc["tick_failures"] == 3 and pc["last_good_tick"]
    assert events(api, "degraded") == [("pc", "degraded")]
    assert orch.api_health()["degraded"] is True
    monkeypatch.undo()
    tick(orch)
    tick(orch)
    assert api.pc["degraded"] is False and len(events(api, "degraded")) == 1


def test_one_users_failure_does_not_cost_the_others(monkeypatch):
    orch, sessions, api, _agents = build()
    sessions.sessions = [session(), session("dad", 3)]
    real = orch.user_payload
    monkeypatch.setattr(orch, "user_payload", lambda user, now, s: 1 / 0 if user == "dad" else real(user, now, s))
    tick(orch, strict=False)
    assert "kevin" in api.users and "dad" not in api.users


def test_absurd_focus_minutes_are_refused_and_a_stored_one_is_survived():
    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    for minutes in (99999999999, -3, 0, float("nan"), "soon", [5]):
        assert orch.api_command({"action": "focus", "user": "kevin", "minutes": minutes})["ok"] is False
    assert orch.store.get_setting("kevin", "focus_until") is None
    assert orch.api_command({"action": "focus", "user": "kevin", "minutes": 30})["ok"] is True
    orch.store.set_setting("kevin", "focus_until", 6e12)   # what an older version could store
    tick(orch)
    assert user_state(api)["user"] == "kevin"


# -- 3: what an agent sends is bounded ---------------------------------------------------------------

def test_agent_messages_are_limited_in_kind_and_number():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    orch.on_agent_message(2, {"type": "whatever", "junk": "x" * 100000})
    orch.on_agent_message(2, {"type": "screenshot", "id": "nobody asked", "data": "AAAA"})
    orch.on_agent_message(2, ["not", "an", "object"])
    assert orch._commands.qsize() == 0
    for _ in range(200):
        orch.on_agent_message(2, {"type": "closed", "name": "x.exe", "result": "terminated 1"})
    assert orch._commands.qsize() == core_module.AGENT_MESSAGE_BURST
    assert orch._stats["dropped_messages"] == 200 - core_module.AGENT_MESSAGE_BURST
    orch._clocks.advance(core_module.AGENT_MESSAGE_WINDOW + 1)
    orch.on_agent_message(2, {"type": "closed", "name": "x.exe", "result": "terminated 1"})
    assert orch._commands.qsize() == core_module.AGENT_MESSAGE_BURST + 1


def test_text_from_an_agent_reaches_the_log_as_one_short_line(caplog):
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    with caplog.at_level("INFO"):
        orch._handle_agent_message(2, {"type": "closed", "name": "evil\n2026-01-01 ERROR forged" + "x" * 5000, "result": "ok"})
    line = next(r.getMessage() for r in caplog.records if "closed" in r.getMessage())
    assert "\n" not in line and len(line) < 250


def test_made_up_names_end_in_other():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    for i in range(MAX_NAMES_PER_DAY + 40):
        report(orch, name=f"fake{i}.exe")
        tick(orch, 1)
    apps = dict(orch.store.top_apps("kevin", NOW.date(), 10 ** 6))
    assert len(apps) == MAX_NAMES_PER_DAY + 1 and apps["app:other"] == pytest.approx(40)


def test_a_screenshot_error_from_the_agent_is_cut_short():
    orch, sessions, _api, agents = build("screenshots: {enabled: true}\n" + BASE)
    sessions.sessions = [session()]
    agents.connected.add(2)
    tick(orch)
    req = orch.request_screenshot("kevin")["id"]
    orch._handle_agent_message(2, {"type": "screenshot", "id": req, "error": "boom\n" * 5000})
    reason = orch._users["kevin"].last_refusal["reason"]
    assert len(reason) <= 200 and "\n" not in reason


# -- 4 / 13 / idea 3: agents are supervised ----------------------------------------------------------

def agents_setup(text=BASE):
    orch, sessions, api, agents = build(text, launcher=True)
    sessions.sessions = [session()]
    tick(orch)
    return orch, sessions, api, agents, orch.launcher


def test_a_silent_agent_is_replaced_and_flagged():
    orch, _sessions, api, _agents, launcher = agents_setup()
    first = launcher.launched[0]
    for _ in range(10):   # reports keep it alive
        report(orch)
        tick(orch, 20)
    assert launcher.terminated == []
    for _ in range(6):    # alive, but nothing arrives any more (hung or suspended)
        tick(orch, 20)
    assert launcher.terminated == [first] and len(launcher.launched) == 2
    assert user_state(api)["tamper_reason"] == "agent_silent"
    assert orch.validate_agent_token(2, orch._agents[2].token)


def test_the_watchdog_does_not_fire_after_sleep_and_can_be_switched_off():
    orch, _sessions, _api, _agents, launcher = agents_setup()
    report(orch)
    tick(orch, 8 * 3600)   # the PC slept: silence means nothing
    assert launcher.terminated == []
    orch.cfg.agent.watchdog_seconds = 0
    for _ in range(30):
        tick(orch, 20)
    assert launcher.terminated == []


def test_an_agent_that_keeps_dying_is_flagged_but_not_one_we_asked_to_exit():
    orch, _sessions, api, _agents, launcher = agents_setup()
    orch._agents[2].expected_exit = True   # new settings: we told it to go
    launcher.launched[-1].alive = False
    tick(orch)
    assert orch._agents[2].exits == [] and orch._agents[2].token is None   # the old token died with it
    for _ in range(3):
        tick(orch, 100)   # past the backoff: relaunched
        launcher.launched[-1].alive = False
        tick(orch, 1)
    assert user_state(api)["tamper_reason"] == "agent_killed"


def test_the_agent_survives_a_disconnect_and_ends_with_the_session():
    orch, sessions, _api, _agents, launcher = agents_setup()
    first = launcher.launched[0]
    sessions.sessions = [session(state="disconnected")]   # switch user
    tick(orch)
    sessions.sessions = [session()]
    tick(orch)
    assert launcher.launched == [first] and launcher.terminated == []   # no second agent
    sessions.sessions = []                                               # logged off
    tick(orch)
    assert launcher.terminated == [first] and orch._agents == {}


# -- 5 / C4: one slug allocator -----------------------------------------------------------------------

def test_colliding_names_get_ids_of_their_own_everywhere():
    orch, sessions, api, _agents = build(BASE.replace("name: kevin", "name: kevin.b"))
    sessions.sessions = [session("kevin.b"), session("kevin-b", 3)]
    tick(orch)
    other = orch.slugs.slug("kevin-b")
    assert orch.slugs.slug("kevin.b") == "kevin_b" and other.startswith("kevin_b_") and other != "kevin_b"
    assert orch.slugs.slug("kevin-b") == other   # stable for the same name
    assert user_state(api, "kevin.b")["user"] == "kevin.b" and user_state(api, "kevin-b")["user"] == "kevin-b"
    keys = {e["key"] for e in orch.api_entities()["entities"]}
    assert f"user_{other}_status" in keys and "user_kevin_b_status" in keys
    orch.dispatch(f"user_{other}_logoff", "PRESS")
    assert sessions.logged_off == [3]
    orch.dispatch("user_kevin_b_logoff", "PRESS")
    assert sessions.logged_off == [3, 2]


def test_the_registry_never_hands_out_an_id_twice():
    reg = SlugRegistry()
    ids = [reg.assign(name) for name in ("kevin.b", "kevin-b", "kevin b", "KEVIN.B")]
    assert ids[0] == ids[3] == "kevin_b" and len(set(ids[:3])) == 3
    assert reg.user(ids[1]) == "kevin-b" and SlugRegistry().assign("kevin.b") == "kevin_b"
    second = SlugRegistry()
    second.assign("kevin.b")
    assert second.assign("kevin-b") == ids[1]   # the same after a restart


# -- 6 / idea 8: shutdown and other people's sessions -------------------------------------------------

def test_shutdown_becomes_a_logoff_when_somebody_else_is_only_disconnected():
    orch, sessions, _api, _agents = build(BASE.replace("grace_seconds: 0", "grace_seconds: 0\n      action: shutdown"))
    orch.timekeeper.set_limit_today("kevin", NOW.date(), 0)
    called = []
    orch.power.shutdown = lambda *a, **k: called.append(1)
    sessions.sessions = [session(), session("dad", 3, state="disconnected")]
    for _ in range(3):
        tick(orch)
    assert called == [] and 2 in sessions.logged_off
    sessions.sessions = [session()]   # alone: the PC is shut down
    for _ in range(3):
        tick(orch, 40)
    assert called


# -- 7 / idea 6: events are edge-triggered -------------------------------------------------------------

def close_setup():
    orch, sessions, api, agents = build()
    sessions.sessions = [session()]
    agents.connected.add(2)
    tick(orch)
    return orch, sessions, api, agents


def test_app_closed_is_announced_once_per_window_and_passes_the_creation_time():
    orch, _sessions, api, agents = close_setup()
    for _ in range(4):
        report(orch, name="roblox.exe", pid=555, created=1234.5)
        tick(orch)
    closes = agents.of("close")
    assert len(closes) == 4 and all(c["pid"] == 555 and c["created"] == 1234.5 and c["tab"] is False for c in closes)
    assert len(events(api, "app_closed")) == 1
    assert len(orch.store.recent_events(50, kind="app_close")) == 1
    report(orch, name="notepad.exe")
    tick(orch)
    report(orch, name="roblox.exe", pid=556)   # started again: a new window, a new event
    tick(orch)
    assert len(events(api, "app_closed")) == 2


def test_a_window_that_will_not_close_is_escalated_to_a_lock():
    orch, sessions, api, agents = close_setup()
    orch.timekeeper.set_limit_today("kevin", NOW.date(), 600)
    for _ in range(20):   # 100 s of a blocked site that stays in front
        report(orch, name="chrome.exe", domain="youtube.com", pid=77)
        orch.store.add_rule("kevin", NOW.date(), "youtube", 3600)
        tick(orch)
    closes = agents.of("close")
    assert closes[0]["tab"] is True and closes[-1]["tab"] is False   # tab first, then the browser
    assert events(api, "app_close_failed") == [("user/kevin", "app_close_failed")]
    assert agents.of("lock") or sessions.disconnected
    assert "app_close_failed" in USER_TRIGGERS


def test_the_agents_answer_brings_the_next_step_forward():
    orch, _sessions, _api, agents = close_setup()
    report(orch, name="roblox.exe", pid=9)
    tick(orch)
    for _ in range(2):
        orch.on_agent_message(2, {"type": "closed", "name": "roblox.exe", "pid": 9, "result": "terminated 0"})
    report(orch, name="roblox.exe", pid=9)
    tick(orch, 1)
    assert orch._users["kevin"].closing["step"] == "lock"
    assert orch.store.recent_events(5, kind="app_close_failed")


def test_time_up_is_announced_once_while_a_locked_session_stays():
    orch, sessions, api, agents = build(BASE.replace("grace_seconds: 0", "grace_seconds: 0\n      action: lock"))
    orch.timekeeper.set_limit_today("kevin", NOW.date(), 0)
    sessions.sessions = [session(locked=True)]
    agents.connected.add(2)
    for _ in range(40):
        tick(orch, 30)
    assert events(api, "time_up") == [("user/kevin", "time_up")]
    orch.timekeeper.set_limit_today("kevin", NOW.date(), 600)   # time again, then out of it again: a new spell
    tick(orch, 30)
    orch.timekeeper.set_limit_today("kevin", NOW.date(), 0)
    for _ in range(4):
        tick(orch, 30)
    assert len(events(api, "time_up")) == 2


# -- 8 / 20: login blocks -------------------------------------------------------------------------------

def test_logoff_only_writes_no_event_per_reassert():
    orch, sessions, _api, _agents = build(BASE.replace("logon_hours", "logoff_only"), blocker=NullBlocker())
    sessions.sessions = [session()]
    for _ in range(5):
        tick(orch, 400)
    assert len(orch.store.recent_events(50, kind="login_unblock")) <= 1


def test_login_blocked_is_only_announced_when_it_worked_and_is_retried():
    blocker = FakeBlocker()
    blocker.fail.add("kevin")
    orch, sessions, api, _agents = build(blocker=blocker)
    orch.timekeeper.set_manually_blocked("kevin", True)
    sessions.sessions = []
    tick(orch)
    assert events(api, "login_blocked") == [] and not orch.store.recent_events(5, kind="login_block")
    blocker.fail.clear()
    tick(orch, 400)   # the next reassert
    assert blocker.state["kevin"] is True and len(orch.store.recent_events(5, kind="login_block")) == 1


def test_a_failed_unblock_of_a_removed_user_is_tried_again():
    blocker = FakeBlocker()
    orch, sessions, _api, _agents = build(blocker=blocker)
    orch.timekeeper.set_manually_blocked("kevin", True)
    tick(orch)
    assert blocker.state["kevin"] is True
    blocker.fail.add("kevin")
    orch.reload_config(Config.from_dict(yaml.safe_load(BASE.replace("    - name: kevin\n", "    - name: other\n"))))
    tick(orch)
    assert blocker.state["kevin"] is True and "kevin" in orch._pending_unblocks
    blocker.fail.clear()
    tick(orch, 400)
    assert blocker.state["kevin"] is False and not orch._pending_unblocks


# -- 9: "no limit" is a value, not "forget what I set" ----------------------------------------------------

def test_no_limit_requests_lift_the_limit():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    tk, today = orch.timekeeper, NOW.date()
    assert orch.api_command({"action": "set_limit", "user": "kevin", "scope": "weekly", "minutes": 0})["ok"]
    assert tk.weekly_limit_minutes("kevin") is None
    assert orch.api_command({"action": "set_limit", "user": "kevin", "scope": "weekly"})["ok"]   # back to the config
    assert tk.weekly_limit_minutes("kevin") == 300
    assert orch.dispatch("user_kevin_weekly_limit", "0")["ok"] and tk.weekly_limit_minutes("kevin") is None
    assert orch.api_command({"action": "set_limit", "user": "kevin", "scope": "today", "minutes": -1})["ok"]
    assert tk.status("kevin", NOW).remaining_seconds is None or tk.status("kevin", NOW).remaining_seconds > 60 * 600
    rule = next(r for r in tk.policy_for("kevin").rules if r.slug == "youtube")
    assert orch.api_command({"action": "rule_limit", "user": "kevin", "rule": "YouTube", "minutes": -1})["ok"]
    assert orch.rules.rule_limit_today("kevin", rule, today) is None
    assert orch.api_command({"action": "rule_limit", "user": "kevin", "rule": "YouTube"})["ok"]
    assert orch.rules.rule_limit_today("kevin", rule, today) == 30
    assert orch.dispatch("user_kevin_rule_youtube_limit_today", "1440")["ok"]
    assert orch.rules.rule_limit_today("kevin", rule, today) is None


# -- 10 / idea 4: the PC's clock ------------------------------------------------------------------------------

def test_setting_the_clock_back_stalls_nothing_and_is_noticed():
    orch, sessions, api, _agents = build(BASE.replace("interval_seconds: 0", "interval_seconds: 15"))
    sessions.sessions = [session()]
    tick(orch)
    tick(orch, 20)
    orch._clocks.wall -= 2 * 3600   # two hours back
    pushes = api.pushes
    for _ in range(8):
        tick(orch, 5)
    assert api.pushes - pushes >= 2             # the PC state keeps going out
    assert user_state(api)["tamper_reason"] == "clock_changed"


def test_sleep_is_no_clock_change():
    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    orch._clocks.wall += 8 * 3600
    orch._clocks.mono += 8 * 3600 - 300   # the monotonic clock may lag over a sleep; the hole is long
    tick(orch)
    assert user_state(api)["tamper"] is False


def test_a_changed_time_zone_is_noticed_also_across_a_restart_but_not_daylight_saving():
    store = Store(":memory:")
    orch, sessions, api, _agents = build(store=store)
    sessions.sessions = [session()]
    tick(orch)
    orch._zone_probe = lambda: (3600.0, "Central Europe Standard Time")   # the same zone: winter time
    tick(orch, 60)
    assert user_state(api)["tamper"] is False
    # a new service (after a restart) finds the PC in another zone
    orch2, sessions2, api2, _a = build(store=store)
    orch2._zone_probe = lambda: (14 * 3600.0, "Line Islands Standard Time")
    tick(orch2)                         # nobody logged in yet: kept
    sessions2.sessions = [session()]
    tick(orch2)
    state = user_state(api2)
    assert state["tamper_reason"] == "time_zone_changed" and "Line Islands" in state["tamper_details"]["detail"]


def test_a_tamper_flag_survives_a_restart_and_a_parent_clears_it():
    store = Store(":memory:")
    orch, sessions, api, _agents = build(store=store)
    sessions.sessions = [session()]
    tick(orch)
    orch._flag_tamper("kevin", "agent_silent", "test")
    orch._flag_tamper("dad", "agent_silent", "unmanaged users are not watched")
    assert orch._users["dad"].tamper is None
    orch2, sessions2, api2, _a = build(store=store)
    sessions2.sessions = [session()]
    tick(orch2)
    assert user_state(api2)["tamper_reason"] == "agent_silent"
    assert orch2.dispatch("user_kevin_clear_tamper", "PRESS")["ok"]
    tick(orch2)
    assert user_state(api2)["tamper"] is False and store.get_setting("kevin", "tamper") is None
    orch2._flag_tamper("kevin", "agent_killed")
    assert orch2.api_command({"action": "clear_tamper"})["cleared"] == ["kevin"]


def test_clearing_a_tamper_flag_needs_the_pin():
    orch, sessions, _api, _agents = build(BASE.replace("time_limits:", "time_limits:\n  parent_pin: '4321'"))
    sessions.sessions = [session()]
    tick(orch)
    orch._flag_tamper("kevin", "agent_silent")
    assert orch.api_command({"action": "clear_tamper", "user": "kevin"})["ok"] is False
    assert orch.api_command({"action": "clear_tamper", "user": "kevin", "pin": "4321"})["ok"] is True


# -- 11 / 12: locking, session enumeration --------------------------------------------------------------------

def test_a_lock_pressed_twice_does_not_disconnect():
    orch, sessions, _api, agents = build()
    sessions.sessions = [session()]
    agents.connected.add(2)
    tick(orch)
    orch.api_command({"action": "lock", "user": "kevin"})
    orch.api_command({"action": "lock", "user": "kevin"})
    assert len(agents.of("lock")) == 1 and sessions.disconnected == []
    tick(orch, 30)   # still unlocked half a minute later: the agent did not do it
    orch.api_command({"action": "lock", "user": "kevin"})
    assert sessions.disconnected == [2]


def test_a_failed_enumeration_is_not_everybody_logged_off():
    orch, sessions, api, _agents = build(launcher=True)
    sessions.sessions = [session()]
    tick(orch)
    sessions.fail = True
    tick(orch, 5, strict=False)
    assert "sessions" in orch._failing_steps and [s.user for s in orch._last_sessions] == ["kevin"]
    assert events(api, "logged_off") == [] and len(orch.launcher.launched) == 1 and orch.launcher.terminated == []
    assert orch.store.usage("kevin", NOW.date()).active == pytest.approx(5)
    sessions.fail = False
    tick(orch)


def test_windows_sessions_raises_instead_of_returning_nobody():
    from pclink.winsessions import SessionQueryError, WindowsSessions

    class Wts:
        WTSUserName, WTSDomainName = 5, 7

        def WTSEnumerateSessions(self, server):  # noqa: N802
            raise OSError("RPC server unavailable")

    ws = WindowsSessions.__new__(WindowsSessions)
    ws._wts = Wts()
    with pytest.raises(SessionQueryError):
        ws.list_sessions()


# -- 14 / hot reload ------------------------------------------------------------------------------------------

def test_a_configuration_that_cannot_be_applied_leaves_the_old_one_in_force(monkeypatch):
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    old_cfg, old_classifier, old_policy = orch.cfg, orch.classifier, orch.timekeeper.policy_for("kevin")
    monkeypatch.setattr(core_module, "ActivityClassifier", lambda cfg: 1 / 0)
    result = orch.reload_config(Config.from_dict(yaml.safe_load(BASE.replace("daily_limit_minutes: 60", "daily_limit_minutes: 5"))))
    assert result["ok"] is False and "ZeroDivisionError" in result["error"]
    assert orch.cfg is old_cfg and orch.classifier is old_classifier and orch.timekeeper.policy_for("kevin") is old_policy
    assert orch.store.recent_events(5, kind="config_reload_failed")


def test_reload_hands_the_api_its_live_settings_but_not_its_address():
    class Api:
        client_count = 0

        def __init__(self, cfg):
            self.cfg = cfg

        def push(self, message):
            return 0

    text = "api: {enabled: true, host: 0.0.0.0, token: aaaaaaaaaaaaaaaaaaaa, allow_config_edit: true}\n" + BASE
    cfg = Config.from_dict(yaml.safe_load(text))
    orch, _sessions, _api, _agents = build(text, api=Api(cfg.api))
    new = text.replace("allow_config_edit: true", "allow_config_edit: false").replace("0.0.0.0", "127.0.0.1").replace("aaaa", "bbbb")
    result = orch.reload_config(Config.from_dict(yaml.safe_load(new)))
    assert result["ok"] and result["restart_needed"] == ["api.host"]
    assert orch.api.cfg.allow_config_edit is False and orch.api.cfg.token.startswith("bbbb")
    assert orch.api.cfg.host == "0.0.0.0"   # still what the server is bound to


def test_the_catalog_follows_the_configuration():
    text = BASE + "apps:\n  - {name: Minecraft, launch: 'mc.exe'}\ncommands:\n  - {name: Backup, command: 'b.cmd'}\n"
    orch, sessions, api, _agents = build(text)
    sessions.sessions = [session()]
    tick(orch)

    def catalog():
        return {e["key"]: e for e in orch.api_entities()["entities"]}

    for key in ("launch_minecraft", "cmd_backup", "user_kevin_rule_roblox_enabled", "user_kevin_status", "user_dad_status"):
        assert key in catalog()
    # the app, the command, one rule and a whole user leave the configuration; kevin gets a display name
    new = BASE.replace("        - {name: Roblox, apps: [roblox.exe], blocked: true}\n", "")
    new = new.replace("    - name: dad\n      unlimited: true\n", "").replace("    - name: kevin\n", "    - name: kevin\n      display_name: Kev\n")
    orch.reload_config(Config.from_dict(yaml.safe_load(new)))
    tick(orch)
    after = catalog()
    for key in ("launch_minecraft", "cmd_backup", "user_kevin_rule_roblox_enabled"):
        assert key not in after
    assert orch.api_entities()["users"]["dad"]["managed"] is False   # no longer limited, still seen
    assert after["user_kevin_status"]["name"].startswith("Kev")   # a changed description is in the catalog


# -- 15: parts that could not start --------------------------------------------------------------------------------

def test_a_part_that_cannot_start_is_retried_and_reported():
    orch, sessions, api, agents = build()
    attempts = []

    def start():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("address already in use")

    agents.start = start
    orch._start_components()
    assert orch.agent_server is None and "agent_server" in orch._down
    sessions.sessions = [session()]
    tick(orch)
    assert api.pc["degraded"] is True
    assert "agent IPC server" in api.pc["health"]["not_started"]
    tick(orch, 61)
    assert orch.agent_server is None
    tick(orch, 61)
    assert orch.agent_server is agents and not orch._down
    tick(orch)
    assert api.pc["degraded"] is False


# -- 17 / 18: one status per tick, tray status ----------------------------------------------------------

def test_the_time_status_is_worked_out_once_per_user_and_tick(monkeypatch):
    orch, sessions, _api, agents = build()
    sessions.sessions = [session()]
    agents.connected.add(2)
    report(orch, name="roblox.exe")
    tick(orch)
    calls = []
    real = orch.timekeeper.status
    monkeypatch.setattr(orch.timekeeper, "status", lambda user, now: calls.append(user) or real(user, now))
    report(orch, name="roblox.exe")
    tick(orch)
    assert calls.count("kevin") <= 2   # once inside TimeKeeper.tick, once for every step of the orchestrator


def test_every_agent_gets_the_tray_status_again_when_it_connects():
    orch, sessions, _api, agents = build()
    sessions.sessions = [session("dad"), session("dad", sid=3)]   # "No time limit": a text that never changes
    agents.connected.add(2)
    tick(orch)
    tick(orch, 1)
    assert [sid for sid, m in agents.sent if m["type"] == "status"] == [2]
    agents.connected.add(3)          # the second seat's agent arrives later
    orch.on_agent_connected(3, "dad", 1)
    orch.on_agent_connected(2, "dad", 1)   # ... and the first one was restarted
    tick(orch, 1)
    assert sorted(sid for sid, m in agents.sent if m["type"] == "status") == [2, 2, 3]


# -- 19 / 20: names in commands, small items ---------------------------------------------------------------------------

def test_a_name_in_a_command_does_not_start_managing_somebody():
    orch, _sessions, _api, _agents = build(BASE.replace("time_limits:", "time_limits:\n  manage_unknown_users: true"))
    tick(orch)
    for name in ("/domain", "stranger", "x" * 300):
        assert orch.api_command({"action": "block", "user": name})["ok"] is False
    assert sorted(orch.timekeeper.all_policy_users()) == ["dad", "kevin"]


def test_account_names_are_checked_before_they_reach_net_exe(monkeypatch):
    assert valid_account_name("kevin") and valid_account_name("Kevin Smith") and valid_account_name("péter")
    for bad in ("/domain", "a" * 21, "", "x/y", 'a"b', " kevin", "kevin.", None, 5):
        assert not valid_account_name(bad)
    ran = []
    monkeypatch.setattr(logonblock.subprocess, "run", lambda *a, **k: ran.append(a))
    blocker = LogonBlocker.__new__(LogonBlocker)
    blocker.method = "logon_hours"
    assert blocker.block("/domain") is False and ran == []


def test_logon_hours_an_account_had_before_are_put_back(monkeypatch):
    custom = bytes([0xFF] * 10 + [0x00] * 11)

    class Net:
        hours = custom

        def NetUserGetInfo(self, server, user, level):  # noqa: N802
            return {"name": user, "logon_hours": self.hours, "flags": 0}

    net = Net()
    blocker = LogonBlocker.__new__(LogonBlocker)
    blocker._net, blocker.method = net, "logon_hours"
    stored, written = {}, []
    blocker.on_remember = lambda user, hours: stored.__setitem__(user, hours)

    def run(args, **kw):
        net.hours = bytes(21) if args[3] == "/times:" else bytes([0xFF] * 21)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(logonblock.subprocess, "run", run)
    monkeypatch.setattr(blocker, "_write_hours", lambda user, hours: written.append((user, hours)) or setattr(net, "hours", hours))
    assert blocker.block("Kevin") and stored == {"kevin": custom}
    assert blocker.unblock("Kevin") and written == [("Kevin", custom)] and stored == {"kevin": None}
    assert net.hours == custom
    # when the hours cannot be written back, the account is at least not left blocked
    blocker.block("Kevin")
    monkeypatch.setattr(blocker, "_write_hours", lambda user, hours: 1 / 0)
    assert blocker.unblock("Kevin") and net.hours == bytes([0xFF] * 21)


def test_the_orchestrator_keeps_remembered_logon_hours_in_the_store():
    blocker = LogonBlocker.__new__(LogonBlocker)
    blocker.method = "logon_hours"
    store = Store(":memory:")
    store.set_setting("kevin", "logon_hours_saved", "ff" * 10 + "00" * 11)
    orch, *_ = build(blocker=blocker, store=store)
    assert blocker.remembered == {"kevin": bytes([0xFF] * 10 + [0x00] * 11)}
    blocker.on_remember("kevin", None)
    assert store.get_setting("kevin", "logon_hours_saved") is None


def test_shutdown_exe_is_run_by_its_full_path_and_a_failure_is_not_fatal(monkeypatch):
    calls = []
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power.subprocess, "run", lambda args, **kw: calls.append(args))
    power.PowerControl(5).shutdown("bye")
    assert calls[0][0].lower().endswith("system32\\shutdown.exe") or calls[0][0].lower().endswith("system32/shutdown.exe")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("shutdown.exe", 15)

    monkeypatch.setattr(power.subprocess, "run", boom)
    power.PowerControl(5).reboot("bye")   # logged, not raised


def test_a_command_always_gets_an_answer(monkeypatch):
    orch, _sessions, _api, _agents = build()
    monkeypatch.setattr(orch, "_dispatch_json", lambda data: (_ for _ in ()).throw(OSError("no route to host")))
    result = orch.api_command({"action": "wol", "mac": "aa:bb:cc:dd:ee:ff"})
    assert result["ok"] is False and "OSError" in result["error"]


# -- C3 / C10 / C13 / notifications / events ---------------------------------------------------------------------------

def test_starting_focus_needs_the_pin_only_when_it_pauses_the_budget():
    pin = BASE.replace("time_limits:", "time_limits:\n  parent_pin: '4321'\n  pin_required_for: [grant, focus]")
    orch, sessions, _api, _agents = build(pin)
    sessions.sessions = [session()]
    tick(orch)
    assert orch.dispatch("user_kevin_focus_mode", "ON") == {"ok": False, "error": "parent pin required"}
    assert orch.api_command({"action": "focus", "user": "kevin"}) == {"ok": False, "error": "parent pin required"}
    assert orch.api_command({"action": "focus", "user": "kevin", "pin": "4321"})["ok"] is True
    counting = pin.replace("      grace_seconds: 0", "      grace_seconds: 0\n      focus: {counts_toward_limit: true}")
    orch, sessions, _api, _agents = build(counting)
    sessions.sessions = [session()]
    tick(orch)
    assert orch.api_command({"action": "focus", "user": "kevin"})["ok"] is True   # only restricts: free


def test_the_catalog_hash_tells_when_to_fetch_the_catalog_again():
    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    catalog = orch.api_entities()
    assert catalog["hash"] and catalog["hash"] == api.pc["catalog_hash"] == orch.api_entities()["hash"]
    assert orch.api_command({"action": "rule_set", "user": "kevin", "rule": "TikTok", "sites": ["tiktok.com"], "blocked": True})["ok"]
    tick(orch)
    assert api.pc["catalog_hash"] == orch.api_entities()["hash"] != catalog["hash"]


def test_tamper_and_health_entities_and_triggers():
    cfg = Config.from_dict(yaml.safe_load(BASE))
    factory = EntityFactory(cfg)
    policy = cfg.user_policy("kevin")
    kevin = {e.object_id: e for e in factory.user_entities("kevin", policy, True)}
    assert kevin["user_kevin_tamper"].spec["device_class"] == "tamper"
    assert kevin["user_kevin_tamper_reason"].spec["options"] == core_module.TAMPER_REASONS
    assert kevin["user_kevin_clear_tamper"].component == "button"
    assert kevin["user_kevin_login_blocked"].spec.get("device_class") is None   # "lock" would read "Unlocked" when blocked
    assert "user_dad_tamper" not in {e.object_id for e in factory.user_entities("dad", cfg.user_policy("dad"), False)}
    pc = {e.object_id: e for e in factory.pc_entities({})}
    assert pc["degraded"].spec["device_class"] == "problem" and "tick_failures" in pc and "last_good_tick" in pc
    assert "tamper" in USER_TRIGGERS and "degraded" in PC_TRIGGERS


def test_a_notification_the_agent_could_not_show_is_shown_by_the_service():
    orch, sessions, _api, agents = build()
    sessions.sessions = [session()]
    agents.connected.add(2)
    tick(orch)
    orch.notify_sessions([2], "5 minutes left", "Save your work.", "warning")
    note = agents.of("notify")[-1]
    assert note["id"] and sessions.messages == []
    orch.on_agent_message(2, {"type": "notified", "id": note["id"], "shown": False, "via": "none"})
    tick(orch, 1)
    assert sessions.messages == [(2, "5 minutes left", "Save your work.")]
    # shown quietly: enough for information, not for a warning
    orch.notify_sessions([2], "Hello", "info text", "info")
    orch.on_agent_message(2, {"type": "notified", "id": agents.of("notify")[-1]["id"], "shown": True, "via": "quiet"})
    tick(orch, 1)
    assert len(sessions.messages) == 1
    # no answer at all to a warning: the service steps in after a few seconds; information is let go
    orch.notify_sessions([2], "Bedtime", "Time for bed.", "critical")
    orch.notify_sessions([2], "Hello", "again", "info")
    tick(orch, 10)
    assert sessions.messages[1:] == [(2, "Bedtime", "Time for bed.")] and not orch._pending_notes


def test_announced_events_can_be_asked_for_again():
    orch, sessions, _api, _agents = build()
    tick(orch)
    sessions.sessions = [session()]
    tick(orch)
    before = orch._clocks.wall
    announced = orch.api_events(50, announced=True)
    assert announced[0]["type"] == "event" and isinstance(announced[0]["ts"], float)
    assert {"logged_in", "started"} <= {e["event"] for e in announced}
    kevins = orch.api_events(50, user="kevin", announced=True)
    assert "logged_in" in [e["event"] for e in kevins] and all(e["user"] == "kevin" for e in kevins)
    assert orch.api_events(50, announced=True, since=announced[0]["ts"]) == []
    rows = orch.api_events(50)
    assert rows and all(row["event"] == row["kind"] and isinstance(row["ts"], float) for row in rows)
    assert before


def test_never_is_before_everything():
    assert NEVER < 0 and 0 - NEVER > 1e300
