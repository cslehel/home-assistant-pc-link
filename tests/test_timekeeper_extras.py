"""Weekly budgets, rollover, bedtime, parent presence, shared budgets, focus, rewards."""
from datetime import date, datetime, timedelta

import yaml

from pclink.config import Config
from pclink.models import SessionInfo
from pclink.store import Store
from pclink.timekeeper import TimeKeeper, bedtime_windows


def make_tk(users_yaml):
    cfg = Config.from_dict(yaml.safe_load(users_yaml))
    store = Store(":memory:")
    return TimeKeeper(cfg.time_limits, store, cfg.build_default_policy), store, cfg


def sess(sid=2, idle=0.0, presence=None, reason="", state="active", locked=False):
    s = SessionInfo(session_id=sid, user="kevin", state=state, locked=locked, idle_seconds=idle)
    s.presence = presence
    s.presence_reason = reason
    return s


# -- weekly budgets -------------------------------------------------------------------

WEEKLY = """
time_limits:
  week_start: mon
  users:
    - name: kevin
      daily_limit_minutes: 120
      weekly_limit_minutes: 300
"""


def test_weekly_limit_applies_across_days():
    tk, store, cfg = make_tk(WEEKLY)
    monday = date(2026, 9, 28)
    for offset in range(3):  # Mon..Wed 90 min each = 270
        store.add_usage("kevin", monday + timedelta(days=offset), active=90 * 60)
    now = datetime(2026, 10, 1, 12, 0, 0)  # Thursday
    st = tk.status("kevin", now)
    assert st.weekly_used_seconds == 270 * 60
    assert st.weekly_remaining_seconds == 30 * 60
    assert st.remaining_seconds == 120 * 60           # daily is untouched today
    assert st.effective_remaining_seconds == 30 * 60  # weekly is the tighter one
    store.add_usage("kevin", now.date(), active=31 * 60)
    st = tk.status("kevin", now)
    assert st.out_of_time and st.limiting_factor == "weekly"
    actions = tk.tick(now, {"kevin": [sess()]})
    assert any(a.kind == "block" and a.reason == "weekly" for a in actions)
    assert any(a.kind == "warn" and "week" in a.message for a in actions)


def test_weekly_limit_override_from_ha():
    tk, store, cfg = make_tk(WEEKLY)
    now = datetime(2026, 10, 1, 12, 0, 0)
    tk.set_weekly_limit("kevin", 1000)
    assert tk.status("kevin", now).weekly_limit_minutes == 1000
    tk.set_weekly_limit("kevin", None)
    assert tk.status("kevin", now).weekly_limit_minutes == 300


def test_week_start_configurable():
    tk, store, cfg = make_tk(WEEKLY.replace("week_start: mon", "week_start: sun"))
    assert tk.week_start(date(2026, 9, 30)) == date(2026, 9, 27)  # Wednesday -> previous Sunday


# -- rollover ----------------------------------------------------------------------

ROLLOVER = """
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 60
      rollover_max_minutes: 20
"""


def test_unused_minutes_roll_over_with_cap():
    tk, store, cfg = make_tk(ROLLOVER)
    yesterday = date(2026, 9, 27)
    today = date(2026, 9, 28)
    store.add_usage("kevin", yesterday, active=10 * 60)  # 50 unused -> capped at 20
    now = datetime(2026, 9, 28, 12, 0, 0)
    st = tk.status("kevin", now)
    assert st.rollover_seconds == 20 * 60
    assert st.remaining_seconds == (60 + 20) * 60
    # stored once; later changes to yesterday do not alter it
    store.add_usage("kevin", yesterday, active=100 * 60)
    assert tk.status("kevin", now).rollover_seconds == 20 * 60
    assert store.get_setting("kevin", f"rollover:{today.isoformat()}") == 20 * 60


def test_no_rollover_when_yesterday_was_fully_used_or_disabled():
    tk, store, cfg = make_tk(ROLLOVER)
    store.add_usage("kevin", date(2026, 9, 27), active=60 * 60)
    assert tk.status("kevin", datetime(2026, 9, 28, 12)).rollover_seconds == 0
    tk2, store2, _ = make_tk(ROLLOVER.replace("rollover_max_minutes: 20", "rollover_max_minutes: 0"))
    assert tk2.status("kevin", datetime(2026, 9, 28, 12)).rollover_seconds == 0


def test_rollover_chains_only_through_stored_values():
    tk, store, cfg = make_tk(ROLLOVER)
    # day 1 unused fully -> day 2 gets 20; day 2 unused -> day 3 gets min(60+20-0, cap) = 20 (cap holds)
    d1, d2, d3 = date(2026, 9, 26), date(2026, 9, 27), date(2026, 9, 28)
    assert tk.rollover_seconds("kevin", d2) == 20 * 60
    assert tk.rollover_seconds("kevin", d3) == 20 * 60


# -- bedtime -----------------------------------------------------------------------

BEDTIME = """
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 600
      bedtime: {weekdays: "21:00", weekend: "22:00"}
      wake_time: 07:00
      bedtime_parent_extension_minutes: 60
      warn_at_minutes: [15, 5]
"""


def test_bedtime_windows_and_status():
    tk, store, cfg = make_tk(BEDTIME)
    policy = cfg.user_policy("kevin")
    monday = date(2026, 9, 28)
    assert bedtime_windows(policy, monday, 0, 0) == [(7 * 60, 21 * 60)]
    assert bedtime_windows(policy, monday, 30, 0) == [(7 * 60, 21 * 60 + 30)]
    day = datetime(2026, 9, 28, 20, 0, 0)
    st = tk.status("kevin", day)
    assert st.has_bedtime and st.in_bedtime_window and not st.out_of_time
    assert st.bedtime_remaining_seconds == 60 * 60
    assert st.effective_remaining_seconds == 60 * 60  # bedtime is tighter than the 600-min budget
    night = datetime(2026, 9, 28, 21, 30, 0)
    st = tk.status("kevin", night)
    assert st.past_bedtime and st.out_of_time and st.limiting_factor == "bedtime" and st.block_desired
    early = datetime(2026, 9, 29, 6, 0, 0)
    assert tk.status("kevin", early).limiting_factor == "bedtime"
    actions = tk.tick(night, {"kevin": [sess()]})
    warn = next(a for a in actions if a.kind == "warn")
    assert warn.title == "Bedtime" and "bedtime" in warn.message.lower()


def test_bedtime_warning_names_bedtime():
    tk, store, cfg = make_tk(BEDTIME)
    now = datetime(2026, 9, 28, 20, 50, 0)
    actions = tk.tick(now, {"kevin": [sess()]})
    warn = next(a for a in actions if a.kind == "warn")
    assert warn.reason == "bedtime" and warn.message.startswith("Bedtime is in")


def test_bedtime_extension_manual_and_parent_presence():
    tk, store, cfg = make_tk(BEDTIME)
    night = datetime(2026, 9, 28, 21, 30, 0)
    assert tk.status("kevin", night).out_of_time
    tk.extend_bedtime("kevin", night.date(), 45)
    st = tk.status("kevin", night)
    assert not st.out_of_time and st.bedtime_extension_minutes == 45 and st.bedtime_minute == 21 * 60 + 45
    tk.set_bedtime_extension("kevin", night.date(), 0)
    assert tk.status("kevin", night).out_of_time
    tk.set_parent_present(True)
    st = tk.status("kevin", night)
    assert not st.out_of_time and st.bedtime_auto_extension_minutes == 60
    tk.set_parent_present(False)
    assert tk.status("kevin", night).out_of_time


def test_bedtime_past_midnight_spills_into_next_day():
    tk, store, cfg = make_tk("""
    time_limits:
      users:
        - name: kevin
          bedtime: "01:00"
          wake_time: "09:00"
    """)
    policy = cfg.user_policy("kevin")
    monday = date(2026, 9, 28)
    assert bedtime_windows(policy, monday, 0, 0) == [(0, 60), (9 * 60, 1440)]
    assert not tk.status("kevin", datetime(2026, 9, 28, 0, 30)).out_of_time
    assert tk.status("kevin", datetime(2026, 9, 28, 2, 0)).out_of_time
    assert not tk.status("kevin", datetime(2026, 9, 28, 23, 30)).out_of_time
    # an extension across midnight spills into tomorrow's windows
    assert bedtime_windows(policy, monday + timedelta(days=1), 0, 90) == [(0, 150), (9 * 60, 1440)]


# -- presence-aware counting -----------------------------------------------------------

PRESENCE = """
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 60
      count_idle: false
      idle_threshold_minutes: 10
    - name: strict
      daily_limit_minutes: 60
      count_idle: true
    - name: nopassive
      daily_limit_minutes: 60
      count_passive: false
"""


def test_session_counts_uses_presence():
    tk, store, cfg = make_tk(PRESENCE)
    kevin = cfg.user_policy("kevin")
    assert tk.session_counts(sess(idle=5, presence="active"), kevin)
    assert tk.session_counts(sess(idle=500, presence="passive", reason="audio playing"), kevin)
    # idle below the user's threshold still counts; beyond it does not
    assert tk.session_counts(sess(idle=300, presence="idle", reason="no input"), kevin)
    assert not tk.session_counts(sess(idle=700, presence="idle", reason="no input"), kevin)
    # monitor off / screensaver: away immediately, regardless of the threshold
    assert not tk.session_counts(sess(idle=45, presence="away", reason="display off"), kevin)
    assert not tk.session_counts(sess(idle=45, presence="away", reason="screensaver running"), kevin)
    # plain "no input" away still honours the threshold
    assert tk.session_counts(sess(idle=300, presence="away", reason="no input for 5 min"), kevin)
    strict = cfg.user_policy("strict")
    assert tk.session_counts(sess(idle=5000, presence="away", reason="display off"), strict)
    assert tk.session_counts(sess(idle=5000, presence="idle", reason="no input"), strict)
    assert not tk.session_counts(sess(idle=5000, presence="away", locked=True), strict)
    nop = cfg.user_policy("nopassive")
    assert not tk.session_counts(sess(idle=500, presence="passive"), nop)
    # no presence attached (old path) -> threshold only
    assert tk.session_counts(sess(idle=300), kevin) and not tk.session_counts(sess(idle=700), kevin)


# -- shared budget across PCs ------------------------------------------------------------

def test_shared_budget_counts_external_usage():
    tk, store, cfg = make_tk("""
    time_limits:
      shared_budget: true
      users:
        - name: kevin
          daily_limit_minutes: 60
          weekly_limit_minutes: 300
    """)
    tk.external_usage = lambda user, day, week_start: (50 * 60, 200 * 60)
    now = datetime(2026, 9, 28, 12)
    store.add_usage("kevin", now.date(), active=5 * 60)
    st = tk.status("kevin", now)
    assert st.external_used_seconds == 50 * 60
    assert st.remaining_seconds == 5 * 60
    assert st.weekly_used_seconds == 205 * 60
    tk.cfg.shared_budget = False
    assert tk.status("kevin", now).remaining_seconds == 55 * 60


# -- focus mode ----------------------------------------------------------------------

FOCUS = """
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 60
      focus:
        allow_apps: [winword.exe]
        default_minutes: 45
        counts_toward_limit: false
        schedule: {mon: 16:00-17:00}
"""


def test_focus_manual_start_stop_and_budget_pause():
    tk, store, cfg = make_tk(FOCUS)
    now = datetime(2026, 9, 29, 12, 0, 0)  # Tuesday, no schedule
    assert tk.status("kevin", now).focus_active is False
    until = tk.start_focus("kevin", now)
    assert until == now.timestamp() + 45 * 60
    st = tk.status("kevin", now)
    assert st.focus_active and st.focus_source == "manual" and st.focus_pauses_limit
    # out of budget but focus pauses the budget -> not out of time
    store.add_usage("kevin", now.date(), active=61 * 60)
    st = tk.status("kevin", now)
    assert not st.out_of_time and st.effective_remaining_seconds is None
    # time does not accrue during focus
    used_before = store.usage("kevin", now.date()).active
    tk.tick(now, {"kevin": [sess()]})
    tk.tick(now + timedelta(seconds=30), {"kevin": [sess()]})
    assert store.usage("kevin", now.date()).active == used_before
    tk.stop_focus("kevin", now)
    st = tk.status("kevin", now)
    assert not st.focus_active and st.out_of_time
    # expired focus cleans itself up
    tk.start_focus("kevin", now, minutes=1, allow=["docs.google.com"])
    assert tk.focus_extra_allow("kevin") == ["docs.google.com"]
    later = now + timedelta(minutes=2)
    assert not tk.status("kevin", later).focus_active
    assert tk.focus_extra_allow("kevin") == []


def test_focus_schedule_and_skip():
    tk, store, cfg = make_tk(FOCUS)
    monday = datetime(2026, 9, 28, 16, 30, 0)
    st = tk.status("kevin", monday)
    assert st.focus_active and st.focus_source == "schedule"
    assert abs(st.focus_until - (monday.timestamp() + 30 * 60)) < 1
    tk.stop_focus("kevin", monday)  # skips today's scheduled block
    assert not tk.status("kevin", monday).focus_active
    assert tk.status("kevin", monday + timedelta(days=7)).focus_active


# -- rewards ---------------------------------------------------------------------------

def test_rewards_and_earned_minutes():
    tk, store, cfg = make_tk("""
    time_limits:
      rewards:
        - {name: Homework done, minutes: 30, daily_max: 1}
      users:
        - name: kevin
          daily_limit_minutes: 60
          rewards:
            - {name: Chores, minutes: 15}
    """)
    policy = cfg.user_policy("kevin")
    assert [r.name for r in policy.rewards] == ["Homework done", "Chores"]
    day = date(2026, 9, 28)
    hw = policy.rewards[0]
    ok, why = tk.claim_reward("kevin", hw, day)
    assert ok and tk.bonus_seconds("kevin", day) == 30 * 60
    ok, why = tk.claim_reward("kevin", hw, day)
    assert not ok and "already" in why
    ok, _ = tk.claim_reward("kevin", policy.rewards[1], day)
    ok, _ = tk.claim_reward("kevin", policy.rewards[1], day)
    assert ok and tk.bonus_seconds("kevin", day) == 60 * 60
    tk.earn_minutes("kevin", day, 10, reason="test")
    assert tk.earned_seconds("kevin", day) == 70 * 60


def test_breaks_enabled_setting():
    tk, store, cfg = make_tk("""
    time_limits:
      breaks: {every_minutes: 30}
      users:
        - name: kevin
          daily_limit_minutes: 60
        - name: sarah
          daily_limit_minutes: 60
          breaks: false
    """)
    kevin, sarah = cfg.user_policy("kevin"), cfg.user_policy("sarah")
    assert kevin.breaks.enabled and kevin.breaks.every_minutes == 30
    assert not sarah.breaks.enabled
    assert tk.breaks_enabled("kevin", kevin)
    tk.set_breaks_enabled("kevin", False)
    assert not tk.breaks_enabled("kevin", kevin)


def test_hot_reload_swaps_policies_keeps_runtime():
    tk, store, cfg = make_tk(WEEKLY)
    now = datetime(2026, 9, 28, 12)
    tk.tick(now, {"kevin": [sess()]})
    tk.tick(now + timedelta(seconds=10), {"kevin": [sess()]})
    new_cfg = Config.from_dict(yaml.safe_load(WEEKLY.replace("daily_limit_minutes: 120", "daily_limit_minutes: 30")))
    tk.update_config(new_cfg.time_limits, new_cfg.build_default_policy)
    st = tk.status("kevin", now)
    assert st.limit_minutes == 30 and st.used_seconds >= 10
