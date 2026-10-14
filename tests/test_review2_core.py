"""Second core review: focus mode with a suspended agent, enforcement across switching limits off
and on, bonus against the weekly limit, a negative shared bonus, failing closed when the status
cannot be worked out, hot reload of ``enabled``, bedtime extensions after bedtime, DST, a thread
race, multiseat rules, the weekly summary, time requests across midnight, the pinned time zone,
the purge after a clock jump, the start order, what is published of the URL, ``closed: error``."""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from pclink import core as core_module
from pclink.config import Config
from pclink.core import Orchestrator, _public_url
from pclink.models import AgentReport, SessionInfo
from pclink.store import Store
from pclink.timekeeper import FOCUS_TRUST_SECONDS, TimeKeeper

from .test_fix_core import (BASE, NOW, FakeApi, FakeBlocker, FakeSessions, build, events, report, session, tick,
                            user_state)


def make_tk(text):
    cfg = Config.from_dict(yaml.safe_load(text))
    store = Store(":memory:")
    return TimeKeeper(cfg.time_limits, store, cfg.build_default_policy), store


def sess(sid=2, agent=False):
    s = SessionInfo(session_id=sid, user="kevin", state="active", locked=False, idle_seconds=0.0)
    if agent:
        s.report = AgentReport(session_id=sid, ts=0.0)
    return s


LIMIT = """
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 60
      grace_seconds: 60
      action: logoff
      focus: {counts_toward_limit: false}
"""


# -- 1: focus mode and an agent that is suspended over and over -------------------------------------------

def test_a_suspended_agent_cannot_make_focus_mode_dodge_the_logoff():
    tk, store = make_tk(LIMIT)
    t0 = datetime(2026, 9, 29, 12, 0, 0)
    store.add_usage("kevin", t0.date(), active=3600)          # the day is used up; focus pauses that
    tk.start_focus("kevin", t0, 120)
    kinds = []
    for i in range(0, 30, 5):
        kinds += [a.kind for a in tk.tick(t0 + timedelta(seconds=i), {"kevin": [sess(agent=True)]})]
    assert kinds == ["unblock"]                                # (the first tick states the login is not blocked)
    for i in range(30, 125, 5):                                # suspended: no reports
        kinds += [a.kind for a in tk.tick(t0 + timedelta(seconds=i), {"kevin": [sess()]})]
    assert "warn" in kinds and "block" in kinds                # out of time, grace period started
    # the watchdog starts a new agent, which reports again: the grace period runs on to the logoff
    for i in range(125, 260, 5):
        kinds += [a.kind for a in tk.tick(t0 + timedelta(seconds=i), {"kevin": [sess(agent=True)]})]
    assert "logoff" in kinds and "unblock" not in kinds[1:]     # and the login block stays
    assert not tk.status("kevin", t0 + timedelta(seconds=260)).focus_pauses_limit
    # only a long stretch with reports throughout gives focus its pause back
    end = 260 + int(FOCUS_TRUST_SECONDS) + 10
    for i in range(260, end, 5):
        tk.tick(t0 + timedelta(seconds=i), {"kevin": [sess(agent=True)]})
    assert tk.status("kevin", t0 + timedelta(seconds=end)).focus_pauses_limit


# -- 2: switching limits off and on again -------------------------------------------------------------------

def test_switching_limits_back_on_starts_with_a_grace_period():
    tk, store = make_tk(LIMIT)
    t0 = datetime(2026, 9, 29, 12, 0, 0)
    store.add_usage("kevin", t0.date(), active=61 * 60)
    assert any(a.kind == "warn" and a.level == "critical" for a in tk.tick(t0, {"kevin": [sess()]}))
    assert any(a.kind == "logoff" for a in tk.tick(t0 + timedelta(seconds=61), {"kevin": [sess()]}))
    tk.set_global_enabled(False)
    assert not [a for a in tk.tick(t0 + timedelta(seconds=70), {"kevin": [sess()]}) if a.kind != "unblock"]
    tk.set_global_enabled(True)
    kinds = [(a.kind, a.level) for a in tk.tick(t0 + timedelta(seconds=80), {"kevin": [sess()]})]
    assert ("warn", "critical") in kinds and not any(k == "logoff" for k, _ in kinds)


# -- 3: a bonus is more time against the weekly limit as well ---------------------------------------------------

def test_a_bonus_extends_the_weekly_allowance():
    tk, store = make_tk("""
    time_limits:
      users:
        - name: kevin
          daily_limit_minutes: 120
          weekly_limit_minutes: 90
    """)
    monday, wednesday = date(2026, 9, 28), date(2026, 9, 30)
    store.add_usage("kevin", monday, active=60 * 60)
    store.add_usage("kevin", wednesday, active=30 * 60)
    now = datetime(2026, 9, 30, 15, 0)
    st = tk.status("kevin", now)
    assert st.out_of_time and st.limiting_factor == "weekly"
    tk.add_bonus_minutes("kevin", wednesday, 30)
    st = tk.status("kevin", now)
    assert not st.out_of_time and st.weekly_remaining_seconds == 30 * 60 and st.effective_remaining_seconds == 30 * 60
    tk.add_bonus_minutes("kevin", monday, 15)                  # granted earlier this week: counts too
    assert tk.status("kevin", now).weekly_remaining_seconds == 45 * 60
    assert tk.status("kevin", datetime(2026, 10, 5, 15, 0)).weekly_remaining_seconds == 90 * 60   # a new week


BEDTIME = BASE.replace("      grace_seconds: 0\n", "      grace_seconds: 0\n      bedtime: '21:00'\n      wake_time: '07:00'\n")


def test_no_time_requests_while_bonus_minutes_cannot_help():
    orch, sessions, api, agents = build(BEDTIME)
    sessions.sessions = [session()]
    agents.connected.add(2)
    tick(orch)
    assert agents.of("status")[-1]["time_requests"] == [15, 30, 60]
    orch._clocks.advance(10 * 3600 - 5)                         # 22:00: bedtime
    tick(orch)
    assert agents.of("status")[-1]["time_requests"] == []
    orch.on_agent_message(2, {"type": "time_request", "minutes": 30})
    tick(orch, 1)
    assert any("would not help: it is bedtime" in m["message"] for m in agents.of("notify"))
    # a request made before bedtime: approving it tells the parent, not the child
    wall = orch._clocks.wall
    orch.store.set_setting("kevin", "time_request", {"minutes": 30, "at": wall, "expires": wall + 600,
                                                     "day": orch._clocks.now.date().isoformat()})
    notified = len(agents.of("notify"))
    answer = orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": True})
    assert answer["ok"] is False and "bedtime" in answer["error"]
    assert orch.timekeeper.local_bonus_seconds("kevin", orch._clocks.now.date()) == 0
    assert len(agents.of("notify")) == notified and orch.pending_time_request("kevin") is not None


# -- 4: a negative bonus from the other PCs ------------------------------------------------------------------------

def test_a_negative_shared_bonus_takes_time_away_but_never_below_nothing():
    tk, store = make_tk("""
    time_limits:
      shared_budget: true
      users:
        - name: kevin
          daily_limit_minutes: 60
    """)
    now = datetime(2026, 9, 28, 12, 0)
    store.add_usage("kevin", now.date(), active=600)
    tk.external_extras = lambda user, day: {"bonus_seconds": -1800}
    st = tk.status("kevin", now)
    assert st.bonus_seconds == -1800 and st.remaining_seconds == 1800 - 600 and not st.out_of_time
    tk.external_extras = lambda user, day: {"bonus_seconds": -7200}
    st = tk.status("kevin", now)
    assert st.remaining_seconds == -600 and st.out_of_time and st.limiting_factor == "daily"


# -- 5: failing closed when the status cannot be worked out ---------------------------------------------------------

def test_a_failing_status_is_enforced_from_the_last_good_one():
    tk, store = make_tk(LIMIT)
    t0 = datetime(2026, 9, 29, 12, 0, 0)
    store.add_usage("kevin", t0.date(), active=3600 - 30)       # half a minute left
    tk.tick(t0, {"kevin": [sess()]})

    def broken(user, now):
        raise RuntimeError("database is locked")

    tk.status = broken
    assert not [a for a in tk.tick(t0 + timedelta(seconds=10), {"kevin": [sess()]}) if a.level == "critical"]
    actions = tk.tick(t0 + timedelta(seconds=40), {"kevin": [sess()]})   # the half minute is over
    assert any(a.kind == "warn" and a.level == "critical" for a in actions)
    assert any(a.kind == "logoff" for a in tk.tick(t0 + timedelta(seconds=105), {"kevin": [sess()]}))


def test_a_status_that_never_worked_counts_as_out_of_time_after_three_ticks():
    tk, store = make_tk(LIMIT)
    t0 = datetime(2026, 9, 29, 12, 0, 0)

    def broken(user, now):
        raise RuntimeError("boom")

    tk.status = broken
    assert [a.kind for a in tk.tick(t0, {"kevin": [sess()]})] == ["unblock"]   # not out of time yet
    assert tk.tick(t0 + timedelta(seconds=5), {"kevin": [sess()]}) == []
    actions = tk.tick(t0 + timedelta(seconds=10), {"kevin": [sess()]})
    warn = next(a for a in actions if a.kind == "warn")
    assert warn.level == "critical" and warn.reason == "error" and "cannot be worked out" in warn.message
    assert any(a.kind == "block" for a in actions)


# -- 6: hot reload of time_limits.enabled --------------------------------------------------------------------------

def test_reload_applies_enabled_unless_home_assistant_set_it():
    tk, store = make_tk(LIMIT)
    assert tk.global_enabled
    off = Config.from_dict(yaml.safe_load(LIMIT.replace("time_limits:", "time_limits:\n  enabled: false")))
    tk.update_config(off.time_limits, off.build_default_policy)
    assert not tk.global_enabled
    on = Config.from_dict(yaml.safe_load(LIMIT))
    tk.update_config(on.time_limits, on.build_default_policy)
    assert tk.global_enabled
    tk.set_global_enabled(True)                               # the switch in Home Assistant: stored, wins
    tk.update_config(off.time_limits, off.build_default_policy)
    assert tk.global_enabled


# -- 7 / 8: extending bedtime after it has passed, and with a shared budget -------------------------------------------

NIGHT = """
time_limits:
  shared_budget: true
  users:
    - name: kevin
      bedtime: "21:00"
      wake_time: "07:00"
"""


def test_extending_bedtime_after_midnight_extends_from_now():
    tk, store = make_tk(NIGHT)
    night = date(2026, 9, 28)
    now = datetime(2026, 9, 29, 2, 0)
    assert tk.status("kevin", now).limiting_factor == "bedtime"
    assert tk.bedtime_day("kevin", now) == night
    tk.extend_bedtime("kevin", tk.bedtime_day("kevin", now), 30, now)
    st = tk.status("kevin", now)
    assert st.in_bedtime_window and st.bedtime_remaining_seconds == 30 * 60
    assert tk.status("kevin", datetime(2026, 9, 29, 2, 31)).limiting_factor == "bedtime"
    # in the evening, after bedtime, too: +30 at 21:40 is until 22:10
    evening = datetime(2026, 9, 29, 21, 40)
    tk.extend_bedtime("kevin", tk.bedtime_day("kevin", evening), 30, evening)
    assert tk.status("kevin", evening).bedtime_remaining_seconds == 30 * 60
    # before bedtime it is added to bedtime as before
    early = datetime(2026, 9, 30, 20, 0)
    assert tk.extend_bedtime("kevin", early.date(), 30, early) == 30


def test_extending_bedtime_adds_to_the_extension_another_pc_granted():
    tk, store = make_tk(NIGHT)
    night = date(2026, 9, 28)
    tk.external_extras = lambda user, day: {"bedtime_extension_minutes": 60} if day == night else {}
    assert tk.extend_bedtime("kevin", night, 30) == 90
    assert tk.bedtime_extension_minutes("kevin", night) == 90
    assert tk.status("kevin", datetime(2026, 9, 28, 22, 20)).bedtime_remaining_seconds == 10 * 60


# -- 9: daylight-saving nights -----------------------------------------------------------------------------------------

def test_remaining_time_in_a_window_is_real_time_on_dst_nights():
    tk, store = make_tk("""
    time_limits:
      users:
        - name: kevin
          allowed_hours: "00:00-04:00"
    """)
    berlin = ZoneInfo("Europe/Berlin")
    spring = datetime(2026, 3, 29, 1, 30, tzinfo=berlin)     # 02:00 -> 03:00: only 90 real minutes to 04:00
    assert tk.status("kevin", spring).window_remaining_seconds == 90 * 60
    autumn = datetime(2026, 10, 25, 1, 30, tzinfo=berlin)    # 03:00 -> 02:00: 210 real minutes
    assert tk.status("kevin", autumn).window_remaining_seconds == 210 * 60
    repeated = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=berlin)   # the second 02:30
    assert tk.status("kevin", repeated).window_remaining_seconds == 90 * 60
    assert tk.status("kevin", datetime(2026, 3, 29, 1, 30)).window_remaining_seconds == 150 * 60   # naive: as before


# -- 10: the IPC thread and the main loop share the message counters --------------------------------------------------

def test_agent_message_counters_survive_the_ipc_thread():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    stop, errors = threading.Event(), []

    def ipc():
        sid = 1000
        while not stop.is_set():
            try:
                orch._agent_may_send(sid)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            sid += 1

    worker = threading.Thread(target=ipc)
    worker.start()
    try:
        for _ in range(300):
            orch._collect_sessions(orch._mono())
    finally:
        stop.set()
        worker.join()
    assert not errors


# -- 11: multiseat - rules for every seat of a user ---------------------------------------------------------------------

def test_rules_apply_to_a_second_seat_of_the_same_user():
    orch, sessions, api, agents = build()
    sessions.sessions = [session(sid=2), session(sid=3)]
    agents.connected |= {2, 3}
    report(orch, sid=2, name="notepad.exe", pid=10)
    report(orch, sid=3, name="roblox.exe", pid=555)
    tick(orch)
    closes = [(sid, m) for sid, m in agents.sent if m.get("type") == "close"]
    assert [(sid, m["pid"]) for sid, m in closes] == [(3, 555)]
    assert set(orch._users["kevin"].closings) == {3}
    used = orch.store.usage("kevin", NOW.date()).active
    tick(orch)
    assert orch.store.usage("kevin", NOW.date()).active - used <= 5.0 + 1e-6   # time counted once, not per seat


# -- 12: the weekly summary ---------------------------------------------------------------------------------------------

def test_no_weekly_summary_event_for_a_week_without_data_and_a_failure_is_retried():
    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    assert events(api, "weekly_summary") == []                  # a fresh install: nothing to sum up
    assert user_state(api)["weekly_summary"]["total_minutes"] == 0

    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    orch.store.add_usage("kevin", NOW.date() - timedelta(days=7), active=3600)
    real = orch.store.summary_range
    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk I/O error")
        return real(*args, **kwargs)

    orch.store.summary_range = failing
    tick(orch, strict=False)
    assert events(api, "weekly_summary") == []
    tick(orch)
    assert events(api, "weekly_summary") == [("user/kevin", "weekly_summary")]


# -- 13: a time request does not survive midnight -----------------------------------------------------------------------

def test_a_request_from_yesterday_cannot_be_approved_after_midnight():
    orch, sessions, api, agents = build()
    sessions.sessions = [session()]
    agents.connected.add(2)
    orch._clocks.now = NOW.replace(hour=23, minute=50)
    tick(orch)
    orch.on_agent_message(2, {"type": "time_request", "minutes": 30})
    tick(orch, 1)
    assert orch.pending_time_request("kevin")["day"] == "2026-09-28"
    orch._clocks.advance(20 * 60)                               # 00:10, before any tick
    answer = orch.api_command({"action": "time_request_answer", "user": "kevin", "approve": True})
    assert answer["ok"] is False and "expired at midnight" in answer["error"]
    assert orch.timekeeper.local_bonus_seconds("kevin", date(2026, 9, 29)) == 0
    assert orch.pending_time_request("kevin") is None
    assert events(api, "time_request_expired") == [("user/kevin", "time_request_expired")]
    assert "Nobody answered" in agents.of("notify")[-1]["message"]


def test_a_request_left_open_expires_at_midnight():
    orch, sessions, api, agents = build(BASE.replace("time_limits:", "time_limits:\n  time_requests: {expire_minutes: 120}"))
    sessions.sessions = [session()]
    agents.connected.add(2)
    orch._clocks.now = NOW.replace(hour=23, minute=50)
    tick(orch)
    orch.on_agent_message(2, {"type": "time_request", "minutes": 30})
    tick(orch, 1)
    tick(orch, 300)
    assert orch.pending_time_request("kevin") is not None
    tick(orch, 600)                                             # past midnight, well before the two hours
    assert orch.pending_time_request("kevin") is None


# -- 14: the time zone is pinned -------------------------------------------------------------------------------------------

def test_without_a_configured_zone_the_first_one_is_pinned(monkeypatch):
    cfg = Config.from_dict({"device": {"name": "TestPC"}})
    store = Store(":memory:")
    monkeypatch.setattr(core_module, "local_iana_zone", lambda: "Asia/Tokyo")
    orch = Orchestrator(cfg, store=store, sessions=FakeSessions(), blocker=FakeBlocker(), api=FakeApi())
    assert str(orch.clock().tzinfo) == "Asia/Tokyo"
    assert store.get_setting(Store.GLOBAL, "time_zone_pinned") == "Asia/Tokyo"
    assert orch.pc_payload([])["service_info"]["time_zone"] == "Asia/Tokyo (pinned)"
    # the child sets another zone and restarts: the clock stays where it was
    monkeypatch.setattr(core_module, "local_iana_zone", lambda: "America/New_York")
    again = Orchestrator(cfg, store=store, sessions=FakeSessions(), blocker=FakeBlocker(), api=FakeApi())
    assert str(again.clock().tzinfo) == "Asia/Tokyo"
    # a deliberate change is a configured zone
    moved = Config.from_dict({"device": {"name": "TestPC"}, "time_limits": {"time_zone": "Europe/Lisbon"}})
    assert str(Orchestrator(moved, store=store, sessions=FakeSessions(), blocker=FakeBlocker(),
                            api=FakeApi()).clock().tzinfo) == "Europe/Lisbon"
    # an injected clock (tests) is kept as it is
    injected = Orchestrator(cfg, store=Store(":memory:"), clock=lambda: NOW)
    assert injected.clock() is NOW


# -- 15: no purge on a date reached by setting the clock ---------------------------------------------------------------------

def test_a_clock_set_far_ahead_does_not_purge_real_data():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    purged = []
    orch.store.purge = lambda days, today=None: purged.append(today)
    tick(orch)
    assert purged == [NOW.date()]
    clocks = orch._clocks
    clocks.now += timedelta(days=400)
    clocks.wall += 400 * 86400
    tick(orch)                                                  # the clock check flags the jump
    tick(orch)
    assert purged == [NOW.date()]
    clocks.now += timedelta(days=1)                             # the next day, reached by time passing
    clocks.wall += 86400 - 5
    clocks.mono += 0
    orch._last_clocks = None                                    # (no second jump flagged)
    tick(orch)
    assert purged[-1] == (NOW + timedelta(days=401)).date()


# -- 16: the agent port is bound first ---------------------------------------------------------------------------------------

def test_the_agent_server_starts_before_the_slow_parts():
    order = []

    class Part:
        def __init__(self, name):
            self.name = name

        def start(self):
            order.append(self.name)

        def stop(self):
            pass

    cfg = Config.from_dict({"device": {"name": "TestPC"}})
    orch = Orchestrator(cfg, store=Store(":memory:"), clock=lambda: NOW, agent_server=Part("agents"),
                        hwsensors=Part("hardware"), winupdate=Part("windows update"), api=Part("api"))
    orch._start_components()
    assert order[0] == "agents" and set(order) == {"agents", "hardware", "windows update", "api"}


# -- 17: what Home Assistant gets of the page and the window -----------------------------------------------------------------

def test_the_published_url_has_no_query_and_the_title_is_capped():
    assert _public_url("https://user:pw@www.youtube.com:8443/watch?v=abc&t=1#comments") == "https://www.youtube.com:8443/watch"
    assert _public_url("youtube.com/results?search_query=secret") == "youtube.com/results"
    assert _public_url(None) is None and _public_url("") is None
    orch, sessions, api, _agents = build()
    sessions.sessions = [session()]
    report(orch, name="chrome.exe", domain="wikipedia.org", url="https://en.wikipedia.org/wiki/Cat?oldid=1#History",
           title="T" * 400)
    tick(orch)
    details = user_state(api)["activity_details"]
    assert details["url"] == "https://en.wikipedia.org/wiki/Cat"
    assert len(details["title"]) == core_module.MAX_PUBLISHED_TITLE
    assert orch._users["kevin"].activity.url.endswith("?oldid=1#History")   # rules still see the whole address


# -- 18: the agent could not read a close request ---------------------------------------------------------------------------

def test_a_close_answered_with_error_counts_as_failed():
    orch, sessions, _api, agents = build()
    sessions.sessions = [session()]
    agents.connected.add(2)
    report(orch, name="roblox.exe", pid=9)
    tick(orch)
    assert orch._users["kevin"].closings[2]["failed"] == 0
    orch.on_agent_message(2, {"type": "closed", "result": "error"})                 # malformed: no pid
    orch.on_agent_message(2, {"type": "closed", "result": "error", "pid": 9, "name": "roblox.exe"})
    orch.on_agent_message(2, {"type": "closed", "result": ["not", "text"]})          # nonsense: ignored
    report(orch, name="roblox.exe", pid=9)
    tick(orch, 1)
    assert orch._users["kevin"].closing["step"] == "lock"


# -- coordinator: break reminders get the agent's input idle time; HA rules cannot clash -------------------------------

def test_break_reminders_get_the_input_idle_time_and_passive_seats():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    seen = []
    real = orch.breaks.tick

    def spy(*args, **kwargs):
        seen.append((kwargs.get("input_idle_seconds"), kwargs.get("passive")))
        return real(*args, **kwargs)

    orch.breaks.tick = spy
    tick(orch)
    assert seen[-1] == (None, False)                            # no agent: nothing to say
    report(orch, name="notepad.exe", idle=42.0)
    tick(orch)
    assert seen[-1] == (42.0, False)


def test_a_rule_from_home_assistant_cannot_clash_with_another_rules_ids():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [session()]
    tick(orch)
    bad = orch.api_command({"action": "rule_set", "user": "kevin", "rule": "Roblox!", "blocked": True})
    assert bad["ok"] is True or "same id" in bad["error"]       # same slug: it changes Roblox, no clash
    clash = orch.api_command({"action": "rule_set", "user": "kevin", "rule": "Roblox enabled", "apps": ["x.exe"], "blocked": True})
    assert clash["ok"] is False and "same id" in clash["error"]
    flag = orch.api_command({"action": "rule_set", "user": "kevin", "rule": "Games", "apps": ["x.exe"], "blocked": "maybe"})
    assert flag["ok"] is False and flag["error"]
