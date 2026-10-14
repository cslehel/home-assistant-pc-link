"""Review 3: a grant restarts the grace period only when it can help; the local clock is
zone-aware (DST-correct real seconds) also when no IANA zone can be told."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from pclink.config import Config
from pclink.models import SessionInfo
from pclink.store import Store
from pclink.timekeeper import TimeKeeper, wall_seconds_ahead
from pclink import util
from pclink.util import LOCAL_TZ, LocalTimezone, make_clock


def make_tk(text):
    cfg = Config.from_dict(yaml.safe_load(text))
    store = Store(":memory:")
    return TimeKeeper(cfg.time_limits, store, cfg.build_default_policy), store


KID = [SessionInfo(session_id=2, user="kid", state="active", locked=False, idle_seconds=0.0)]


def in_grace(tk, now, used=0):
    """Tick once so that the grace period of 'kid' starts; returns its start."""
    tk.tick(now, {"kid": KID})
    rt = tk._runtime["kid"]
    assert rt.grace_started is not None
    return rt.grace_started


# -- 1. grants while bonus minutes cannot help ------------------------------------------------------

def test_bonus_outside_allowed_hours_keeps_the_grace_period_but_is_stored():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 60, allowed_hours: "08:00-20:00", grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 21, 0)
    started = in_grace(tk, now)
    assert tk.status("kid", now).limiting_factor == "hours"
    assert tk.add_bonus_minutes("kid", now.date(), 30) == 1800
    assert tk._runtime["kid"].grace_started == started          # the countdown goes on
    assert tk.local_bonus_seconds("kid", now.date()) == 1800      # ... and the bonus is kept
    tk.set_bonus_minutes("kid", now.date(), 60)
    tk.earn_minutes("kid", now.date(), 10, "chores")
    assert tk._runtime["kid"].grace_started == started
    # the grace period runs out as it would have without the grant
    tk.tick(now + timedelta(seconds=61), {"kid": KID})
    assert tk._runtime["kid"].enforced


def test_bonus_while_enforced_at_bedtime_does_not_forget_the_enforcement():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 600, bedtime: "21:00", wake_time: "07:00", grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 21, 30)
    in_grace(tk, now)
    tk.tick(now + timedelta(seconds=61), {"kid": KID})
    rt = tk._runtime["kid"]
    assert rt.enforced and tk.status("kid", now).limiting_factor == "bedtime"
    tk.add_bonus_minutes("kid", now.date(), 30)
    assert rt.enforced
    # a bedtime extension does help: the countdown is reset
    tk.extend_bedtime("kid", now.date(), 30, now=now + timedelta(seconds=61))
    assert not rt.enforced and rt.grace_started is None


def test_a_bedtime_extension_that_falls_short_restarts_nothing():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 600, bedtime: "21:00", wake_time: "07:00", grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 22, 0)
    started = in_grace(tk, now)
    tk.set_bedtime_extension("kid", now.date(), 30)            # until 21:30: still bedtime at 22:00
    assert tk._runtime["kid"].grace_started == started
    tk.set_bedtime_extension("kid", now.date(), 90)            # until 22:30: helps
    assert tk._runtime["kid"].grace_started is None


def test_bonus_during_a_pause_and_lifting_the_pause():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 60, grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 15, 0)
    tk.set_manually_blocked("kid", True)
    started = in_grace(tk, now)
    tk.add_bonus_minutes("kid", now.date(), 30)
    assert tk._runtime["kid"].grace_started == started
    tk.set_manually_blocked("kid", False)                       # the user has time: reset
    assert tk._runtime["kid"].grace_started is None


def test_lifting_a_pause_outside_allowed_hours_restarts_nothing():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 60, allowed_hours: "08:00-20:00", grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 21, 0)
    tk.set_manually_blocked("kid", True)
    started = in_grace(tk, now)
    tk.set_manually_blocked("kid", False)
    assert tk._runtime["kid"].grace_started == started


def test_bonus_on_the_daily_or_weekly_budget_restarts_the_grace_period():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 10, weekly_limit_minutes: 600, grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 15, 0)
    store.add_usage("kid", now.date(), active=3600)
    in_grace(tk, now)
    assert tk.status("kid", now).limiting_factor == "daily"
    tk.add_bonus_minutes("kid", now.date(), 5)       # not enough, but minutes are what is short: reset
    assert tk._runtime["kid"].grace_started is None
    tk2, store2 = make_tk("""
time_limits:
  users: [{name: kid, weekly_limit_minutes: 30, grace_seconds: 60}]
""")
    store2.add_usage("kid", now.date(), active=3600)
    in_grace(tk2, now)
    assert tk2.status("kid", now).limiting_factor == "weekly"
    tk2.add_bonus_minutes("kid", now.date(), 60)
    assert tk2._runtime["kid"].grace_started is None


def test_a_raised_limit_outside_allowed_hours_restarts_nothing():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 10, allowed_hours: "08:00-20:00", grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 21, 0)
    store.add_usage("kid", now.date(), active=3600)
    started = in_grace(tk, now)
    tk.set_limit_today("kid", now.date(), 120)
    tk.set_weekly_limit("kid", 0)
    assert tk._runtime["kid"].grace_started == started


def test_the_attached_clock_is_what_a_grant_is_judged_by():
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 60, allowed_hours: "08:00-20:00", grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 21, 0)
    started = in_grace(tk, now)
    tk.clock = lambda: datetime(2026, 10, 13, 19, 0)            # inside the hours: the bonus helps
    tk.add_bonus_minutes("kid", now.date(), 30)
    assert tk._runtime["kid"].grace_started is None
    assert started is not None


def test_a_status_that_fails_after_a_grant_keeps_the_countdown(monkeypatch):
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 10, grace_seconds: 60}]
""")
    now = datetime(2026, 10, 13, 15, 0)
    store.add_usage("kid", now.date(), active=3600)
    started = in_grace(tk, now)
    monkeypatch.setattr(tk, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    tk.add_bonus_minutes("kid", now.date(), 60)
    assert tk._runtime["kid"].grace_started == started


# -- 2. the local clock carries a DST-correct zone ----------------------------------------------------

BUDAPEST = ZoneInfo("Europe/Budapest")


@pytest.fixture
def budapest_os(monkeypatch):
    """Make the 'operating system' local time Europe/Budapest for LocalTimezone."""
    def offset_at(ts):
        return int(datetime.fromtimestamp(ts, BUDAPEST).utcoffset().total_seconds())
    monkeypatch.setattr(LocalTimezone, "_offset_at", staticmethod(offset_at))


def test_make_clock_without_a_zone_is_aware_local_time():
    now = make_clock(None)()
    assert now.tzinfo is LOCAL_TZ
    assert abs(now.timestamp() - datetime.now().timestamp()) < 5
    assert now.replace(tzinfo=None) - datetime.now() < timedelta(seconds=5)
    assert make_clock("Asia/Tokyo")().utcoffset() == timedelta(hours=9)   # a named zone as before


@pytest.mark.parametrize("utc", [
    datetime(2026, 3, 29, 0, 30), datetime(2026, 3, 29, 1, 0), datetime(2026, 3, 29, 1, 30),
    datetime(2026, 10, 25, 0, 15), datetime(2026, 10, 25, 0, 45), datetime(2026, 10, 25, 1, 15),
    datetime(2026, 10, 25, 1, 45), datetime(2026, 7, 1, 12, 0), datetime(2026, 1, 1, 12, 0),
])
def test_local_timezone_matches_the_zone_database(budapest_os, utc):
    utc = utc.replace(tzinfo=timezone.utc)
    mine, ref = utc.astimezone(LOCAL_TZ), utc.astimezone(BUDAPEST)
    assert mine.replace(tzinfo=None) == ref.replace(tzinfo=None)
    assert mine.fold == ref.fold and mine.utcoffset() == ref.utcoffset()
    assert mine.timestamp() == utc.timestamp()          # and back


def test_local_timezone_resolves_the_repeated_and_the_skipped_hour(budapest_os):
    repeated = datetime(2026, 10, 25, 2, 30, tzinfo=LOCAL_TZ)
    assert repeated.utcoffset() == timedelta(hours=2) and repeated.replace(fold=1).utcoffset() == timedelta(hours=1)
    skipped = datetime(2026, 3, 29, 2, 30, tzinfo=LOCAL_TZ)
    assert skipped.utcoffset() == timedelta(hours=1) and skipped.replace(fold=1).utcoffset() == timedelta(hours=2)
    for naive in (datetime(2026, 10, 25, 2, 30), datetime(2026, 3, 29, 2, 30), datetime(2026, 6, 1, 9, 0)):
        for fold in (0, 1):
            ref = naive.replace(tzinfo=BUDAPEST, fold=fold)
            assert naive.replace(tzinfo=LOCAL_TZ, fold=fold).utcoffset() == ref.utcoffset()


def test_dst_warnings_are_in_real_seconds_on_the_local_clock(budapest_os):
    # 01:30 on the spring-forward night: "until 04:00" is 150 wall minutes, 90 real ones
    now = datetime(2026, 3, 29, 1, 30, tzinfo=LOCAL_TZ)
    assert wall_seconds_ahead(now, 150) == pytest.approx(90 * 60)
    # 01:30 on the fall-back night: "until 04:00" is 150 wall minutes, 210 real ones
    now = datetime(2026, 10, 25, 1, 30, tzinfo=LOCAL_TZ)
    assert wall_seconds_ahead(now, 150) == pytest.approx(210 * 60)
    # an ordinary day
    assert wall_seconds_ahead(datetime(2026, 6, 1, 20, 0, tzinfo=LOCAL_TZ), 60) == pytest.approx(3600)


def test_bedtime_warning_on_the_local_clock_comes_on_time(budapest_os):
    tk, store = make_tk("""
time_limits:
  users: [{name: kid, daily_limit_minutes: 600, bedtime: "04:00", wake_time: "08:00"}]
""")
    now = datetime(2026, 3, 29, 1, 30, tzinfo=LOCAL_TZ)
    st = tk.status("kid", now)
    assert st.bedtime_remaining_seconds == pytest.approx(90 * 60)


def test_the_service_runs_on_the_local_aware_clock():
    """The orchestrator with LOCAL_TZ times throughout: ticks, payloads, grants, reports."""
    from .test_ideas import make
    from .test_orchestrator import session

    orch, sessions, api, _agent = make(now=datetime(2026, 9, 28, 15, 0, tzinfo=LOCAL_TZ))
    sessions.sessions = [session()]
    orch.tick()
    orch._holder["now"] += timedelta(seconds=30)
    orch.tick()
    assert orch.store.usage("kevin", orch._holder["now"].date()).active == pytest.approx(30)
    assert orch.api_command({"action": "set_bonus", "user": "kevin", "minutes": 10, "pin": "4321"})["ok"]
    assert orch.api_command({"action": "focus", "user": "kevin", "minutes": 10, "pin": "4321"})["ok"]
    orch._holder["now"] += timedelta(seconds=30)
    orch.tick()
    payload = api.users["kevin"]
    assert payload["date"] == "2026-09-28"
    assert orch.pc_payload([])
    orch.user_payload("kevin", orch.clock(), [])
