from datetime import date, timedelta

from pclink.store import Store


def make_store():
    return Store(":memory:")


def test_usage_accumulates_and_flushes():
    s = make_store()
    day = date(2026, 9, 28)
    s.add_usage("kevin", day, active=60, gaming=30)
    s.add_usage("kevin", day, active=60, browsing=20)
    totals = s.usage("kevin", day)
    assert totals.active == 120 and totals.gaming == 30 and totals.browsing == 20
    s.flush()
    # reopen the same in-memory connection state via cache clear simulation
    assert s.usage("kevin", day).active == 120


def test_apps_and_sites_top():
    s = make_store()
    day = date(2026, 9, 28)
    s.add_app("kevin", day, "game:minecraft", 300)
    s.add_app("kevin", day, "app:chrome", 100)
    s.add_site("kevin", day, "youtube.com", 200)
    s.add_site("kevin", day, "wikipedia.org", 50)
    top_apps = s.top_apps("kevin", day, 10)
    assert top_apps[0][0] == "game:minecraft"
    top_sites = s.top_sites("kevin", day, 1)
    assert top_sites == [("youtube.com", 200)]


def test_settings_roundtrip():
    s = make_store()
    s.set_setting("kevin", "limit_enabled", True)
    s.set_setting("kevin", "bonus:2026-09-28", 900)
    assert s.get_setting("kevin", "limit_enabled") is True
    assert s.get_setting("kevin", "bonus:2026-09-28") == 900
    assert s.get_setting("kevin", "missing", "d") == "d"
    s.delete_setting("kevin", "limit_enabled")
    assert s.get_setting("kevin", "limit_enabled", None) is None


def test_history_length():
    s = make_store()
    today = date(2026, 9, 28)
    s.add_usage("kevin", today, active=600)
    s.add_usage("kevin", today - timedelta(days=1), active=1200)
    hist = s.history("kevin", days=7, today=today)
    assert len(hist) == 7
    assert hist[-1]["active_minutes"] == 10.0
    assert hist[-2]["active_minutes"] == 20.0


def test_purge_removes_old_days():
    s = make_store()
    today = date(2026, 9, 28)
    old = today - timedelta(days=200)
    s.add_usage("kevin", old, active=600)
    s.add_usage("kevin", today - timedelta(days=1), active=60)   # (the days count back from the newest usage)
    s.flush()
    s.purge(retention_days=90, today=today)
    assert s.usage("kevin", old).active == 0


def test_rule_usage_and_weekly_range():
    s = make_store()
    day = date(2026, 9, 28)
    s.add_rule("kevin", day, "youtube", 120)
    s.add_rule("kevin", day, "youtube", 60)
    s.add_rule("kevin", day, "roblox", 10)
    assert s.rule_seconds("kevin", day, "youtube") == 180
    assert s.rule_usage("kevin", day) == {"youtube": 180, "roblox": 10}
    s.flush()
    assert s.rule_seconds("kevin", day, "youtube") == 180
    s.add_usage("kevin", day, active=60)
    s.add_usage("kevin", day - timedelta(days=1), active=120)
    s.add_usage("kevin", day - timedelta(days=8), active=999)
    total = s.usage_range("kevin", day - timedelta(days=6), day)
    assert total.active == 180


def test_purge_removes_dated_settings_of_all_kinds():
    s = make_store()
    today = date(2026, 9, 28)
    old = (today - timedelta(days=200)).isoformat()
    for key in (f"bonus:{old}", f"limit:{old}", f"rollover:{old}", f"bedtime_ext:{old}", f"reward:homework:{old}", f"earned:{old}"):
        s.set_setting("kevin", key, 1)
    s.set_setting("kevin", f"bonus:{today.isoformat()}", 2)
    s.set_setting("kevin", "focus_until", 12345)
    s.add_rule("kevin", today - timedelta(days=200), "x", 5)
    s.flush()
    s.purge(retention_days=90, today=today)
    remaining = s.settings_for("kevin")
    assert set(remaining) == {f"bonus:{today.isoformat()}", "focus_until"}
    assert s.rule_seconds("kevin", today - timedelta(days=200), "x") == 0


def test_recent_events_filters():
    s = make_store()
    s.log_event("kevin", "bonus", "+15")
    s.log_event("sarah", "bonus", "+5")
    s.log_event("kevin", "logoff", "")
    assert len(s.recent_events(10)) == 3
    assert {e["user"] for e in s.recent_events(10, user="kevin")} == {"kevin"}
    assert [e["kind"] for e in s.recent_events(10, kind="logoff")] == ["logoff"]


def test_failed_setting_write_does_not_change_the_cache():
    import sqlite3

    import pytest

    s = make_store()
    s.set_setting("kevin", "blocked", False)

    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("disk full")

    real, s._db = s._db, Broken()
    with pytest.raises(sqlite3.Error):
        s.set_setting("kevin", "blocked", True)
    with pytest.raises(sqlite3.Error):
        s.delete_setting("kevin", "blocked")
    s._db = real
    assert s.get_setting("kevin", "blocked") is False
