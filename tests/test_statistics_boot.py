""""Time used" of users without a limit is time in use, not time logged in; the boot time
does not wander with the clock."""
from __future__ import annotations

import pytest

from pclink import telemetry as telemetry_module
from pclink.config import Config
from pclink.telemetry import Telemetry

from .test_fix_core import NOW, build, session, tick


def _used(orch, user):
    return orch.store.usage(user, NOW.date()).active


@pytest.mark.parametrize("user", ["lehel", "dad"])   # not listed at all / listed with unlimited: true
def test_idle_and_away_time_of_a_user_without_a_limit_is_not_time_used(user):
    orch, sessions, _api, _agents = build(agent=False)
    sessions.sessions = [session(user=user, idle=0.0)]
    tick(orch)
    for _ in range(12):
        tick(orch, 5)
    in_use = _used(orch, user)
    assert in_use == pytest.approx(60, abs=6)
    sessions.sessions = [session(user=user, idle=4 * 3600.0)]   # the PC left on all night
    for _ in range(12):
        tick(orch, 5)
    assert _used(orch, user) == pytest.approx(in_use, abs=6)
    sessions.sessions = [session(user=user, locked=True, idle=0.0)]
    for _ in range(12):
        tick(orch, 5)
    assert _used(orch, user) == pytest.approx(in_use, abs=6)


def test_a_user_without_a_limit_can_still_count_idle_time():
    cfg = Config.from_dict({"time_limits": {"users": [{"name": "dad", "unlimited": True, "count_idle": True},
                                                      {"name": "mum", "unlimited": True},
                                                      {"name": "kevin", "daily_limit_minutes": 60}]}})
    assert cfg.user_policy("dad").count_idle is True
    assert cfg.user_policy("mum").count_idle is False
    assert cfg.user_policy("kevin").count_idle is True   # a limit keeps the strict default


def test_the_boot_time_keeps_its_first_value_until_a_real_jump(monkeypatch):
    readings = iter([1000.0, 1004.0, 997.0, 1100.0, 5000.0, 5002.0])
    monkeypatch.setattr(telemetry_module.psutil, "boot_time", lambda: next(readings))
    tel = Telemetry.__new__(Telemetry)
    tel._boot = None
    assert [tel.boot_time() for _ in range(6)] == [1000.0, 1000.0, 1000.0, 1000.0, 5000.0, 5000.0]
    assert telemetry_module.BOOT_JITTER_SECONDS == 120.0
