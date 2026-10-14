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


# -- review fixes ------------------------------------------------------------------------

GRACE = """
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 1
      weekly_limit_minutes: 600
      grace_seconds: 60
      warn_at_minutes: []
"""


def _run(tk, start, seconds, hook=None, step=5):
    out = []
    for i in range(0, seconds + 1, step):
        t = start + timedelta(seconds=i)
        if hook:
            hook(i, t)
        out += [(i, a.kind) for a in tk.tick(t, {"kevin": [sess()]})]
    return out


def test_resending_or_lowering_a_value_does_not_restart_the_grace_period():
    start = datetime(2026, 9, 28, 15, 0, 0)

    def spam(i, t):
        if i and i % 50 == 0:
            tk.set_bonus_minutes("kevin", t.date(), tk.bonus_seconds("kevin", t.date()) / 60)   # same value
            tk.set_limit_today("kevin", t.date(), 1 if i < 100 else 0)   # same limit, later a lower one
            if i >= 100:                                     # already out of time: take more away
                tk.add_bonus_minutes("kevin", t.date(), -5)
            tk.set_weekday_limit("kevin", 1)
            tk.set_weekly_limit("kevin", 500)                # lower weekly limit
            tk.set_bedtime_extension("kevin", t.date(), 0)
            tk.set_manually_blocked("kevin", False)          # was not blocked
            tk.set_parent_present(i % 100 == 0)              # no bedtime extension to gain

    tk, store, cfg = make_tk(GRACE)
    plain = [a for a in _run(tk, start, 300) if a[1] in ("warn", "logoff")]
    tk, store, cfg = make_tk(GRACE)
    spammed = [a for a in _run(tk, start, 300, hook=spam) if a[1] in ("warn", "logoff")]
    assert plain[:2] == [(60, "warn"), (120, "logoff")]
    assert spammed == plain


def test_a_real_grant_still_restarts_grace():
    start = datetime(2026, 9, 28, 15, 0, 0)
    tk, store, cfg = make_tk(GRACE)
    _run(tk, start, 90)
    assert tk.status("kevin", start + timedelta(seconds=90)).grace_active
    tk.add_bonus_minutes("kevin", start.date(), 10)
    assert not tk.status("kevin", start + timedelta(seconds=90)).grace_active
    # ... and so does a raised limit once the bonus is gone again
    tk.set_bonus_minutes("kevin", start.date(), 0)
    acts = tk.tick(start + timedelta(seconds=95), {"kevin": [sess()]})
    assert "warn" in [a.kind for a in acts]
    assert tk.status("kevin", start + timedelta(seconds=95)).grace_active
    tk.set_limit_today("kevin", start.date(), 30)
    assert not tk.status("kevin", start + timedelta(seconds=95)).grace_active


def test_is_loosening_limit_compares_within_the_scope():
    tk, store, cfg = make_tk("""
    time_limits:
      users:
        - name: kevin
          daily_limit_minutes: {weekdays: 60, weekend: 180}
          weekly_limit_minutes: 600
        - name: free
          daily_limit_minutes: 60
    """)
    saturday, monday = date(2026, 10, 3), date(2026, 9, 28)
    # today
    assert not tk.is_loosening_limit("kevin", "today", 60, monday)
    assert not tk.is_loosening_limit("kevin", "today", 0, monday)
    assert tk.is_loosening_limit("kevin", "today", 61, monday)
    assert tk.is_loosening_limit("kevin", "today", None, monday)
    # weekday / weekend are judged against their own days, not against today's limit
    assert tk.is_loosening_limit("kevin", "weekday", 180, saturday)
    assert not tk.is_loosening_limit("kevin", "weekday", 60, saturday)
    assert not tk.is_loosening_limit("kevin", "weekend", 180, monday)
    assert tk.is_loosening_limit("kevin", "weekend", 181, monday)
    tk.set_weekday_limit("kevin", 30)
    assert tk.is_loosening_limit("kevin", "weekday", 45, monday)
    assert tk.is_loosening_limit("kevin", "weekday", None, monday)
    # weekly: 0 / negative / None mean "no weekly limit"
    assert not tk.is_loosening_limit("kevin", "weekly", 600, monday)
    assert not tk.is_loosening_limit("kevin", "weekly", 60, monday)
    assert tk.is_loosening_limit("kevin", "weekly", 601, monday)
    for unlimited in (0, -1, None):
        assert tk.is_loosening_limit("kevin", "weekly", unlimited, monday)
    assert not tk.is_loosening_limit("free", "weekly", 0, monday)       # has no weekly limit to lose
    assert not tk.is_loosening_limit("free", "weekly", 300, monday)
    assert tk.is_loosening_limit("kevin", "bogus", 1, monday)


def test_bedtime_day_and_extension_after_midnight():
    tk, store, cfg = make_tk("""
    time_limits:
      users:
        - name: kevin
          bedtime: "23:50"
          wake_time: "07:00"
        - name: owl
          bedtime: "01:00"
          wake_time: "08:00"
        - name: nobed
          daily_limit_minutes: 60
    """)
    d1, d2 = date(2026, 9, 28), date(2026, 9, 29)
    assert tk.bedtime_day("kevin", datetime(2026, 9, 28, 22, 0)) == d1
    assert tk.bedtime_day("kevin", datetime(2026, 9, 29, 0, 10)) == d1     # last night is still the one in play
    assert tk.bedtime_day("kevin", datetime(2026, 9, 29, 6, 59)) == d1
    assert tk.bedtime_day("kevin", datetime(2026, 9, 29, 7, 0)) == d2
    assert tk.bedtime_day("owl", datetime(2026, 9, 29, 0, 30)) == d1        # inside the after-midnight part
    assert tk.bedtime_day("owl", datetime(2026, 9, 29, 9, 0)) == d2
    assert tk.bedtime_day("nobed", datetime(2026, 9, 29, 0, 30)) == d2
    # 23:50 -> 00:20, then +30 granted at 00:10 really extends tonight
    tk.extend_bedtime("kevin", d1, 30)
    now = datetime(2026, 9, 29, 0, 10)
    assert tk.status("kevin", now).bedtime_remaining_seconds == 600
    tk.extend_bedtime("kevin", tk.bedtime_day("kevin", now), 30)
    assert tk.status("kevin", now).bedtime_remaining_seconds == 2400
    assert tk.status("kevin", datetime(2026, 9, 29, 0, 45)).in_bedtime_window
    assert tk.bedtime_extension_minutes("kevin", d2) == 0                   # tomorrow night is untouched


def test_blocked_user_is_unblocked_once_when_no_longer_managed():
    for change in ("unlimited: true", None):
        tk, store, cfg = make_tk(GRACE)
        now = datetime(2026, 9, 28, 15, 0, 0)
        store.add_usage("kevin", now.date(), active=600)
        assert [a.kind for a in tk.tick(now, {})] == ["block"]
        users = [{"name": "kevin", "unlimited": True}] if change else [{"name": "other", "daily_limit_minutes": 5}]
        new_cfg = Config.from_dict({"time_limits": {"users": users}})
        tk.update_config(new_cfg.time_limits, new_cfg.build_default_policy)
        acts = tk.tick(now + timedelta(seconds=5), {})
        assert [(a.kind, a.user) for a in acts if a.user == "kevin"] == [("unblock", "kevin")]
        assert [a for a in tk.tick(now + timedelta(seconds=10), {}) if a.user == "kevin"] == []


def test_focus_only_pauses_the_budget_while_an_agent_reports():
    from pclink.models import AgentReport
    tk, store, cfg = make_tk(FOCUS)
    now = datetime(2026, 9, 29, 12, 0, 0)       # a Tuesday: no scheduled focus
    store.add_usage("kevin", now.date(), active=3600)
    tk.start_focus("kevin", now, 120)

    def with_agent():
        s = sess()
        s.report = AgentReport(session_id=2, ts=0.0)
        return s

    for i in range(0, 300, 5):
        tk.tick(now + timedelta(seconds=i), {"kevin": [with_agent()]})
    assert store.usage("kevin", now.date()).active == 3600
    assert not tk.status("kevin", now + timedelta(seconds=300)).out_of_time
    # the agent is killed: after a short allowance the time counts and the limit applies again
    kinds = []
    for i in range(300, 600, 5):
        kinds += [a.kind for a in tk.tick(now + timedelta(seconds=i), {"kevin": [sess()]})]
    assert store.usage("kevin", now.date()).active > 3600 + 200
    st = tk.status("kevin", now + timedelta(seconds=600))
    assert st.focus_active and not st.focus_pauses_limit and st.out_of_time
    assert "logoff" in kinds
    # the agent is back: focus pauses the budget again - but only after ten minutes with reports
    # throughout (an agent that is suspended over and over must not flip it back every time)
    tk.tick(now + timedelta(seconds=605), {"kevin": [with_agent()]})
    assert not tk.status("kevin", now + timedelta(seconds=605)).focus_pauses_limit
    for i in range(610, 1220, 5):
        tk.tick(now + timedelta(seconds=i), {"kevin": [with_agent()]})
    assert tk.status("kevin", now + timedelta(seconds=1220)).focus_pauses_limit


ROLLOVER_SHARED = """
time_limits:
  shared_budget: true
  users:
    - name: kevin
      daily_limit_minutes: 60
      rollover: 60
"""


def test_rollover_counts_yesterdays_usage_on_other_pcs():
    tk, store, cfg = make_tk(ROLLOVER_SHARED)
    now = datetime(2026, 9, 29, 12)
    yesterday = now.date() - timedelta(days=1)
    tk.external_usage = lambda user, day, week_start: (0.0, 45 * 60.0 if week_start <= yesterday <= day else 0.0)
    assert tk.status("kevin", now).rollover_seconds == 15 * 60


def test_no_rollover_while_other_pcs_are_unknown():
    tk, store, cfg = make_tk(ROLLOVER_SHARED)
    now = datetime(2026, 9, 29, 12)
    known = {"ready": False}
    tk.external_usage = lambda user, day, week_start: (0.0, 0.0)
    tk.external_ready = lambda: known["ready"]
    assert tk.status("kevin", now).rollover_seconds == 0
    known["ready"] = True                      # not stored while unknown: worked out once the peers are known
    assert tk.status("kevin", now).rollover_seconds == 60 * 60

    def boom(user, day, week_start):
        raise RuntimeError("x")

    tk2, store2, cfg2 = make_tk(ROLLOVER_SHARED)
    tk2.external_usage = boom
    assert tk2.status("kevin", now).rollover_seconds == 0


def test_external_usage_failure_keeps_the_last_known_values():
    tk, store, cfg = make_tk(ROLLOVER_SHARED)
    now = datetime(2026, 9, 30, 12)
    state = {"fail": False}

    def provider(user, day, week_start):
        if state["fail"]:
            raise ValueError("bad payload")
        return 50 * 60, 200 * 60

    tk.external_usage = provider
    assert tk.status("kevin", now).external_used_seconds == 50 * 60
    state["fail"] = True
    st = tk.status("kevin", now)
    assert st.external_used_seconds == 50 * 60 and st.weekly_used_seconds == 200 * 60
    tk.external_usage = lambda user, day, week_start: (float("nan"), -5)
    assert tk.status("kevin", now).external_used_seconds == 50 * 60


def test_long_tick_gap_and_backwards_clock_are_not_screen_time():
    tk, store, cfg = make_tk(WEEKLY)
    now = datetime(2026, 9, 28, 9, 0, 0)
    tk.tick(now, {"kevin": [sess()], "guest": [sess()]})
    tk.tick(now + timedelta(seconds=5), {"kevin": [sess()], "guest": [sess()]})
    tk.tick(now + timedelta(seconds=10), {"kevin": [sess()]})            # guest logged off
    later = now + timedelta(hours=3)                                     # ... and the PC slept
    tk.tick(later, {"kevin": [sess()], "guest": [sess()]})
    tick = cfg.time_limits.tick_seconds
    assert store.usage("kevin", now.date()).active == 10 + tick
    assert store.usage("guest", now.date()).active == 5 + tick
    tk.tick(later - timedelta(minutes=30), {"kevin": [sess()]})          # clock set back
    tk.tick(later - timedelta(minutes=30) + timedelta(seconds=5), {"kevin": [sess()]})
    assert store.usage("kevin", now.date()).active == 15 + tick


def test_read_only_policy_lookup_does_not_create_policies():
    tk, store, cfg = make_tk("""
    time_limits:
      manage_unknown_users: true
      default_policy: {daily_limit_minutes: 30}
      users:
        - name: kevin
          daily_limit_minutes: 60
    """)
    assert tk.policy_for("stranger", create=False) is None
    assert tk.all_policy_users() == ["kevin"]
    assert tk.policy_for("kevin", create=False) is not None
    assert tk.policy_for("guest").daily_limits[0] == 30
    assert tk.policy_for("guest", create=False) is not None
    from pclink.timekeeper import MAX_AUTO_POLICIES
    for i in range(MAX_AUTO_POLICIES + 50):
        assert tk.policy_for(f"u{i}") is not None
    assert len(tk.all_policy_users()) == 1 + MAX_AUTO_POLICIES
