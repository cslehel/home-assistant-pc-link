from datetime import datetime

from pclink.config import Config
from pclink.models import SessionInfo
from pclink.store import Store
from pclink.timekeeper import TimeKeeper


def make_tk(users_yaml, block_method="logon_hours"):
    import yaml
    cfg = Config.from_dict(yaml.safe_load(users_yaml))
    store = Store(":memory:")
    tk = TimeKeeper(cfg.time_limits, store, cfg.build_default_policy)
    return tk, store


BASIC = """
time_limits:
  block_method: logon_hours
  users:
    - name: kevin
      daily_limit_minutes: 60
      warn_at_minutes: [15, 5, 1]
      action: logoff
      grace_seconds: 60
      count_idle: false
    - name: dad
      unlimited: true
"""


def active_session(user="kevin", sid=2, idle=0.0, locked=False, state="active"):
    return SessionInfo(session_id=sid, user=user, state=state, locked=locked, idle_seconds=idle)


def test_unlimited_user_never_enforced():
    tk, store = make_tk(BASIC)
    now = datetime(2026, 9, 28, 12, 0, 0)
    st = tk.status("dad", now)
    assert not st.managed and st.remaining_seconds is None


def test_time_accrues_only_for_active_sessions():
    tk, store = make_tk(BASIC)
    base = datetime(2026, 9, 28, 12, 0, 0)
    by_user = {"kevin": [active_session()]}
    tk.tick(base, by_user)
    # advance 100 seconds
    tk.tick(base.replace(minute=1, second=40), by_user)
    used = store.usage("kevin", base.date()).active
    assert 95 <= used <= 105


def test_idle_session_does_not_count_when_configured():
    tk, store = make_tk(BASIC)
    base = datetime(2026, 9, 28, 12, 0, 0)
    # idle beyond threshold (600s default), count_idle false
    by_user = {"kevin": [active_session(idle=700)]}
    tk.tick(base, by_user)
    tk.tick(base.replace(minute=1), by_user)
    assert store.usage("kevin", base.date()).active == 0


def test_warning_then_grace_then_logoff():
    tk, store = make_tk(BASIC)
    base = datetime(2026, 9, 28, 12, 0, 0)
    # pre-load 59 minutes used so ~60s remain
    store.add_usage("kevin", base.date(), active=59 * 60)
    by_user = {"kevin": [active_session()]}
    actions = tk.tick(base, by_user)
    kinds = [a.kind for a in actions]
    assert "warn" in kinds  # under the 1-minute threshold
    # burn the remaining minute
    store.add_usage("kevin", base.date(), active=90)
    t1 = base.replace(minute=1, second=40)
    actions = tk.tick(t1, by_user)
    assert any(a.kind == "warn" and a.level == "critical" for a in actions)  # grace start
    # after grace window
    t2 = t1.replace(minute=3)
    actions = tk.tick(t2, by_user)
    assert any(a.kind == "logoff" for a in actions)


def test_block_desired_when_out_of_time():
    tk, store = make_tk(BASIC)
    base = datetime(2026, 9, 28, 12, 0, 0)
    store.add_usage("kevin", base.date(), active=61 * 60)
    actions = tk.tick(base, {"kevin": [active_session()]})
    assert any(a.kind == "block" for a in actions)
    # grant bonus -> should unblock next tick
    tk.add_bonus_minutes("kevin", base.date(), 30)
    actions = tk.tick(base.replace(second=5), {"kevin": [active_session()]})
    assert any(a.kind == "unblock" for a in actions)


def test_allowed_hours_block_outside_window():
    tk, store = make_tk("""
    time_limits:
      users:
        - name: kevin
          allowed_hours: 07:00-20:00
          action: logoff
    """)
    night = datetime(2026, 9, 28, 22, 0, 0)
    actions = tk.tick(night, {"kevin": [active_session()]})
    assert any(a.kind == "block" for a in actions)
    st = tk.status("kevin", night)
    assert st.out_of_time and not st.in_window


def test_manual_block_and_global_disable():
    tk, store = make_tk(BASIC)
    now = datetime(2026, 9, 28, 12, 0, 0)
    tk.set_manually_blocked("kevin", True)
    st = tk.status("kevin", now)
    assert st.manually_blocked and st.block_desired
    tk.set_global_enabled(False)
    st = tk.status("kevin", now)
    assert not st.enforcing and not st.block_desired


def test_limit_override_today():
    tk, store = make_tk(BASIC)
    now = datetime(2026, 9, 28, 12, 0, 0)
    tk.set_limit_today("kevin", now.date(), 200)
    st = tk.status("kevin", now)
    assert st.limit_minutes == 200
