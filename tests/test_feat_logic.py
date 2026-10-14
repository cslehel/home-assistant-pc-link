"""Phase-2 features: time requests, profiles, shared bonus / bedtime extension, weekly
summary figures, the PC's time zone."""
import json
import time
from datetime import date, datetime, timedelta

import pytest
import yaml

from pclink import util
from pclink.config import Config, ConfigError, FleetConfig, TimeRequestsConfig
from pclink.fleet import FleetTracker
from pclink.store import Store
from pclink.timekeeper import TimeKeeper
from pclink.util import local_iana_zone, windows_cover, windows_zone_to_iana


def cfg_of(text):
    return Config.from_dict(yaml.safe_load(text))


def make_tk(text):
    cfg = cfg_of(text)
    store = Store(":memory:")
    return TimeKeeper(cfg.time_limits, store, cfg.build_default_policy), store, cfg


# -- F1: time requests ---------------------------------------------------------------------

def test_time_requests_defaults_and_forms():
    req = cfg_of("{}").time_limits.time_requests
    assert req == TimeRequestsConfig() and req.enabled and req.options == [15, 30, 60]
    assert (req.cooldown_minutes, req.expire_minutes, req.max_per_day) == (10, 30, 5)
    assert cfg_of("time_limits: {time_requests: false}").time_limits.time_requests.enabled is False
    req = cfg_of("time_limits: {time_requests: {options: [60, '10', 10, 240], cooldown_minutes: 0, "
                 "expire_minutes: 5, max_per_day: 1}}").time_limits.time_requests
    assert req.enabled and req.options == [10, 60, 240] and req.cooldown_minutes == 0 and req.max_per_day == 1
    assert cfg_of("time_limits: {time_requests: {options: 20}}").time_limits.time_requests.options == [20]
    assert cfg_of("time_limits: {time_requests: {options: []}}").time_limits.time_requests.options == []


@pytest.mark.parametrize("section", [
    "{options: [0]}", "{options: [241]}", "{options: [15.5]}", "{options: [soon]}", "{options: [true]}",
    "{options: [1, 2, 3, 4, 5, 6, 7]}", "{options: {a: 1}}", "{cooldown_minutes: -1}", "{expire_minutes: 0}",
    "{max_per_day: 0}", "{enabled: maybe}", "[15, 30]",
])
def test_time_requests_validation(section):
    with pytest.raises(ConfigError):
        cfg_of(f"time_limits: {{time_requests: {section}}}")


# -- F2: profiles ----------------------------------------------------------------------------

PROFILES = """
time_limits:
  parent_pin: "1234"
  manage_unknown_users: true
  default_policy: {daily_limit_minutes: 45}
  profiles:
    holiday:
      daily_limit: 180
      bedtime: "22:00"
    exam:
      daily_limit_minutes: {weekdays: 30, weekend: 60}
      allowed_hours: 16:00-19:00
  users:
    - name: kid
      daily_limit_minutes: {weekdays: 90, weekend: 120}
      weekly_limit_minutes: 600
      allowed_hours: 07:00-20:00
      bedtime: "20:30"
      wake_time: "07:00"
      profiles:
        holiday: {daily_limit_minutes: 240, weekly_limit: unlimited}
        sick: {rollover: 30}
    - name: mum
      unlimited: true
"""


def test_profiles_are_parsed_and_merged():
    cfg = cfg_of(PROFILES)
    kid = cfg.user_policy("kid")
    assert sorted(kid.profiles) == ["exam", "holiday", "sick"]
    holiday = kid.profiles["holiday"]                     # the user's own entry replaces the shared one entirely
    assert holiday.daily_limits[0] == 240 and holiday.weekly_limit_minutes is None
    assert holiday.bedtime == kid.bedtime and holiday.allowed_windows == kid.allowed_windows
    exam = kid.profiles["exam"]
    assert exam.daily_limits[0] == 30 and exam.daily_limits[5] == 60 and exam.allowed_windows[2] == [(960, 1140)]
    assert exam.weekly_limit_minutes == 600 and exam.bedtime == kid.bedtime      # not named: the user's own
    assert kid.profiles["sick"].rollover_max_minutes == 30 and kid.profiles["sick"].daily_limits == kid.daily_limits
    assert exam.name == "kid" and exam.profiles == {}
    assert cfg.user_policy("mum").profiles == {}
    assert sorted(cfg.build_default_policy("guest").profiles) == ["exam", "holiday"]
    assert cfg.build_default_policy("guest").profiles["holiday"].daily_limits[0] == 180
    assert cfg.time_limits.profiles["holiday"] == {"daily_limit_minutes": 180, "bedtime": "22:00"}


@pytest.mark.parametrize("profiles", [
    "{normal: {daily_limit_minutes: 60}}", "{'bad name': {daily_limit_minutes: 60}}", "{" + "x" * 33 + ": {bedtime: '21:00'}}",
    "{holiday: {}}", "{holiday: 60}", "{holiday: {action: lock}}", "{holiday: {bedtime: '25:00'}}",
    "{holiday: {daily_limit_minutes: lots}}", "[holiday]", "{Holiday: {bedtime: '21:00'}, holiday: {bedtime: '22:00'}}",
])
def test_profile_validation(profiles):
    with pytest.raises(ConfigError):
        cfg_of(f"time_limits: {{profiles: {profiles}, users: [{{name: kid, daily_limit_minutes: 60}}]}}")
    with pytest.raises(ConfigError):
        cfg_of(f"time_limits: {{users: [{{name: kid, daily_limit_minutes: 60, profiles: {profiles}}}]}}")


def test_profile_api_and_policy_for():
    tk, store, cfg = make_tk(PROFILES)
    now = datetime(2026, 10, 13, 12, 0)                    # a Tuesday
    assert tk.profile_names("kid") == ["exam", "holiday", "sick"] and tk.profile_names("KID") == tk.profile_names("kid")
    assert tk.profile_names("mum") == [] and tk.profile_names("guest") == ["exam", "holiday"]
    assert tk.active_profile("kid") is None and tk.policy_for("kid") is cfg.user_policy("kid")
    assert tk.status("kid", now).limit_minutes == 90
    tk.set_profile("kid", "Holiday")
    assert tk.active_profile("kid") == "holiday" and tk.policy_for("kid") is cfg.user_policy("kid").profiles["holiday"]
    st = tk.status("kid", now)
    assert st.limit_minutes == 240 and st.weekly_limit_minutes is None
    assert store.recent_events(1, user="kid", kind="profile")[0]["detail"] == "holiday"
    # overrides from Home Assistant still win over the profile
    tk.set_limit_today("kid", now.date(), 50)
    assert tk.status("kid", now).limit_minutes == 50
    tk.set_limit_today("kid", now.date(), None)
    tk.set_weekly_limit("kid", 100)
    assert tk.status("kid", now).weekly_limit_minutes == 100
    tk.set_weekly_limit("kid", None)
    # it survives a restart and a reload
    again = TimeKeeper(cfg.time_limits, store, cfg.build_default_policy)
    assert again.active_profile("kid") == "holiday" and again.status("kid", now).limit_minutes == 240
    again.update_config(cfg_of(PROFILES).time_limits)
    assert again.active_profile("kid") == "holiday"
    # a profile that is gone from the configuration counts as none
    again.update_config(cfg_of(PROFILES.replace("holiday: {daily_limit_minutes: 240, weekly_limit: unlimited}",
                                                "other: {bedtime: '21:00'}").replace(
        "    holiday:\n      daily_limit: 180\n      bedtime: \"22:00\"\n", "")).time_limits)
    assert again.profile_names("kid") == ["exam", "other", "sick"]
    assert again.active_profile("kid") is None and again.status("kid", now).limit_minutes == 90
    for clear in (None, "normal", " NORMAL ", ""):
        tk.set_profile("kid", "exam")
        tk.set_profile("kid", clear)
        assert tk.active_profile("kid") is None and store.get_setting("kid", "profile") is None
    with pytest.raises(ValueError):
        tk.set_profile("kid", "nonsense")
    with pytest.raises(ValueError):
        tk.set_profile("mum", "holiday")
    store.set_setting("kid", "profile", ["holiday"])           # junk in the store is no profile
    assert tk.active_profile("kid") is None and tk.policy_for("kid") is cfg.user_policy("kid")


def test_profile_of_an_unlisted_user():
    tk, store, cfg = make_tk(PROFILES)
    now = datetime(2026, 10, 13, 12, 0)
    assert tk.status("guest", now).limit_minutes == 45
    tk.set_profile("guest", "holiday")
    st = tk.status("guest", now)
    assert st.limit_minutes == 180 and st.has_bedtime and st.bedtime_minute == 22 * 60


def test_is_loosening_profile():
    tk, store, cfg = make_tk(PROFILES)
    now = datetime(2026, 10, 13, 12, 0)
    assert tk.is_loosening_profile("kid", "holiday", now)          # more minutes, no weekly limit
    assert not tk.is_loosening_profile("kid", "exam", now)         # fewer minutes, narrower hours
    assert tk.is_loosening_profile("kid", "sick", now)             # minutes can be carried over
    assert not tk.is_loosening_profile("kid", None, now) and not tk.is_loosening_profile("kid", "normal", now)
    assert tk.is_loosening_profile("kid", "nonsense", now)         # unknown: ask for the PIN
    assert not tk.is_loosening_profile("mum", "normal", now)
    tk.set_profile("kid", "exam")
    assert tk.is_loosening_profile("kid", "normal", now) and tk.is_loosening_profile("kid", "holiday", now)
    assert not tk.is_loosening_profile("kid", "exam", now)
    tk.set_profile("kid", "holiday")
    assert not tk.is_loosening_profile("kid", "normal", now) and not tk.is_loosening_profile("kid", "exam", now)
    # a later bedtime or wider hours alone are loosening too
    tk, store, cfg = make_tk("""
time_limits:
  users:
    - name: kid
      bedtime: "20:30"
      allowed_hours: 08:00-20:00
      profiles:
        late: {bedtime: "22:00"}
        early: {bedtime: "19:00"}
        nobed: {bedtime: none}
        wide: {allowed_hours: 07:00-21:00}
        narrow: {allowed_hours: 09:00-12:00}
        weekend_only: {daily_limit_minutes: {weekend: 600, weekdays: 10}}
""")
    verdict = {name: tk.is_loosening_profile("kid", name, now) for name in tk.profile_names("kid")}
    assert verdict == {"early": False, "late": True, "narrow": False, "nobed": True, "weekend_only": False, "wide": True}


def test_only_a_loosening_switch_restarts_the_grace_period():
    from pclink.models import SessionInfo
    tk, store, cfg = make_tk("""
time_limits:
  users:
    - name: kid
      daily_limit_minutes: 1
      grace_seconds: 60
      profiles: {same: {wake_time: "00:00"}, more: {daily_limit_minutes: 2}}
""")
    now = datetime(2026, 10, 13, 16, 0)
    store.add_usage("kid", now.date(), active=600)
    sessions = {"kid": [SessionInfo(session_id=2, user="kid", state="active", locked=False, idle_seconds=0.0)]}
    tk.tick(now, sessions)
    assert tk.status("kid", now).grace_active
    started = tk._runtime["kid"].grace_started
    tk.set_profile("kid", "same")                               # gives nothing: the countdown goes on
    assert tk._runtime["kid"].grace_started == started
    tk.set_profile("kid", "more")
    assert tk._runtime["kid"].grace_started is None


def test_windows_cover():
    assert windows_cover([], [(0, 60)]) and windows_cover([], [])
    assert windows_cover([(0, 1440)], []) and not windows_cover([(60, 120)], [])
    assert windows_cover([(0, 60), (60, 120)], [(30, 110)]) and not windows_cover([(0, 60)], [(30, 90)])


# -- F3: bonus and bedtime extension from the other PCs ---------------------------------------

def make_fleet():
    return FleetTracker(FleetConfig(), "pc-b", own_name="pc-b")


def state(day, **figures):
    return {"user": "kid", "date": day.isoformat(), "used_minutes": 10, **figures}


def send(fleet, dev, payload, name=None):
    info = {"users": {"kid": payload}}
    if name is not None:
        info["name"] = name
    return fleet.update_peers({dev: info})


def test_fleet_external_extras():
    fleet = make_fleet()
    day = date(2026, 10, 13)
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 0, "bedtime_extension_minutes": 0}
    send(fleet, "pc-a", state(day, bonus_local_minutes=30, bedtime_extension_local_minutes=15))
    send(fleet, "pc-c", state(day, bonus_local_minutes=12.5, bedtime_extension_local_minutes=45))
    assert fleet.external_extras("KID", day) == {"bonus_seconds": 2550, "bedtime_extension_minutes": 45}
    assert fleet.external_extras("kid", day + timedelta(days=1)) == {"bonus_seconds": 0, "bedtime_extension_minutes": 0}
    assert fleet.external_extras("other", day)["bonus_seconds"] == 0
    # the latest figure counts, also when it went down; a payload without the keys changes nothing
    send(fleet, "pc-a", state(day, bonus_local_minutes=5))
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 1050, "bedtime_extension_minutes": 45}
    send(fleet, "pc-c", state(day))
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 1050, "bedtime_extension_minutes": 45}
    # the next day's payload does not disturb last night's figures
    send(fleet, "pc-c", state(day + timedelta(days=1), bonus_local_minutes=0,
                                    bedtime_extension_local_minutes=0))
    assert fleet.external_extras("kid", day)["bedtime_extension_minutes"] == 45
    assert fleet.external_extras("kid", day + timedelta(days=1)) == {"bonus_seconds": 0, "bedtime_extension_minutes": 0}
    # sanitised
    for bad in ("x", float("nan"), True, None, [1], 10 ** 400):
        send(fleet, "pc-a", {"user": "kid", "date": day.isoformat(), "bonus_local_minutes": bad,
                             "bedtime_extension_local_minutes": bad})
        assert fleet.external_extras("kid", day) == {"bonus_seconds": 750, "bedtime_extension_minutes": 45}
    # minutes taken away on another PC count (a bedtime extension cannot be negative)
    send(fleet, "pc-a", {"user": "kid", "date": day.isoformat(), "bonus_local_minutes": -5,
                         "bedtime_extension_local_minutes": -5})
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 450, "bedtime_extension_minutes": 45}
    send(fleet, "pc-a", state(day, bonus_local_minutes=1e9, bedtime_extension_local_minutes=1e9))
    send(fleet, "pc-d", state(day, bonus_local_minutes=1e9))
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 86400, "bedtime_extension_minutes": 1440}
    send(fleet, "pc-a", {"user": "kid", "bonus_local_minutes": 7})   # no date
    assert fleet.external_extras("kid", day)["bonus_seconds"] == 86400
    # grants taken back on the other PCs; old days are dropped
    send(fleet, "pc-a", state(day, bonus_local_minutes=0, bedtime_extension_local_minutes=0))
    send(fleet, "pc-d", state(day, bonus_local_minutes=0))
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 750, "bedtime_extension_minutes": 45}
    for offset in range(2, 6):
        send(fleet, "pc-c", state(day + timedelta(days=offset), bonus_local_minutes=1))
    assert len(fleet.peers()["pc-c"].extras["kid"]) == 3 and fleet.external_extras("kid", day)["bonus_seconds"] == 0


def test_fleet_extras_from_home_assistant_and_not_from_this_pc_itself():
    fleet = FleetTracker(FleetConfig(), "new-id", own_name="Family-PC")
    day = date(2026, 10, 13)
    peers = {"den": {"name": "Den", "users": {"Kid": {"date": day.isoformat(), "used_minutes": 5,
                                                       "bonus_local_minutes": 20, "bedtime_extension_local_minutes": 30}}},
             "new-id": {"users": {"kid": {"date": day.isoformat(), "bonus_local_minutes": 999}}}}
    assert fleet.update_peers(peers) == 1
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 1200, "bedtime_extension_minutes": 30}
    fleet.update_peers({"den": {"users": {"kid": {"date": day.isoformat(), "used_minutes": 6}}}})   # keys absent: kept
    assert fleet.external_extras("kid", day)["bonus_seconds"] == 1200
    fleet.update_peers({"den": {"users": {"kid": {"date": day.isoformat(), "bonus_local_minutes": 0}}}})
    assert fleet.external_extras("kid", day) == {"bonus_seconds": 0, "bedtime_extension_minutes": 30}
    send(fleet, "old-id", state(day, bonus_local_minutes=60), name="Family-PC")
    assert fleet.external_extras("kid", day)["bonus_seconds"] == 0


SHARED = """
time_limits:
  shared_budget: %s
  users:
    - name: kid
      daily_limit_minutes: 60
      bedtime: "21:00"
      wake_time: "07:00"
"""


def test_timekeeper_counts_the_other_pcs_bonus_and_bedtime_extension():
    tk, store, cfg = make_tk(SHARED % "true")
    now = datetime(2026, 10, 13, 20, 0)
    day = now.date()
    extras = {"bonus_seconds": 600, "bedtime_extension_minutes": 30}
    tk.external_extras = lambda user, d: dict(extras) if (user, d) == ("kid", day) else {}
    assert tk.external_extras is not None
    assert tk.local_bonus_seconds("kid", day) == 0 and tk.bonus_seconds("kid", day) == 600
    st = tk.status("kid", now)
    assert st.bonus_seconds == 600 and st.remaining_seconds == 70 * 60
    assert st.bedtime_extension_minutes == 30 and st.bedtime_minute == 21 * 60 + 30
    assert not tk.status("kid", datetime(2026, 10, 13, 21, 15)).out_of_time
    # granting here adds to this PC's own figure, never to the sum
    assert tk.add_bonus_minutes("kid", day, 5) == 300
    assert tk.local_bonus_seconds("kid", day) == 300 and tk.bonus_seconds("kid", day) == 900
    tk.set_bonus_minutes("kid", day, 2)
    assert tk.local_bonus_seconds("kid", day) == 120 and store.get_setting("kid", f"bonus:{day.isoformat()}") == 120
    # the longer of the two extensions counts; extending adds to the one that counts and is
    # stored as this PC's own (so +20 really moves bedtime by 20 minutes)
    assert tk.local_bedtime_extension_minutes("kid", day) == 0 and tk.bedtime_extension_minutes("kid", day) == 30
    assert tk.extend_bedtime("kid", day, 20) == 50 and tk.bedtime_extension_minutes("kid", day) == 50
    assert tk.extend_bedtime("kid", day, 25) == 75 and tk.bedtime_extension_minutes("kid", day) == 75
    assert tk.local_bedtime_extension_minutes("kid", day) == 75
    extras["bonus_seconds"] = 0                                  # the parent took it back on the other PC
    assert tk.bonus_seconds("kid", day) == 120
    assert tk.bonus_seconds("kid", day + timedelta(days=1)) == 0


def test_extras_need_a_shared_budget_and_a_working_provider():
    tk, store, cfg = make_tk(SHARED % "false")
    day = date(2026, 10, 13)
    tk.external_extras = lambda user, d: {"bonus_seconds": 600, "bedtime_extension_minutes": 30}
    assert tk.bonus_seconds("kid", day) == 0 and tk.bedtime_extension_minutes("kid", day) == 0
    tk, store, cfg = make_tk(SHARED % "true")
    assert tk.external_extras is None and tk.bonus_seconds("kid", day) == 0

    def boom(user, d):
        raise RuntimeError("x")

    for provider in (boom, lambda u, d: None, lambda u, d: {"bonus_seconds": "x"}, lambda u, d: 5,
                     lambda u, d: {"bonus_seconds": float("nan"), "bedtime_extension_minutes": float("inf")},
                     lambda u, d: {"bonus_seconds": 0, "bedtime_extension_minutes": -5}):
        tk.external_extras = provider
        assert tk.bonus_seconds("kid", day) == 0 and tk.bedtime_extension_minutes("kid", day) == 0
        tk.status("kid", datetime(2026, 10, 13, 12, 0))
    # a bonus taken away on another PC is a negative total (the bedtime extension cannot be)
    tk.external_extras = lambda u, d: {"bonus_seconds": -50, "bedtime_extension_minutes": -5}
    assert tk.bonus_seconds("kid", day) == -50 and tk.bedtime_extension_minutes("kid", day) == 0
    tk.external_extras = lambda u, d: {"bonus_seconds": 1e12, "bedtime_extension_minutes": 1e12}
    assert tk.bonus_seconds("kid", day) == 86400 and tk.bedtime_extension_minutes("kid", day) == 1440
    # wired to a real tracker
    fleet = make_fleet()
    send(fleet, "pc-a", state(day, bonus_local_minutes=15, bedtime_extension_local_minutes=10))
    tk.external_extras = fleet.external_extras
    assert tk.bonus_seconds("kid", day) == 900 and tk.bedtime_extension_minutes("kid", day) == 10


# -- F4: figures for the weekly summary -----------------------------------------------------

def test_summary_range():
    store = Store(":memory:")
    monday = date(2026, 10, 5)
    for offset, minutes in enumerate([60, 0, 30]):
        day = monday + timedelta(days=offset)
        store.add_usage("Kid", day, active=minutes * 60)
        store.add_app("kid", day, "app:chrome", minutes * 30)
        store.add_app("kid", day, "game:minecraft", minutes * 20)
        store.add_app("kid", day, "legacy.exe", 10)
        store.add_site("kid", day, "youtube.com", minutes * 12)
        store.add_site("kid", day, f"site{offset}.com", 5)
    store.add_usage("kid", monday - timedelta(days=1), active=9999)          # last week: not counted
    store.add_app("kid", monday + timedelta(days=7), "game:later", 9999)      # next week: not counted
    store.add_usage("other", monday, active=777)
    for flushed in (False, True):
        if flushed:
            store.flush()
            store._usage.clear(), store._apps.clear(), store._sites.clear()   # read back from the database
        s = store.summary_range("KID", monday, monday + timedelta(days=6), top=2)
        assert list(s["days"]) == [(monday + timedelta(days=i)).isoformat() for i in range(7)]
        assert s["days"][monday.isoformat()] == 3600 and s["days"][(monday + timedelta(days=1)).isoformat()] == 0
        assert s["active_seconds"] == 5400 and isinstance(s["active_seconds"], int)
        assert s["apps"] == [{"name": "chrome", "seconds": 2700}, {"name": "legacy.exe", "seconds": 30}]
        assert s["games"] == [{"name": "minecraft", "seconds": 1800}]
        assert s["sites"][0] == {"name": "youtube.com", "seconds": 1080} and len(s["sites"]) == 2
    assert len(store.summary_range("kid", monday, monday + timedelta(days=6))["sites"]) == 4
    assert store.summary_range("kid", monday, monday, top=0)["apps"] == []
    empty = store.summary_range("nobody", monday, monday + timedelta(days=1))
    assert empty == {"days": {monday.isoformat(): 0, (monday + timedelta(days=1)).isoformat(): 0},
                     "active_seconds": 0, "apps": [], "sites": [], "games": []}
    assert store.summary_range("kid", monday, monday - timedelta(days=1))["days"] == {}
    json.dumps(s)


def test_count_events():
    store = Store(":memory:")
    start = time.time()
    for user, kind in (("Kid", "time_up"), ("kid", "time_up"), ("kid", "bonus"), ("other", "time_up"), (None, "time_up")):
        store.log_event(user, kind, "x")
    end = time.time() + 1
    assert store.count_events("kid", "time_up", start - 1, end) == 2
    assert store.count_events("KID", "bonus", start - 1, end) == 1
    assert store.count_events(None, "time_up", start - 1, end) == 4
    assert store.count_events("kid", "time_up", end, end + 10) == 0 and store.count_events("kid", "nope", 0, end) == 0
    assert store.count_events("kid", "time_up", start - 10, start - 1) == 0


# -- F5: the PC's time zone ---------------------------------------------------------------------

@pytest.mark.parametrize("name,zone", [
    ("Central Europe Standard Time", "Europe/Budapest"), ("W. Europe Standard Time", "Europe/Berlin"),
    ("GMT Standard Time", "Europe/London"), ("Eastern Standard Time", "America/New_York"),
    ("Pacific Standard Time", "America/Los_Angeles"), ("UTC", "Etc/UTC"), ("India Standard Time", "Asia/Kolkata"),
    ("  central europe   standard time ", "Europe/Budapest"),
    ("Central Europe Daylight Time", "Europe/Budapest"), ("W. Europe Summer Time", "Europe/Berlin"),
    ("Közép-európai téli idő", None), ("Mars Standard Time", None), ("", None), (None, None), (5, None),
])
def test_windows_zone_to_iana(name, zone):
    assert windows_zone_to_iana(name) == zone


def test_every_mapped_zone_exists():
    for windows, iana in util._WINDOWS_ZONES.items():
        assert util.time_zone(iana) is not None, windows


def test_local_iana_zone(monkeypatch):
    zone = local_iana_zone()
    assert zone is None or util.time_zone(zone) is not None       # whatever this machine is set to
    monkeypatch.setattr(util, "_windows_zone_key", lambda: "Tokyo Standard Time")
    assert local_iana_zone() == "Asia/Tokyo"
    import time as time_module
    monkeypatch.setattr(util, "_windows_zone_key", lambda: None)
    monkeypatch.setattr(time_module, "tzname", ("Romance Standard Time", "Romance Daylight Time"))
    monkeypatch.delenv("TZ", raising=False)
    assert local_iana_zone() == "Europe/Paris"
    monkeypatch.setattr(time_module, "tzname", ("Mitteleuropäische Zeit", "Mitteleuropäische Sommerzeit"))
    assert local_iana_zone() is None
    monkeypatch.setenv("TZ", ":Europe/Budapest")
    assert local_iana_zone() == "Europe/Budapest"
    monkeypatch.setenv("TZ", "Not/AZone")
    assert local_iana_zone() is None


def test_api_allowed_hosts():
    assert cfg_of("{}").api.allowed_hosts == []
    cfg = cfg_of("api: {allowed_hosts: [Family-PC.lan, 'family-pc.lan.', router, 192.168.1.20, '[::1]']}")
    assert cfg.api.allowed_hosts == ["family-pc.lan", "router", "192.168.1.20", "::1"]
    assert cfg_of("api: {allowed_hosts: pc.lan}").api.allowed_hosts == ["pc.lan"]
    for bad in ("[http://pc.lan]", "['pc.lan:47392']", "[pc.lan/x]", "['bad host']", "[-pc.lan]", "['']", "[true]",
                "[[a]]", "[" + ", ".join(f"pc{i}.lan" for i in range(21)) + "]", "{a: b}"):
        with pytest.raises(ConfigError, match="allowed_hosts"):
            cfg_of(f"api: {{allowed_hosts: {bad}}}")
    assert len(cfg_of("api: {allowed_hosts: [" + ", ".join(f"pc{i}.lan" for i in range(20)) + "]}").api.allowed_hosts) == 20


def test_example_config_documents_the_new_sections():
    from pclink import example_config
    cfg = cfg_of(example_config.EMBEDDED)
    assert cfg.time_limits.time_requests == TimeRequestsConfig() and cfg.time_limits.profiles == {}
    assert "# profiles:" in example_config.EMBEDDED and "time_requests:" in example_config.EMBEDDED
