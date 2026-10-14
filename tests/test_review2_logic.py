"""Regression tests for the second review round (configuration, rules, activity, fleet,
break reminders, storage, sessions, entity catalog)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytest

from pclink.activity import ActivityClassifier
from pclink.config import BreakConfig, Config, ConfigError, FleetConfig, _parse_minutes, parse_rule
from pclink.entities import EntityFactory
from pclink.fleet import FleetTracker
from pclink.models import Activity, ForegroundInfo
from pclink.rules import RuleEngine
from pclink.store import CACHE_DAYS, Store
from pclink.timekeeper import bedtime_windows
from pclink.util import parse_windows_split
from pclink.wellbeing import BreakReminder


def cfg_of(users=None, **sections) -> Config:
    raw = dict(sections)
    raw.setdefault("time_limits", {})
    if users is not None:
        raw["time_limits"]["users"] = users
    return Config.from_dict(raw)


def policy_of(**user):
    return cfg_of([dict({"name": "kevin"}, **user)]).time_limits.users[0]


# -- 1. bedtime without a wake time --------------------------------------------------

def test_bedtime_without_wake_time_ends_at_six_in_the_morning(caplog):
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        p = policy_of(bedtime="21:00")
    assert all(p.wake_time[d] == 360 for d in range(7))
    assert "wake_time" in caplog.text
    monday = date(2026, 9, 28)
    # allowed until 21:00, and the night runs to 06:00 - not to midnight
    assert bedtime_windows(p, monday, 0, 0) == [(360, 1260)]


def test_bedtime_after_midnight_without_wake_time_does_not_lock_the_whole_day():
    p = policy_of(bedtime="00:30")
    monday = date(2026, 9, 28)
    windows = bedtime_windows(p, monday, 0, 0)
    # last night's 00:30 is the first half hour, then closed until 06:00, open the rest of the day
    assert windows == [(0, 30), (360, 1440)]


def test_bedtime_at_midnight_is_enforced():
    p = policy_of(bedtime="00:00")
    assert bedtime_windows(p, date(2026, 9, 28), 0, 0) == [(360, 1440)]


def test_wake_time_default_only_where_a_night_needs_it():
    p = policy_of(bedtime={"sun-thu": "21:00"})
    # fri (no bedtime, but thursday night had one) needs a wake time; sat has neither
    assert p.wake_time[4] == 360 and p.wake_time[5] == 0 and p.wake_time[0] == 360
    q = policy_of(bedtime="21:00", wake_time="07:30")
    assert set(q.wake_time.values()) == {450}
    r = policy_of()
    assert set(r.wake_time.values()) == {0}


# -- 2. built-in Windows apps are not games ------------------------------------------

def _fg(name, exe, fullscreen=False):
    return ForegroundInfo(pid=1, name=name, exe=exe, title="", fullscreen=fullscreen)


def test_windows_apps_are_not_games_but_microsoft_store_games_are():
    c = ActivityClassifier(Config.from_dict({}).activity)
    apps = r"C:\Program Files\WindowsApps"
    for name, package in (("notepad.exe", "Microsoft.WindowsNotepad_11.2408.12.0_x64__8wekyb3d8bbwe"),
                          ("mspaint.exe", "Microsoft.Paint_11.2408.30.0_x64__8wekyb3d8bbwe"),
                          ("windowsterminal.exe", "Microsoft.WindowsTerminal_1.21_x64__8wekyb3d8bbwe"),
                          ("calculatorapp.exe", "Microsoft.WindowsCalculator_11.2_x64__8wekyb3d8bbwe"),
                          ("olk.exe", "Microsoft.OutlookForWindows_1.2024_x64__8wekyb3d8bbwe")):
        assert not c.is_game(_fg(name, rf"{apps}\{package}\{name}")), name
    assert c.is_game(_fg("minecraft.windows.exe",
                         rf"{apps}\Microsoft.MinecraftUWP_1.21.0.3_x64__8wekyb3d8bbwe\Minecraft.Windows.exe"))
    assert c.is_game(_fg("solitaire.exe",
                         rf"{apps}\Microsoft.MicrosoftSolitaireCollection_4.19_x64__8wekyb3d8bbwe\Solitaire.exe"))


# -- 3. a media player full screen is not a game ---------------------------------------

def test_fullscreen_media_player_is_not_gaming():
    act = Config.from_dict({"activity": {"media_apps": ["mycustomplayer.exe"]}}).activity
    c = ActivityClassifier(act)
    assert not c.is_game(_fg("mycustomplayer.exe", r"C:\Players\mycustomplayer.exe", fullscreen=True))
    default = ActivityClassifier(Config.from_dict({}).activity)
    assert not default.is_game(_fg("kodi.exe", r"C:\Kodi\kodi.exe", fullscreen=True))
    assert c.is_game(_fg("somegame.exe", r"C:\Games\somegame.exe", fullscreen=True))


# -- 4. yes / no flags -------------------------------------------------------------------

def test_flags_written_as_text_mean_what_they_say():
    assert policy_of(unlimited="false").unlimited is False
    assert policy_of(unlimited="yes").unlimited is True
    p = policy_of(count_idle="no", count_away="off", count_passive="false", count_locked="true", block_login="no")
    assert (p.count_idle, p.count_away, p.count_passive, p.count_locked, p.block_login) == (False, False, False, True, False)
    rule = parse_rule({"name": "X", "apps": ["x.exe"], "blocked": "false", "daily_limit_minutes": 10}, "test")
    assert rule.blocked is False
    for bad in ("maybe", 7, [True]):
        with pytest.raises(ConfigError):
            policy_of(unlimited=bad)
        with pytest.raises(ConfigError):
            parse_rule({"name": "X", "apps": ["x.exe"], "blocked": bad}, "test")


# -- 5. ranges ---------------------------------------------------------------------------

@pytest.mark.parametrize("section,values", [
    ("activity", {"idle_seconds": 0}),
    ("activity", {"idle_seconds": 300, "away_seconds": 120}),
    ("activity", {"history_days": 365}),
    ("activity", {"display_off_min_idle_seconds": -1}),
    ("logging", {"max_bytes": 0}),
    ("storage", {"flush_seconds": 100000}),
])
def test_out_of_range_settings_are_refused(section, values):
    with pytest.raises(ConfigError):
        Config.from_dict({section: values})


def test_negative_user_and_break_numbers_are_refused():
    for bad in ({"grace_seconds": -5}, {"idle_threshold_seconds": -1}, {"idle_threshold_minutes": "x"},
                {"breaks": {"duration_seconds": -20}}, {"breaks": {"reset_after_idle_minutes": -1}}):
        with pytest.raises(ConfigError):
            policy_of(**bad)
    assert policy_of(grace_seconds=0).grace_seconds == 0


# -- 6. a rule message that is not text --------------------------------------------------

def _engine():
    return RuleEngine(Store(":memory:"))


def _browsing(domain):
    return Activity(category="browsing", app="chrome.exe", domain=domain)


def test_non_text_rule_message_does_not_stop_the_rules():
    p = policy_of(rules=[{"name": "TikTok", "sites": ["tiktok.com"], "blocked": True, "message": 42},
                         {"name": "Insta", "sites": ["instagram.com"], "blocked": True}])
    assert p.rules[0].message == "42"
    decisions = _engine().tick("kevin", p, datetime(2026, 9, 28, 12, 0), _browsing("tiktok.com"))
    assert decisions and decisions[0].message.startswith("42")


# -- 7. minutes that are not numbers -------------------------------------------------------

def test_bad_minutes_are_config_errors():
    for bad in ("1e308h", "1e400", "abch", "nanh", "infh", "-infm", float("nan"), 10 ** 400):
        with pytest.raises(ConfigError):
            _parse_minutes(bad)
    assert _parse_minutes("1.5h") == 90 and _parse_minutes("10min") == 10 and _parse_minutes("15m") == 15


# -- 8. allowed hours only: warned before the window closes --------------------------------

def test_rule_with_only_allowed_hours_warns_before_it_closes():
    p = policy_of(rules=[{"name": "Roblox", "apps": ["robloxplayerbeta.exe"], "allowed_hours": "16:00-19:00"}])
    eng = _engine()
    act = Activity(category="gaming", app="robloxplayerbeta.exe")
    assert eng.tick("kevin", p, datetime(2026, 9, 28, 18, 0), act) == []
    warns = eng.tick("kevin", p, datetime(2026, 9, 28, 18, 56), act)
    assert warns and warns[0].kind == "warn" and warns[0].minutes_left == 4
    assert "allowed hours" in warns[0].message


# -- 9. 24:00 cannot start a window ----------------------------------------------------------

def test_24_00_as_a_window_start_is_refused():
    with pytest.raises(ValueError):
        parse_windows_split("24:00-07:00")
    with pytest.raises(ConfigError):
        policy_of(allowed_hours={"fri": "24:00-07:00"})
    assert parse_windows_split("18:00-24:00") == ([(1080, 1440)], [])


# -- 10. a focus window past midnight continues into the next day -----------------------------

def test_focus_schedule_keeps_the_part_after_midnight():
    p = policy_of(focus={"schedule": {"fri": "22:00-01:00"}})
    assert p.focus.schedule[4] == [(1320, 1440)]
    assert p.focus.schedule[5] == [(0, 60)]
    # allowed hours keep treating an empty day as unrestricted
    q = policy_of(allowed_hours={"fri": "16:00-01:00"})
    assert q.allowed_windows[5] == []


# -- 11. rule names whose entity ids collide ---------------------------------------------------

def test_rules_whose_entities_would_share_an_id_are_refused():
    with pytest.raises(ConfigError):
        policy_of(rules=[{"name": "YouTube", "sites": ["youtube.com"], "weekly_limit_minutes": 300},
                         {"name": "YouTube weekly", "sites": ["youtube.com"], "daily_limit_minutes": 30}])
    with pytest.raises(ConfigError):
        policy_of(rules=[{"name": "X", "apps": ["x.exe"], "blocked": True},
                         {"name": "X enabled", "apps": ["y.exe"], "blocked": True}])
    # a shared rule and a user's own
    with pytest.raises(ConfigError):
        cfg_of([{"name": "kevin", "rules": [{"name": "Games weekly", "apps": ["x.exe"], "daily_limit_minutes": 9}]}],
               time_limits={"rules": [{"name": "Games", "apps": ["y.exe"], "weekly_limit_minutes": 300}]})
    # the suffixes are the ones the entities really use
    p = policy_of(rules=[{"name": "Games", "apps": ["y.exe"], "daily_limit_minutes": 30, "weekly_limit_minutes": 90}])
    ids = {e.object_id for e in EntityFactory(cfg_of([{"name": "kevin", "rules": [
        {"name": "Games", "apps": ["y.exe"], "daily_limit_minutes": 30, "weekly_limit_minutes": 90}]}])).user_entities(
        "kevin", p, True) if "_rule_" in e.object_id}
    assert {i.split("_rule_", 1)[1] for i in ids} == p.rules[0].entity_ids


# -- 12. sites with a path, apps with a folder ---------------------------------------------------

def test_site_with_a_path_warns_and_app_path_is_cut_to_its_name(caplog):
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        rule = parse_rule({"name": "Shorts", "sites": ["youtube.com/shorts"], "apps": [r"C:\Games\X.exe"],
                           "blocked": True}, "test")
    assert rule.sites == ["youtube.com"] and rule.apps == ["x.exe"]
    assert "youtube.com/shorts" in caplog.text and "x.exe" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        parse_rule({"name": "YT", "sites": ["https://www.youtube.com/"], "blocked": True}, "test")
    assert caplog.text == ""


# -- 13. fleet: huge numbers, negative bonus -------------------------------------------------------

def test_fleet_survives_a_huge_number_and_keeps_the_other_peers():
    fleet = FleetTracker(FleetConfig(), "pc-b", own_name="pc-b")
    day = date(2026, 10, 13)
    taken = fleet.update_peers({
        "pc-a": {"users": {"kid": {"date": day.isoformat(), "used_minutes": 10 ** 400,
                                   "bonus_local_minutes": 10 ** 400}}},
        "pc-c": {"users": {"kid": {"date": day.isoformat(), "used_minutes": 30, "bonus_local_minutes": 60}}},
    })
    assert taken == 2
    today_s, _week = fleet.external_usage("kid", day, day)
    assert today_s == 30 * 60
    assert fleet.external_extras("kid", day)["bonus_seconds"] == 3600


def test_fleet_negative_bonus_offsets_a_positive_one():
    fleet = FleetTracker(FleetConfig(), "pc-b", own_name="pc-b")
    day = date(2026, 10, 13)
    fleet.update_peers({
        "pc-a": {"users": {"kid": {"date": day.isoformat(), "bonus_local_minutes": -60}}},
        "pc-c": {"users": {"kid": {"date": day.isoformat(), "bonus_local_minutes": 60}}},
    })
    assert fleet.external_extras("kid", day)["bonus_seconds"] == 0
    fleet.update_peers({"pc-c": {"users": {"kid": {"date": day.isoformat(), "bonus_local_minutes": 0}}}})
    assert fleet.external_extras("kid", day)["bonus_seconds"] == -3600
    fleet.update_peers({"pc-a": {"users": {"kid": {"date": day.isoformat(), "bonus_local_minutes": -1e9,
                                                    "used_minutes": -50}}}})
    assert fleet.external_extras("kid", day)["bonus_seconds"] == -86400
    assert fleet.external_usage("kid", day, day) == (0.0, 0.0)


# -- 14. break reminders see a real break ------------------------------------------------------------

def _ticks(br, cfg, start, end, day, step=5.0, **kw):
    t, due = start, None
    while t <= end:
        due = br.tick("kevin", cfg, t, day, **kw) or due
        t += step
    return due


def test_a_short_pause_without_input_counts_as_a_break_while_presence_is_still_active():
    br = BreakReminder()
    cfg = BreakConfig(enabled=True, every_minutes=20, duration_seconds=20)
    day = date(2026, 9, 28)
    t0 = 1_000_000.0
    _ticks(br, cfg, t0, t0 + 600, day, engaged=True, input_idle_seconds=1)
    assert br.status("kevin", cfg, t0 + 600)["continuous_minutes"] == 10
    # presence stays "active" for minutes, but there was no input for 25 s: a break
    br.tick("kevin", cfg, t0 + 625, day, engaged=True, input_idle_seconds=25)
    st = br.status("kevin", cfg, t0 + 625)
    assert st["continuous_minutes"] == 0 and st["breaks_taken_today"] == 1
    # a few seconds of reading are not
    br.tick("kevin", cfg, t0 + 630, day, engaged=True, input_idle_seconds=1)
    br.tick("kevin", cfg, t0 + 640, day, engaged=True, input_idle_seconds=8)
    assert br.status("kevin", cfg, t0 + 640)["breaks_taken_today"] == 1
    assert br.status("kevin", cfg, t0 + 640)["continuous_minutes"] > 0


def test_a_film_is_not_a_break_until_reset_after_idle_minutes():
    br = BreakReminder()
    cfg = BreakConfig(enabled=True, every_minutes=20, duration_seconds=20, reset_after_idle_minutes=3)
    day = date(2026, 9, 28)
    t0 = 1_000_000.0
    _ticks(br, cfg, t0, t0 + 120, day, engaged=True, input_idle_seconds=1)
    _ticks(br, cfg, t0 + 125, t0 + 170, day, engaged=True, passive=True, input_idle_seconds=60)
    assert br.status("kevin", cfg, t0 + 170)["breaks_taken_today"] == 0
    br.tick("kevin", cfg, t0 + 175, day, engaged=True, passive=True, input_idle_seconds=181)
    assert br.status("kevin", cfg, t0 + 175)["breaks_taken_today"] == 1
    off = BreakConfig(enabled=True, every_minutes=20, duration_seconds=20, reset_after_idle_minutes=0)
    br2 = BreakReminder()
    _ticks(br2, off, t0, t0 + 60, day, engaged=True, passive=True, input_idle_seconds=1000)
    assert br2.status("kevin", off, t0 + 60)["breaks_taken_today"] == 0


def test_a_ten_minute_sleep_is_a_break():
    br = BreakReminder()
    cfg = BreakConfig(enabled=True, every_minutes=20, duration_seconds=20)
    day = date(2026, 9, 28)
    t0 = 1_000_000.0
    _ticks(br, cfg, t0, t0 + 300, day, engaged=True)
    br.tick("kevin", cfg, t0 + 900, day, engaged=True)
    st = br.status("kevin", cfg, t0 + 900)
    assert st["continuous_minutes"] == 0 and st["breaks_taken_today"] == 1


# -- 15. storage: a clock far ahead, memory, maintenance ---------------------------------------------

def test_purge_after_a_clock_jump_keeps_the_recent_history():
    s = Store(":memory:")
    real = date.today()
    for back in range(10):
        s.add_usage("kevin", real - timedelta(days=back), active=60)
    s.set_setting("kevin", f"bonus:{real.isoformat()}", 15)
    s.flush()
    s.purge(retention_days=90, today=real + timedelta(days=3650))
    assert s.usage("kevin", real).active == 60
    assert s._db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 10
    assert s.get_setting("kevin", f"bonus:{real.isoformat()}") == 15


def test_old_day_buckets_leave_memory_but_stay_readable():
    s = Store(":memory:")
    today = date.today()
    old = today - timedelta(days=CACHE_DAYS + 5)
    s.add_usage("kevin", old, active=60)
    s.add_app("kevin", old, "app:x", 30)
    s.add_usage("kevin", today, active=10)
    s.flush()
    assert ("kevin", old.isoformat()) not in s._usage and ("kevin", old.isoformat()) not in s._apps
    assert ("kevin", today.isoformat()) in s._usage
    assert s.usage("kevin", old).active == 60 and s.top_apps("kevin", old) == [("app:x", 30.0)]
    assert ("kevin", old.isoformat()) not in s._usage   # reading an old day does not cache it
    s.add_usage("kevin", old, active=5)                  # changing one does, until it is written
    assert s.usage("kevin", old).active == 65
    s.flush()
    assert s.usage("kevin", old).active == 65


def test_purge_runs_maintenance_on_a_file_database(tmp_path):
    s = Store(str(tmp_path / "pclink.db"))
    s.add_usage("kevin", date.today(), active=60)
    s.flush()
    s.purge(retention_days=90)
    assert s.usage("kevin", date.today()).active == 60
    s.close()


# -- 16. one session that cannot be queried --------------------------------------------------------

class _FakeWts:
    WTSUserName, WTSDomainName = 5, 7

    def __init__(self, entries, users):
        self.entries, self.users = entries, users

    def WTSEnumerateSessions(self, _server):
        if self.entries is None:
            raise OSError("RPC server unavailable")
        return self.entries

    def WTSQuerySessionInformation(self, _server, sid, what):
        user = self.users.get(sid)
        if isinstance(user, Exception):
            raise user
        return user if what == self.WTSUserName else "PC"


def _sessions(entries, users):
    from pclink.winsessions import WindowsSessions
    ws = WindowsSessions.__new__(WindowsSessions)
    ws._wts = _FakeWts(entries, users)
    ws._query_ex = lambda sid: None
    ws.lock_flag_inverted = False
    ws._known, ws._failed = {}, set()
    return ws


def test_one_session_that_cannot_be_queried_does_not_hide_the_others(caplog):
    from pclink.winsessions import SessionQueryError
    entries = [{"SessionId": 0, "State": 4}, {"SessionId": 1, "State": 0}, {"SessionId": 2, "State": 0},
               {"SessionId": 65536, "State": 6}]
    users = {1: "kevin", 2: "anna", 65536: OSError("listener")}
    ws = _sessions(entries, users)
    assert [s.user for s in ws.list_sessions()] == ["kevin", "anna"]
    ws._wts.users[2] = OSError("access denied")
    with caplog.at_level(logging.WARNING, logger="pclink.winsessions"):
        got = ws.list_sessions()
        ws.list_sessions()
    assert [s.user for s in got] == ["kevin", "anna"]   # anna: what was last known
    assert caplog.text.count("could not be queried") == 1
    fresh = _sessions(entries, {1: "kevin", 2: OSError("access denied")})
    assert [s.user for s in fresh.list_sessions()] == ["kevin"]
    with pytest.raises(SessionQueryError):
        _sessions(None, {}).list_sessions()


# -- 17. attributes kept out of the recorder ---------------------------------------------------------

def test_catalog_lists_attributes_that_change_on_every_push():
    cfg = cfg_of([{"name": "kevin", "daily_limit_minutes": 60,
                   "rules": [{"name": "Games", "apps": ["y.exe"], "daily_limit_minutes": 30}]}],
                 telemetry={"top_processes": 5})
    factory = EntityFactory(cfg)
    entries = {e.object_id: e.catalog_entry() for e in factory.pc_entities({}) +
               factory.user_entities("kevin", cfg.time_limits.users[0], True)}
    assert entries["top_process"]["unrecorded_attributes"] == ["by_cpu"]
    assert "idle_seconds" in entries["user_kevin_presence"]["unrecorded_attributes"]
    assert entries["user_kevin_time_used_today"]["unrecorded_attributes"] == ["history"]
    assert "continuous_minutes" in entries["user_kevin_continuous_use"]["unrecorded_attributes"]
    assert "used_minutes" in entries["user_kevin_rule_games"]["unrecorded_attributes"]
    assert "unrecorded_attributes" not in entries["cpu_percent"]
    for entry in entries.values():
        value = entry.get("unrecorded_attributes")
        assert value is None or (isinstance(value, list) and all(isinstance(v, str) for v in value))
