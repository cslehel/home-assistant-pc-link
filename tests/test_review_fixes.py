"""Regression tests for defects found in the project review (orchestrator, config, IPC, API)."""
import base64
import logging
import os
import socket
import time
from datetime import datetime, timedelta

import pytest
import yaml

from pclink.config import Config, ConfigError
from pclink.core import Orchestrator
from pclink.ipc import _LineReader
from pclink.store import Store

from .test_orchestrator import FakeAgentServer, FakeBlocker, FakeSessions, session

BASE = """
device: {name: TestPC}
telemetry: {interval_seconds: 0}
api: {push_interval_seconds: 0}
time_limits:
  parent_pin: "4321"
  users:
    - name: kevin
      daily_limit_minutes: 60
      weekly_limit_minutes: 300
      grace_seconds: 0
      warn_at_minutes: []
      rules:
        - name: YouTube
          sites: [youtube.com]
          daily_limit_minutes: 30
"""


class FakeApi:
    client_count = 1

    def __init__(self):
        self.pushed = []

    def push(self, message):
        self.pushed.append(message)
        return 1

    def start(self):
        pass

    def stop(self):
        pass


class CountingTelemetry:
    def __init__(self):
        self.n = 0

    def snapshot(self):
        self.n += 1
        return {"cpu_percent": self.n}


def make(text=BASE, api=False, agent=False, storage_dir=None):
    cfg = Config.from_dict(yaml.safe_load(text))
    if storage_dir:
        cfg.storage.path = os.path.join(storage_dir, "pclink.db")
    holder = {"now": datetime(2026, 9, 28, 12, 0, 0)}
    sessions = FakeSessions()
    agent_server = FakeAgentServer() if agent else None
    api_server = FakeApi() if api else None
    orch = Orchestrator(cfg, store=Store(":memory:"), sessions=sessions, blocker=FakeBlocker(),
                        clock=lambda: holder["now"],
                        agent_server=agent_server, api=api_server, telemetry=CountingTelemetry())
    sessions.sessions = [session()]
    orch.tick()
    return orch, sessions, holder, agent_server, api_server


# -- API-only mode ---------------------------------------------------------------------------

def test_api_push_refreshes_pc_state():
    orch, sessions, holder, _agent, api = make(api=True)
    for _ in range(3):
        holder["now"] += timedelta(seconds=5)
        orch.tick()
    values = [m["pc"]["cpu_percent"] for m in api.pushed if m.get("type") == "state"]
    assert len(set(values)) > 1, values


# -- parent PIN gate -------------------------------------------------------------------------

def test_pin_is_not_logged(caplog):
    orch, *_ = make()
    with caplog.at_level(logging.INFO, logger="pclink"):
        orch.api_command({"action": "set", "entity": "parent_pin", "value": "4321"})
        orch.api_command({"action": "pin", "value": "4321"})
        orch.api_command({"action": "bogus", "pin": "4321"})
    assert "4321" not in caplog.text


def test_wrong_pin_through_the_entity_is_reported():
    orch, *_ = make()
    assert orch.api_command({"action": "set", "entity": "parent_pin", "value": "0000"})["ok"] is False


@pytest.mark.parametrize("command", [
    {"action": "set_limit", "user": "kevin", "scope": "weekly", "minutes": 400},   # raise the weekly limit
    {"action": "set_limit", "user": "kevin", "scope": "weekly", "minutes": 0},     # remove it
    {"action": "set_limit", "user": "kevin", "scope": "weekday", "minutes": 61},
    {"action": "rule_limit", "user": "kevin", "rule": "YouTube", "minutes": -1},   # "no limit"
    {"action": "focus", "user": "kevin", "minutes": 600, "allow": ["robloxplayerbeta.exe"]},
    {"action": "focus", "user": "kevin", "minutes": 30},   # focus pauses the time budget: that is more time
])
def test_loosening_needs_the_pin(command):
    orch, *_ = make()
    assert orch.api_command(dict(command)) == {"ok": False, "error": "parent pin required"}
    assert orch.api_command(dict(command, pin="4321"))["ok"] is True


@pytest.mark.parametrize("command", [
    {"action": "set_limit", "user": "kevin", "scope": "weekly", "minutes": 200},
    {"action": "set_limit", "user": "kevin", "scope": "today", "minutes": 30},
    {"action": "rule_limit", "user": "kevin", "rule": "YouTube", "minutes": 10},
])
def test_tightening_needs_no_pin(command):
    orch, *_ = make()
    assert orch.api_command(command)["ok"] is True


def test_resending_a_limit_does_not_postpone_the_logoff():
    text = BASE.replace("grace_seconds: 0", "grace_seconds: 60").replace("daily_limit_minutes: 60\n      weekly", "daily_limit_minutes: 1\n      weekly")
    orch, sessions, holder, *_ = make(text)
    for _ in range(40):  # 200 s: the minute is used up, then the grace period runs
        if not sessions.sessions:
            break
        holder["now"] += timedelta(seconds=5)
        orch.api_command({"action": "set_bonus", "user": "kevin", "minutes": 0})
        orch.tick()
    assert sessions.logged_off, "the user was never logged off"


def test_parent_present_gate_is_opt_in():
    orch, *_ = make()
    assert orch.api_command({"action": "parent_present", "present": True})["ok"] is True
    orch, *_ = make(BASE.replace('parent_pin: "4321"', 'parent_pin: "4321"\n  pin_required_for: [grant, parent_present]'))
    assert orch.api_command({"action": "parent_present", "present": True})["ok"] is False


# -- command results -------------------------------------------------------------------------

def test_empty_user_is_not_everyone():
    orch, sessions, *_ = make()
    assert orch.api_command({"action": "logoff", "user": ""})["ok"] is False
    assert orch.api_command({"action": "logoff", "user": "nobody"})["ok"] is False
    assert sessions.logged_off == []


def test_disabled_power_action_and_bad_values_are_errors():
    orch, *_ = make(BASE + "power: {allow_sleep: false}\n")
    assert orch.api_command({"action": "sleep"})["ok"] is False
    assert orch.api_command({"action": "add_minutes", "user": "kevin", "minutes": "abc", "pin": "4321"})["ok"] is False
    assert orch.api_command({"action": "wol"})["ok"] is False


# -- screenshots -----------------------------------------------------------------------------

def test_agent_cannot_choose_the_screenshot_file_name(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    orch, sessions, holder, agent, _api = make(
        BASE + "screenshots: {enabled: true, require_pin: false, save_to_disk: true}\n", agent=True, storage_dir=str(data_dir))
    agent.connected.add(2)
    req = orch.request_screenshot("kevin")
    orch._handle_agent_message(2, {"type": "screenshot", "id": req["id"], "data": base64.b64encode(b"x").decode(),
                                   "format": "x/../../../escaped.txt", "width": "wide"})
    written = [os.path.relpath(os.path.join(root, name), tmp_path) for root, _d, files in os.walk(tmp_path) for name in files]
    assert len(written) == 1 and written[0].startswith(os.path.join("data", "screenshots")) and written[0].endswith(".jpg")
    assert orch._users["kevin"].screenshot["width"] is None


# -- configuration ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "time_limits: {week_start: 1}",
    "time_limits: {users: [{name: k, grace_seconds: abc}]}",
    "time_limits: {tick_seconds: fast}",
    "time_limits: {reassert_block_seconds: -5}",
    "telemetry: {disks: maybe}",
    "api: {port: 99999}",
    "[1, 2]",
])
def test_bad_configs_are_config_errors(text):
    with pytest.raises(ConfigError):
        Config.from_dict(yaml.safe_load(text))


def test_values_are_brought_to_the_field_type():
    cfg = Config.from_dict(yaml.safe_load("time_limits: {tick_seconds: '5', reassert_block_seconds: null}\napi: {port: '8124'}"))
    assert cfg.time_limits.tick_seconds == 5.0 and cfg.time_limits.reassert_block_seconds == 300.0
    assert cfg.api.port == 8124


def test_reload_survives_a_broken_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(BASE, encoding="utf-8")
    orch = Orchestrator(Config.load(str(path)), store=Store(":memory:"), sessions=FakeSessions(), blocker=FakeBlocker(),
                        clock=lambda: datetime(2026, 9, 28, 12))
    path.write_text("time_limits: {week_start: 1}\n", encoding="utf-8")
    assert orch.reload_config()["ok"] is False
    orch.tick()


def test_wrapped_allowed_hours_continue_into_the_next_day():
    cfg = Config.from_dict(yaml.safe_load(
        "time_limits: {users: [{name: k, allowed_hours: {fri: '16:00-01:00', sat: '10:00-12:00', default: '15:00-20:00'}}]}"))
    windows = cfg.time_limits.users[0].allowed_windows
    assert windows[4] == [(960, 1440)]                  # Friday: no early-morning slot
    assert windows[5] == [(0, 60), (600, 720)]          # Saturday: the tail of Friday night
    same = Config.from_dict(yaml.safe_load("time_limits: {users: [{name: k, allowed_hours: '22:00-02:00'}]}"))
    assert same.time_limits.users[0].allowed_windows[2] == [(0, 120), (1320, 1440)]


# -- IPC -------------------------------------------------------------------------------------

def test_line_reader_keeps_working_after_a_timeout():
    a, b = socket.socketpair()
    try:
        a.settimeout(0.2)
        reader = _LineReader(a)
        b.sendall(b'{"type":"pa')
        with pytest.raises(socket.timeout):
            reader.readline(1024)
        b.sendall(b'rt"}\n{"n":2}\n')
        assert reader.readline(1024) == '{"type":"part"}'
        assert reader.readline(1024) == '{"n":2}'
        b.sendall(b"x" * 5000)
        with pytest.raises(ValueError):
            reader.readline(1024)
        b.close()
    finally:
        a.close()


def test_stalled_websocket_client_does_not_block_push():
    from pclink.api import WS_QUEUE, _WsClient

    a, b = socket.socketpair()  # b never reads
    client = _WsClient(a)
    try:
        started = time.monotonic()
        payload = "x" * 200_000
        results = [client.send_text(payload) for _ in range(WS_QUEUE * 3)]
        assert time.monotonic() - started < 5
        assert results[-1] is False and client.alive is False
    finally:
        client.close()
        b.close()


# -- usage report ----------------------------------------------------------------------------

def test_usage_report_covers_only_the_last_hours():
    from .test_orchestrator import attach, report

    orch, sessions, holder, agent, _api = make(agent=True)
    agent.connected.add(2)

    def use(minutes, name, domain=None):
        for _ in range(minutes * 2):
            holder["now"] += timedelta(seconds=30)
            attach(orch, sessions, 2, report(sid=2, name=name, domain=domain))
            orch.tick()

    use(20, "notepad.exe")                      # 12:00 - 12:20
    holder["now"] += timedelta(hours=11)        # ... a long gap (nothing is counted for it)
    orch.tick()
    use(15, "chrome.exe", "youtube.com")        # about 23:20 - 23:35
    use(5, "code.exe")
    rep = orch.usage_report("kevin", 10)
    assert rep["hours"] == 10 and rep["from"].endswith("T13:00")
    assert "notepad" not in rep["apps"]         # used more than ten hours ago (the gap adds seconds, not minutes)
    assert 14 <= rep["apps"]["chrome"] <= 15.5 and 4 <= rep["apps"]["code"] <= 5.5
    assert list(rep["apps"])[0] == "chrome" and 14 <= rep["sites"]["youtube.com"] <= 15.5
    assert rep["total_minutes"] == pytest.approx(rep["apps"]["chrome"] + rep["apps"]["code"], abs=0.2)
    assert "notepad" in orch.usage_report("kevin", 24)["apps"]
    # it survives a flush, is part of the state payload and available as a command
    orch.store.flush()
    assert orch.usage_report("kevin", 10)["apps"] == rep["apps"]
    assert orch.api_command({"action": "usage_report", "user": "kevin", "hours": 10})["report"]["sites"] == rep["sites"]
    assert orch.api_command({"action": "usage_report"})["ok"] is False
    orch._report_cache.clear()
    payload = orch.user_payload("kevin", holder["now"], sessions.sessions)
    assert payload["usage_recent"]["hours"] == 10 and "chrome" in payload["usage_recent"]["apps"]
    assert orch.api_report("kevin", 10)["apps"] == rep["apps"] and orch.api_report("nobody") is None


def test_hourly_usage_is_purged(tmp_path):
    from datetime import date

    store = Store(str(tmp_path / "db.sqlite"))
    store.add_hourly("kevin", datetime(2026, 9, 1, 10, 30), "app", "old", 600)
    store.add_hourly("kevin", datetime(2026, 9, 28, 10, 30), "app", "new", 600)
    store.flush()
    store.purge(90, date(2026, 9, 28))
    assert store.recent_usage("kevin", datetime(2026, 1, 1)) == {"app": {"new": 600.0}}
    store.close()


# -- blocking apps / websites from Home Assistant ----------------------------------------------

def test_block_item_names_are_normalised():
    from pclink.core import normalize_block_item as norm

    assert norm("Roblox") == "roblox.exe" and norm(" RobloxPlayerBeta.exe ") == "robloxplayerbeta.exe"
    assert norm(r"C:\Games\My Game.exe") == "my game.exe"
    assert norm("youtube.com") == "youtube.com" and norm("https://www.YouTube.com/watch?v=1") == "youtube.com"
    assert norm("*.tiktok.com") == "tiktok.com" and norm("m.facebook.com/path") == "m.facebook.com"
    for bad in ("", "   ", "about:blank", "a" * 300, "bad name!.exe", None):
        assert norm(bad) is None


def test_blocked_site_and_app_are_closed_and_unblocking_needs_the_pin():
    from .test_orchestrator import attach, report

    orch, sessions, holder, agent, _api = make(agent=True)
    agent.connected.add(2)

    def tick(name, domain=None):
        holder["now"] += timedelta(seconds=5)
        attach(orch, sessions, 2, report(sid=2, name=name, domain=domain))
        orch.tick()

    tick("chrome.exe", "youtube.com")
    assert agent.messages("close") == []
    assert orch.api_command({"action": "block_item", "user": "kevin", "item": "https://www.youtube.com/"})["items"] == ["youtube.com"]
    assert orch.dispatch("user_kevin_block_item", "Roblox")["items"] == ["roblox.exe", "youtube.com"]
    tick("chrome.exe", "youtube.com")
    closes = agent.messages("close")
    assert closes and closes[-1][1]["tab"] is True and closes[-1][1]["name"] == "chrome.exe"
    tick("robloxplayerbeta.exe")            # a different program: untouched
    tick("roblox.exe")
    assert agent.messages("close")[-1][1]["name"] == "roblox.exe" and agent.messages("close")[-1][1]["tab"] is False
    payload = orch.user_payload("kevin", holder["now"], sessions.sessions)
    assert payload["blocked_count"] == 2 and payload["blocked_details"]["sites"] == ["youtube.com"]

    # refusals
    assert orch.api_command({"action": "block_item", "user": "kevin", "item": "explorer.exe"})["ok"] is False
    assert orch.api_command({"action": "block_item", "user": "kevin", "item": "not a site!"})["ok"] is False
    assert orch.api_command({"action": "block_item", "user": "nobody", "item": "x.com"})["ok"] is False
    assert orch.api_command({"action": "unblock_item", "user": "kevin", "item": "youtube.com"}) == {"ok": False, "error": "parent pin required"}
    assert orch.api_command({"action": "unblock_item", "user": "kevin", "item": "vimeo.com", "pin": "4321"})["ok"] is False
    # the block list survives a restart of the engine state and is lifted with the PIN
    assert orch.api_command({"action": "unblock_item", "user": "kevin", "item": "youtube.com", "pin": "4321"})["items"] == ["roblox.exe"]
    before = len(agent.messages("close"))
    for _ in range(8):
        tick("chrome.exe", "youtube.com")
    assert len(agent.messages("close")) == before
    assert orch.api_command({"action": "unblock_item", "user": "kevin", "item": "all", "pin": "4321"})["items"] == []
    assert orch.blocked_items("kevin") == []


def test_idle_and_away_time_are_counted_and_reported():
    from .test_orchestrator import attach, report

    orch, sessions, holder, agent, _api = make(agent=True)
    agent.connected.add(2)

    def spend(minutes, idle):
        for _ in range(minutes * 2):
            holder["now"] += timedelta(seconds=30)
            sessions.sessions[0].idle_seconds = idle
            attach(orch, sessions, 2, report(sid=2, name="notepad.exe", idle=idle))
            orch.tick()

    spend(10, 0)          # at the keyboard
    spend(6, 300)         # no input for 5 minutes: idle (activity.idle_seconds is 120)
    spend(8, 3600)        # gone for an hour: away (activity.away_seconds is 600)
    rep = orch.usage_report("kevin", 10)
    assert 9 <= rep["active_minutes"] <= 11 and 5 <= rep["idle_minutes"] <= 7 and 7 <= rep["away_minutes"] <= 9
    orch._presence_cache.clear()
    payload = orch.user_payload("kevin", holder["now"], sessions.sessions)
    assert 5 <= payload["idle_minutes_today"] <= 7 and 7 <= payload["away_minutes_today"] <= 9
    assert 9 <= rep["apps"]["notepad"] <= 11       # idle and away time is not credited to the app
