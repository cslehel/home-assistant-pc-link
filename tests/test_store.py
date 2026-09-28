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
    s.flush()
    s.purge(retention_days=90, today=today)
    assert s.usage("kevin", old).active == 0
