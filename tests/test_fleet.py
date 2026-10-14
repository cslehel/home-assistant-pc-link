from datetime import date

from pclink.config import FleetConfig
from pclink.fleet import FleetTracker


def make(device="bbb-pc", clock=None):
    holder = {"t": 1000.0}
    tracker = FleetTracker(FleetConfig(peer_timeout_seconds=180), device, clock=lambda: holder["t"])
    return tracker, holder


def user_payload(user="kevin", used=30, status="active", day="2026-09-28", history=None, **extra):
    p = {"user": user, "name": user.title(), "date": day, "used_minutes": used, "gaming_minutes": 5, "browsing_minutes": 10,
         "status": status, "presence": "active" if status == "active" else "none", "logged_in": status != "logged_off",
         "managed": True, "remaining_minutes": 60 - used, "history": history or [{"date": day, "active_minutes": used}]}
    p.update(extra)
    return p


def feed(t, dev, *payloads, name=None):
    info = {"users": {p["user"]: p for p in payloads}}
    if name is not None:
        info["name"] = name
    return t.update_peers({dev: info})


def test_ingests_peers_and_ignores_own_and_bogus_ids():
    t, clock = make()
    assert not t.ready()
    assert t.update_peers({"aaa-pc": {"users": {"kevin": user_payload()}},
                           "bbb-pc": {"users": {"kevin": user_payload()}},     # ourselves
                           "x" * 200: {"users": {}},                           # id too long
                           "ccc-pc": "not a dict"}) == 1
    assert t.ready()
    peers = t.peers()
    assert set(peers) == {"aaa-pc"} and "kevin" in peers["aaa-pc"].users
    # what update_peers does not mention is kept (merged, not replaced)
    t.update_peers({"ddd-pc": {"users": {}}})
    assert "kevin" in t.peers()["aaa-pc"].users


def test_external_usage_today_and_week():
    t, clock = make()
    day = date(2026, 9, 30)
    week_start = date(2026, 9, 28)
    hist = [{"date": "2026-09-27", "active_minutes": 100}, {"date": "2026-09-28", "active_minutes": 20},
            {"date": "2026-09-29", "active_minutes": 30}, {"date": "2026-09-30", "active_minutes": 40}]
    feed(t, "aaa-pc", user_payload(used=40, day="2026-09-30", history=hist))
    feed(t, "ccc-pc", user_payload(used=15, day="2026-09-30", history=[{"date": "2026-09-30", "active_minutes": 15}]))
    today_s, week_s = t.external_usage("kevin", day, week_start)
    assert today_s == 55 * 60
    assert week_s == (20 + 30 + 40 + 15) * 60  # the 27th is last week
    # stale payload from yesterday: not today's usage, but its history still counts for the week
    feed(t, "ddd-pc", user_payload(used=99, day="2026-09-29", history=[{"date": "2026-09-29", "active_minutes": 99}]))
    today_s, week_s = t.external_usage("kevin", day, week_start)
    assert today_s == 55 * 60 and week_s == (20 + 30 + 40 + 15 + 99) * 60
    assert t.external_usage("nobody", day, week_start) == (0.0, 0.0)


def test_bad_peer_payloads_neither_raise_nor_cancel_real_usage():
    t, clock = make()
    day, week_start = date(2026, 9, 28), date(2026, 9, 28)
    feed(t, "aaa-pc", user_payload(used=60))
    assert t.external_usage("kevin", day, week_start) == (3600.0, 3600.0)
    bad = [
        {"user": "kevin", "date": "2026-09-28", "used_minutes": "x"},
        {"user": "kevin", "date": "2026-09-28", "used_minutes": -100000, "history": [{"date": "2026-09-28", "active_minutes": -5}]},
        {"user": "kevin", "date": "2026-09-28", "used_minutes": float("nan"), "history": 5},
        {"user": "kevin", "date": "2026-09-28", "used_minutes": True, "history": [1, None, {"date": 7}],
         "remaining_minutes": "soon", "managed": True, "status": ["x"], "name": 3},
        {"user": "kevin", "date": "2026-09-28", "used_minutes": float("inf")},
    ]
    for i, payload in enumerate(bad):
        assert feed(t, f"zz{i}", payload) == 1
    assert t.external_usage("kevin", day, week_start) == (3600.0, 3600.0)
    # non-dict containers are ignored
    assert t.update_peers({"yy": {"users": ["kevin"]}, "yz": {"users": {"kevin": "x"}}}) == 2
    assert t.update_peers(["not", "a", "dict"]) == 0
    assert t.external_usage("kevin", day, week_start) == (3600.0, 3600.0)
    # one PC cannot report more than a day per day, and neither can all of them together
    feed(t, "big", {"user": "kevin", "date": "2026-09-28", "used_minutes": 1e9})
    assert t.external_usage("kevin", day, week_start)[0] == 86400.0
    assert t.peers()["big"].usage["kevin"] == {"2026-09-28": 1440.0}


def test_lower_figures_never_give_time_back():
    t, clock = make()
    day = date(2026, 9, 28)
    feed(t, "aaa-pc", user_payload(used=50))
    feed(t, "aaa-pc", user_payload(used=10))
    feed(t, "aaa-pc", {"user": "kevin", "date": "2026-09-28"})   # no figures at all
    assert t.external_usage("kevin", day, day) == (3000.0, 3000.0)


def test_made_up_dates_are_ignored():
    t, clock = make()
    clock["t"] = 1_790_000_000.0          # 2026-09
    today = date.fromtimestamp(clock["t"])
    hist = [{"date": f"{2100 + i}-01-01", "active_minutes": 60} for i in range(40)]
    hist.append({"date": today.isoformat(), "active_minutes": 20})
    feed(t, "aaa-pc", user_payload(used=20, day=today.isoformat(), history=hist))
    marks = t.peers()["aaa-pc"].usage["kevin"]
    assert marks == {today.isoformat(): 20.0}
    assert t.external_usage("kevin", today, today) == (1200.0, 1200.0)


def test_number_of_peers_and_users_is_capped():
    from pclink.fleet import MAX_PEERS, MAX_USERS_PER_PEER
    t, clock = make()
    for i in range(MAX_PEERS + 20):
        t.update_peers({f"pc{i:03d}": {"users": {}}})
    assert len(t.peers()) == MAX_PEERS
    users = {f"u{i}": user_payload(user=f"u{i}") for i in range(MAX_USERS_PER_PEER + 20)}
    t.update_peers({"pc001": {"users": users}})
    assert len(t.peers()["pc001"].users) == MAX_USERS_PER_PEER
    # peers that fell silent and hold no usage make room again
    feed(t, "pc000", user_payload(day=date.fromtimestamp(clock["t"]).isoformat()))
    clock["t"] += 1000
    assert t.update_peers({"new-pc": {"users": {}}}) == 1
    # pc000 and pc001 still hold usage that counts this week
    assert set(t.peers()) == {"pc000", "pc001", "new-pc"}


def test_former_self_is_not_counted():
    holder = {"t": 1000.0}
    t = FleetTracker(FleetConfig(peer_timeout_seconds=180), "new-id", clock=lambda: holder["t"], own_name="Kids-PC")
    day = date(2026, 9, 28)
    feed(t, "old-id", user_payload(used=40, bonus_local_minutes=15), name="kids-pc")
    feed(t, "other-pc", user_payload(used=10, bonus_local_minutes=5), name="Study-PC")
    assert t.external_usage("kevin", day, day) == (600.0, 600.0)
    assert t.external_extras("kevin", day)["bonus_seconds"] == 5 * 60


def test_tracker_is_safe_to_feed_from_another_thread():
    import threading
    t, clock = make()
    day = date(2026, 9, 28)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            feed(t, f"n{i % 60}", user_payload(used=i % 50))
            clock["t"] += 1

    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(300):
            t.external_usage("kevin", day, day)
            t.external_extras("kevin", day)
            t.peers()
    finally:
        stop.set()
        th.join()
