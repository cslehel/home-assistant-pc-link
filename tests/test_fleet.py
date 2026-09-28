import json
from datetime import date

from pclink.config import FleetConfig
from pclink.fleet import FleetTracker


def make(device="bbb-pc", clock=None):
    holder = {"t": 1000.0}
    tracker = FleetTracker(FleetConfig(peer_timeout_seconds=180), "pclink", device, clock=lambda: holder["t"])
    return tracker, holder


def user_payload(user="kevin", used=30, status="active", day="2026-09-28", history=None, **extra):
    p = {"user": user, "name": user.title(), "date": day, "used_minutes": used, "gaming_minutes": 5, "browsing_minutes": 10,
         "status": status, "presence": "active" if status == "active" else "none", "logged_in": status != "logged_off",
         "managed": True, "remaining_minutes": 60 - used, "history": history or [{"date": day, "active_minutes": used}]}
    p.update(extra)
    return json.dumps(p).encode()


def test_ingests_peers_and_ignores_own_and_fleet_topics():
    t, clock = make()
    assert t.on_message("pclink/aaa-pc/availability", b"online")
    assert not t.on_message("pclink/bbb-pc/availability", b"online")       # ourselves
    assert not t.on_message("pclink/fleet/state", b"{}")                    # the roll-up itself
    assert not t.on_message("other/aaa-pc/availability", b"online")
    assert t.on_message("pclink/aaa-pc/user/kevin/state", user_payload())
    peers = t.peers()
    assert peers["aaa-pc"].online and "kevin" in peers["aaa-pc"].users
    assert t.changed
    t.changed = False
    # same summary again -> no change flag
    t.on_message("pclink/aaa-pc/user/kevin/state", user_payload())
    assert not t.changed
    t.on_message("pclink/aaa-pc/user/kevin/state", user_payload(used=31))
    assert t.changed
    # a cleared retained topic removes the user
    t.on_message("pclink/aaa-pc/user/kevin/state", b"")
    assert "kevin" not in t.peers()["aaa-pc"].users


def test_leader_is_smallest_online_device_id():
    t, clock = make("bbb-pc")
    assert t.is_leader()  # alone
    t.on_message("pclink/ccc-pc/availability", b"online")
    assert t.is_leader()
    t.on_message("pclink/aaa-pc/availability", b"online")
    assert not t.is_leader()
    t.on_message("pclink/aaa-pc/availability", b"offline")
    assert t.is_leader()
    # a peer that went silent for longer than the timeout no longer counts
    t.on_message("pclink/aaa-pc/availability", b"online")
    clock["t"] += 500
    assert t.online_peers() == [] and t.is_leader()


def test_external_usage_today_and_week():
    t, clock = make()
    day = date(2026, 9, 30)
    week_start = date(2026, 9, 28)
    hist = [{"date": "2026-09-27", "active_minutes": 100}, {"date": "2026-09-28", "active_minutes": 20},
            {"date": "2026-09-29", "active_minutes": 30}, {"date": "2026-09-30", "active_minutes": 40}]
    t.on_message("pclink/aaa-pc/user/kevin/state", user_payload(used=40, day="2026-09-30", history=hist))
    t.on_message("pclink/ccc-pc/user/kevin/state", user_payload(used=15, day="2026-09-30", history=[{"date": "2026-09-30", "active_minutes": 15}]))
    today_s, week_s = t.external_usage("kevin", day, week_start)
    assert today_s == 55 * 60
    assert week_s == (20 + 30 + 40 + 15) * 60  # the 27th is last week
    # stale payload from yesterday: not today's usage, but its history still counts for the week
    t.on_message("pclink/ddd-pc/user/kevin/state", user_payload(used=99, day="2026-09-29", history=[{"date": "2026-09-29", "active_minutes": 99}]))
    today_s, week_s = t.external_usage("kevin", day, week_start)
    assert today_s == 55 * 60 and week_s == (20 + 30 + 40 + 15 + 99) * 60
    assert t.external_usage("nobody", day, week_start) == (0.0, 0.0)


def test_rollup_combines_own_and_peers():
    t, clock = make("bbb-pc")
    t.on_message("pclink/aaa-pc/availability", b"online")
    t.on_message("pclink/aaa-pc/user/kevin/state", user_payload(used=40))
    t.on_message("pclink/aaa-pc/user/sarah/state", user_payload(user="sarah", used=10, status="logged_off"))
    t.on_message("pclink/ccc-pc/availability", b"offline")
    own = {"kevin": json.loads(user_payload(used=15, status="idle"))}
    r = t.rollup(own)
    assert r["pcs_online"] == 2 and r["pcs_online_list"] == ["aaa-pc", "bbb-pc"]
    assert r["pcs_in_use"] == 1 and r["any_pc_in_use"]
    kevin = r["users"]["kevin"]
    assert kevin["total_minutes_today"] == 55 and kevin["per_pc"] == {"bbb-pc": 15, "aaa-pc": 40}
    assert kevin["active_on"] == ["aaa-pc"] and kevin["status"] == "active" and kevin["remaining_minutes"] == 20
    assert r["users"]["sarah"]["status"] == "logged_off" and r["users"]["sarah"]["logged_in_on"] == []
    assert r["active_users"] == ["kevin"]
