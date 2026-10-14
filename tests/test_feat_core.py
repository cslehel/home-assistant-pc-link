"""Phase-2 features in the orchestrator: asking for more time, profiles, bonus shared across PCs,
the weekly summary - and the glue that came with them (host matching, oversized agent messages,
a start from the last good configuration)."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
import yaml

from pclink.api import is_admin_command
from pclink.config import Config
from pclink.entities import PC_TRIGGERS, USER_TRIGGERS, EntityFactory
from pclink.models import AgentReport, ForegroundInfo

from .test_fix_core import BASE, NOW, build, events, report, session, tick, user_state

PIN = BASE.replace("time_limits:", "time_limits:\n  parent_pin: '4321'")
PROFILES = PIN.replace("  block_method: logon_hours", """  block_method: logon_hours
  profiles:
    holiday: {daily_limit_minutes: 180}
    exams: {daily_limit_minutes: 20}""")


def setup(text=PIN):
    orch, sessions, api, agents = build(text)
    sessions.sessions = [session()]
    agents.connected.add(2)
    tick(orch)
    return orch, sessions, api, agents


def ask(orch, minutes=30, sid=2):
    orch.on_agent_message(sid, {"type": "time_request", "minutes": minutes})
    tick(orch, 1)


def notes(agents):
    return [m["message"] for m in agents.of("notify")]


# -- F1: ask for more time -----------------------------------------------------------------------------

def test_a_child_asks_and_a_parent_approves():
    orch, _sessions, api, agents = setup()
    assert agents.of("status")[-1]["time_requests"] == [15, 30, 60]
    ask(orch, 30)
    state = user_state(api)
    assert state["time_request_minutes"] == 30 and state["time_request"]["minutes"] == 30
    assert state["time_request"]["at"] and state["time_request"]["expires"] > state["time_request"]["at"]
    assert events(api, "time_requested") == [("user/kevin", "time_requested")]
    assert "asked for 30 more minutes" in notes(agents)[-1]
    before = orch.timekeeper.local_bonus_seconds("kevin", NOW.date())
    # approving is a grant: it needs the parent PIN
    assert orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": True})["error"] == "parent pin required"
    answer = orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": True, "pin": "4321"})
    assert answer == {"ok": True, "approved": True, "minutes": 30}
    assert orch.timekeeper.local_bonus_seconds("kevin", NOW.date()) == before + 30 * 60
    assert "got 30 more minutes" in notes(agents)[-1]
    tick(orch, 1)
    state = user_state(api)
    assert state["time_request"] is None and state["time_request_minutes"] == 0
    assert events(api, "time_request_approved") == [("user/kevin", "time_request_approved")]
    assert orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": True, "pin": "4321"})["ok"] is False


def test_a_parent_may_grant_another_amount_or_deny():
    orch, _sessions, api, agents = setup(BASE)
    ask(orch, 60)
    assert orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": True, "minutes": 9999})["ok"] is False
    assert orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": True, "minutes": 10})["minutes"] == 10
    assert orch.timekeeper.local_bonus_seconds("kevin", NOW.date()) == 600
    orch._clocks.advance(11 * 60)
    ask(orch, 15)
    assert orch.dispatch("user_kevin_deny_time_request", "PRESS") == {"ok": True, "approved": False}   # needs no PIN
    assert "not granted" in notes(agents)[-1] and events(api, "time_request_denied")
    assert orch.timekeeper.local_bonus_seconds("kevin", NOW.date()) == 600
    orch._clocks.advance(11 * 60)
    ask(orch, 15)
    assert orch.dispatch("user_kevin_approve_time_request", "PRESS")["minutes"] == 15


def test_requests_are_limited_and_every_refusal_is_answered():
    text = BASE.replace("time_limits:", "time_limits:\n  time_requests: {options: [15, 30], cooldown_minutes: 10, max_per_day: 2, expire_minutes: 30}")
    orch, _sessions, api, agents = setup(text)
    ask(orch, 45)   # not one of the options
    assert "not available" in notes(agents)[-1] and user_state(api)["time_request"] is None
    ask(orch, 15)
    ask(orch, 30)   # one request at a time
    assert "already asked" in notes(agents)[-1] and user_state(api)["time_request_minutes"] == 15
    orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": False})
    ask(orch, 15)   # too soon after the last one
    assert "ask again in" in notes(agents)[-1] and user_state(api)["time_request"] is None
    orch._clocks.advance(11 * 60)
    ask(orch, 15)
    assert user_state(api)["time_request_minutes"] == 15
    orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": False})
    orch._clocks.advance(11 * 60)
    ask(orch, 15)   # the third of the day
    assert "again today" in notes(agents)[-1] and user_state(api)["time_request"] is None
    assert len(events(api, "time_requested")) == 2


def test_an_unanswered_request_expires():
    orch, _sessions, api, agents = setup(BASE)
    ask(orch, 15)
    tick(orch, 29 * 60)
    assert user_state(api)["time_request_minutes"] == 15
    tick(orch, 2 * 60)
    assert user_state(api)["time_request"] is None and events(api, "time_request_expired")
    assert "Nobody answered" in notes(agents)[-1]


def test_time_requests_are_not_offered_when_switched_off_or_to_unlimited_users():
    off = BASE.replace("time_limits:", "time_limits:\n  time_requests: {enabled: false}")
    orch, sessions, api, agents = setup(off)
    assert agents.of("status")[-1]["time_requests"] == []
    ask(orch, 15)
    assert user_state(api)["time_request"] is None and not events(api, "time_requested")
    factory = EntityFactory(Config.from_dict(yaml.safe_load(off)))
    assert "user_kevin_time_request" not in {e.object_id for e in factory.user_entities("kevin", factory.cfg.user_policy("kevin"), True)}
    orch, sessions, api, agents = build(BASE)
    sessions.sessions = [session("dad", 3)]
    agents.connected.add(3)
    tick(orch)
    assert agents.of("status")[-1]["time_requests"] == []
    orch.on_agent_message(3, {"type": "time_request", "minutes": 15})
    tick(orch, 1)
    assert orch.pending_time_request("dad") is None


def test_time_request_entities_and_triggers():
    cfg = Config.from_dict(yaml.safe_load(BASE))
    factory = EntityFactory(cfg)
    kevin = {e.object_id: e for e in factory.user_entities("kevin", cfg.user_policy("kevin"), True)}
    assert kevin["user_kevin_time_request"].spec["value"] == "time_request_minutes"
    assert kevin["user_kevin_time_request"].spec["attributes"] == "time_request"
    assert kevin["user_kevin_approve_time_request"].component == kevin["user_kevin_deny_time_request"].component == "button"
    assert {"time_requested", "time_request_approved", "time_request_denied", "time_request_expired",
            "profile_changed", "weekly_summary"} <= set(USER_TRIGGERS)


# -- F2: profiles -------------------------------------------------------------------------------------------

def test_profile_select_payload_and_switching():
    orch, _sessions, api, _agents = setup(PROFILES)
    state = user_state(api)
    assert state["profile"] == "normal" and state["profiles"] == ["normal", "exams", "holiday"]
    entry = next(e for e in orch.api_entities()["entities"] if e["key"] == "user_kevin_profile")
    assert entry["platform"] == "select" and entry["options"] == ["normal", "exams", "holiday"] and entry["value"] == "profile"
    assert entry["command"]
    # a tighter profile needs no PIN, a looser one does
    assert orch.dispatch("user_kevin_profile", "exams")["ok"] is True
    tick(orch, 1)
    assert user_state(api)["profile"] == "exams" and user_state(api)["limit_minutes"] == 20
    assert events(api, "profile_changed") == [("user/kevin", "profile_changed")]
    assert orch.dispatch("user_kevin_profile", "holiday") == {"ok": False, "error": "parent pin required"}
    answer = orch.api_command({"action": "set_profile", "user": "kevin", "profile": "holiday", "pin": "4321"})
    assert answer["ok"] and answer["changed"] == ["kevin"]
    tick(orch, 1)
    assert user_state(api)["profile"] == "holiday" and user_state(api)["limit_minutes"] == 180
    assert orch.api_command({"action": "set_profile", "user": "kevin", "profile": "holiday", "pin": "4321"})["changed"] == []
    assert len(events(api, "profile_changed")) == 2
    assert orch.api_command({"action": "set_profile", "user": "kevin", "profile": "nonsense"})["ok"] is False
    assert orch.api_command({"action": "set_profile", "user": "dad", "profile": "holiday"})["ok"] is False


def test_a_profile_for_everybody_and_back_to_normal():
    two = PROFILES.replace("    - name: dad\n      unlimited: true", "    - name: anna\n      daily_limit_minutes: 90\n    - name: dad\n      unlimited: true")
    orch, _sessions, _api, _agents = setup(two)
    answer = orch.api_command({"action": "set_profile", "profile": "exams"})
    assert answer["ok"] and sorted(answer["users"]) == ["anna", "kevin"]
    assert orch.timekeeper.active_profile("kevin") == orch.timekeeper.active_profile("anna") == "exams"
    assert orch.api_command({"action": "set_profile", "profile": "normal"})["error"] == "parent pin required"   # more time again
    assert orch.api_command({"action": "set_profile", "profile": "normal", "pin": "4321"})["ok"]
    assert orch.timekeeper.active_profile("kevin") is None
    assert orch.api_command({"action": "set_profile", "profile": "summer"})["ok"] is False
    assert orch.api_command({"action": "set_profile"})["ok"] is False


def test_no_profile_select_without_profiles():
    orch, _sessions, api, _agents = setup(BASE)
    assert "user_kevin_profile" not in {e["key"] for e in orch.api_entities()["entities"]}
    assert user_state(api)["profiles"] == ["normal"]


# -- F3: bonus and bedtime extension shared across PCs --------------------------------------------------------

SHARED = BASE.replace("time_limits:", "time_limits:\n  shared_budget: true") + "fleet: {enabled: true}\n"


def test_extras_of_other_pcs_are_wired_only_with_a_shared_budget():
    orch, *_ = build(BASE + "fleet: {enabled: true}\n")
    assert orch.timekeeper.external_extras is None
    orch, *_ = build(SHARED)
    assert orch.timekeeper.external_extras == orch.fleet.external_extras
    orch.reload_config(Config.from_dict(yaml.safe_load(BASE + "fleet: {enabled: true}\n")))
    assert orch.timekeeper.external_extras is None


def test_local_figures_are_published_and_what_the_number_entities_show():
    orch, sessions, api, _agents = build(SHARED)
    sessions.sessions = [session()]
    tick(orch)
    today = orch.clock().date().isoformat()
    orch.fleet.update_peers({"den": {"users": {"kevin": {"date": today, "used_minutes": 0, "bonus_local_minutes": 20}}}})
    assert orch.api_command({"action": "set_bonus", "user": "kevin", "minutes": 10})["ok"]
    tick(orch, 1)
    state = user_state(api)
    assert state["bonus_local_minutes"] == 10 and state["bonus_minutes"] == 30   # the total is what the child has
    assert state["bedtime_extension_local_minutes"] == 0
    number = next(e for e in orch.api_entities()["entities"] if e["key"] == "user_kevin_bonus_today")
    assert number["platform"] == "number" and number["value"] == "bonus_local_minutes"
    # "set" compares with what this PC granted: 10 again is no grant, although the total is 30
    pin = build(SHARED.replace("time_limits:", "time_limits:\n  parent_pin: '4321'"))[0]
    pin._ensure_user("kevin")
    pin.fleet.update_peers({"den": {"users": {"kevin": {"date": today, "used_minutes": 0, "bonus_local_minutes": 20}}}})
    assert pin.api_command({"action": "set_bonus", "user": "kevin", "minutes": 10})["error"] == "parent pin required"
    assert pin.api_command({"action": "set_bonus", "user": "kevin", "minutes": 0})["ok"] is True


# -- F4: weekly summary -------------------------------------------------------------------------------------------

def test_the_weekly_summary_is_built_once_per_week():
    orch, sessions, api, _agents = build()
    last_monday = NOW.date() - timedelta(days=7)
    store = orch.store
    for offset, minutes in enumerate((60, 0, 30, 120, 0, 0, 30)):
        day = last_monday + timedelta(days=offset)
        store.add_usage("kevin", day, active=minutes * 60)
    store.add_app("kevin", last_monday, "game:roblox", 3000)
    store.add_app("kevin", last_monday, "app:winword", 600)
    store.add_site("kevin", last_monday, "youtube.com", 1800)
    orch.timekeeper.add_bonus_minutes("kevin", last_monday + timedelta(days=3), 15)
    sessions.sessions = [session()]
    tick(orch)
    state = user_state(api)
    summary = state["weekly_summary"]
    assert state["last_week_minutes"] == summary["total_minutes"] == 240
    assert summary["week_start"] == last_monday.isoformat() and summary["daily_average_minutes"] == 34
    assert summary["busiest_day"] == (last_monday + timedelta(days=3)).isoformat() and summary["busiest_day_minutes"] == 120
    assert summary["days"][last_monday.isoformat()] == 60 and len(summary["days"]) == 7
    assert summary["top_games"] == {"roblox": 50} and summary["top_apps"] == {"winword": 10} and summary["top_sites"] == {"youtube.com": 30}
    assert summary["bonus_minutes"] == 15 and summary["time_ran_out"] == 0
    assert events(api, "weekly_summary") == [("user/kevin", "weekly_summary")]
    tick(orch, 3600)
    assert len(events(api, "weekly_summary")) == 1                 # not again this week
    tick(orch, 7 * 86400)                                            # the next week begins
    assert len(events(api, "weekly_summary")) == 2
    assert user_state(api)["weekly_summary"]["week_start"] == NOW.date().isoformat()
    assert "dad" not in [e[0].split("/")[1] for e in events(api, "weekly_summary")]   # managed users only


def test_time_up_is_logged_for_the_summary():
    orch, sessions, _api, _agents = build()
    orch.timekeeper.set_limit_today("kevin", NOW.date(), 0)
    sessions.sessions = [session()]
    for _ in range(3):
        tick(orch)
    assert orch.store.count_events("kevin", "time_up", 0, 9e12) == 1


def test_summary_sensor():
    cfg = Config.from_dict(yaml.safe_load(BASE))
    factory = EntityFactory(cfg)
    sensor = next(e for e in factory.user_entities("kevin", cfg.user_policy("kevin"), True) if e.object_id == "user_kevin_time_last_week")
    assert sensor.spec["value"] == "last_week_minutes" and sensor.spec["attributes"] == "weekly_summary"


# -- glue ----------------------------------------------------------------------------------------------------------------

def test_site_rules_match_on_the_host_when_only_the_host_is_known():
    text = BASE.replace("        - {name: YouTube, sites: [youtube.com], daily_limit_minutes: 30}",
                        "        - {name: Music, sites: [music.youtube.com], blocked: true}")
    orch, sessions, _api, agents = setup(text)
    orch.on_agent_report(2, AgentReport(session_id=2, ts=0.0, foreground=ForegroundInfo(pid=5, name="chrome.exe"),
                                        domain="youtube.com", host="music.youtube.com"))
    tick(orch)
    assert orch._users["kevin"].activity.url == "http://music.youtube.com"
    assert agents.of("close") and agents.of("close")[-1]["tab"] is True
    agents.sent.clear()
    orch.on_agent_report(2, AgentReport(session_id=2, ts=0.0, foreground=ForegroundInfo(pid=5, name="chrome.exe"),
                                        domain="youtube.com", host="www.youtube.com"))
    tick(orch)
    assert not agents.of("close")


def test_an_oversized_agent_message_fails_the_pending_screenshot():
    orch, _sessions, api, _agents = setup("screenshots: {enabled: true}\n" + BASE)
    assert orch.request_screenshot("kevin")["ok"]
    orch.on_agent_message(2, {"type": "oversized", "limit": 8 * 1024 * 1024})
    tick(orch, 1)
    assert not orch._pending_screenshots
    assert "too large" in user_state(api)["limit_details"]["last_refusal"]["reason"]


def test_a_start_from_the_last_good_configuration_is_reported():
    orch, sessions, api, _agents = build()
    orch.config_problem = "config.yaml: line 3: mapping values are not allowed here"
    sessions.sessions = [session()]
    tick(orch)
    tick(orch)
    pc = api.pc
    assert pc["degraded"] is True and "line 3" in pc["config_problem"] and "line 3" in pc["health"]["config_problem"]
    assert events(api, "config_invalid") == [("pc", "config_invalid")] and "config_invalid" in PC_TRIGGERS
    assert orch.api_health()["degraded"] is True
    assert orch.reload_config(Config.from_dict(yaml.safe_load(BASE)))["ok"]   # a configuration that works is in force
    tick(orch)
    tick(orch)
    assert api.pc["degraded"] is False and api.pc["config_problem"] is None


def test_a_working_configuration_in_the_data_folder_is_remembered(monkeypatch, tmp_path):
    from pclink import config as config_module
    from pclink import hardening

    kept = []
    monkeypatch.setattr(hardening, "remember_good_config", lambda path, folder: kept.append((path, folder)))
    monkeypatch.setattr(config_module, "data_dir", lambda: str(tmp_path))
    orch, *_ = build()
    new = Config.from_dict(yaml.safe_load(BASE))
    new.source_path = str(tmp_path / "elsewhere" / "config.yaml")   # not the service's own file: left alone
    orch.reload_config(new)
    assert kept == []
    new = Config.from_dict(yaml.safe_load(BASE))
    new.source_path = str(tmp_path / "config.yaml")
    orch.reload_config(new)
    assert kept == [(str(tmp_path / "config.yaml"), str(tmp_path))]


def test_the_update_entity_names_release_notes_and_progress():
    cfg = Config.from_dict(yaml.safe_load("updates: {enabled: true}\n" + BASE))
    factory = EntityFactory(cfg)
    update = next(e for e in factory.pc_entities({}) if e.object_id == "pclink_update")
    assert update.spec["release_notes"] == "pclink_update_release_notes" and update.spec["percentage"] == "pclink_update_percentage"


def test_closing_by_name_from_home_assistant_starts_no_escalation():
    orch, _sessions, api, agents = setup(BASE + "apps:\n  - {name: Minecraft, process: javaw.exe}\n")
    report(orch, name="roblox.exe", pid=9)
    tick(orch)
    assert orch._users["kevin"].closing["step"] == "process"
    assert orch.api_command({"action": "close_app", "name": "Minecraft"})["ok"]
    for _ in range(3):   # the app was not running: "terminated 0", without a pid
        orch.on_agent_message(2, {"type": "closed", "name": "javaw.exe", "pid": 0, "result": "terminated 0"})
    report(orch, name="roblox.exe", pid=9)
    tick(orch, 1)
    assert orch._users["kevin"].closing["step"] == "process" and orch._users["kevin"].closing["failed"] == 0
    assert user_state(api)["tamper"] is False and not events(api, "app_close_failed")


@pytest.mark.parametrize("command", [
    {"action": "run", "name": "backup"}, {"action": "reload"}, {"action": "pclink_update_install"},
    {"action": "set", "entity": "cmd_backup", "value": "PRESS"}, {"action": "set", "entity": "reload_config", "value": "PRESS"},
    {"action": "set", "entity": "pclink_update_install", "value": "PRESS"},
])
def test_what_the_api_treats_as_admin_is_what_core_runs_as_such(command):
    """The API asks for the admin level for exactly the commands that reach these branches of the
    dispatcher; a command that does the same under another name would slip past it."""
    assert is_admin_command(command)
    orch, *_ = build(BASE + "commands:\n  - {name: Backup, command: 'cmd /c exit 0', run_as: user}\n")
    ran = []
    orch._run_custom = lambda slug: ran.append(("run", slug))
    orch.reload_config = lambda new_cfg=None: ran.append(("reload",)) or {"ok": True}
    orch._install_self_update = lambda: ran.append(("update",)) or {"ok": True}
    orch.api_command(command)
    assert len(ran) == 1
    for other in ({"action": "set_profile", "profile": "x"}, {"action": "time_request_answer", "user": "kevin", "approve": True},
                  {"action": "clear_tamper"}, {"action": "refresh"}, {"action": "set", "entity": "user_kevin_profile", "value": "x"}):
        assert not is_admin_command(other)
    assert json.dumps(command)
    assert date.today()
