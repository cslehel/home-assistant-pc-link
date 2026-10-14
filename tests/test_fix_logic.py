"""Fixes from the review of the time accounting, rules, fleet, PIN, store and configuration."""
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta

import pytest
import yaml

from pclink import rules, store as store_module
from pclink.activity import ActivityClassifier
from pclink.config import ActivityConfig, Config, ConfigError, FleetConfig, TimeLimitsConfig
from pclink.fleet import FleetTracker
from pclink.models import Activity, AgentReport, ForegroundInfo, SessionInfo
from pclink.pin import MAX_LOCKOUT_SECONDS, ParentPin, hash_pin, sha256_pin, verify_pin
from pclink.rules import RuleEngine, closable, focus_allows, rule_matches
from pclink.store import SCHEMA_VERSION, Store
from pclink.timekeeper import MAX_FOCUS_MINUTES, TimeKeeper, bedtime_windows
from pclink.util import NO_LIMIT, domain_from_url, normalize_site, registrable_domain, slugify


def cfg_of(text):
    return Config.from_dict(yaml.safe_load(text))


def make_tk(text):
    cfg = cfg_of(text)
    store = Store(":memory:")
    return TimeKeeper(cfg.time_limits, store, cfg.build_default_policy), store, cfg


def sess(user="kid", sid=2, report=None):
    s = SessionInfo(session_id=sid, user=user, state="active", locked=False, idle_seconds=0.0)
    s.report = report
    return s


def browsing(url, domain=None, category="browsing"):
    return Activity(category=category, app="chrome.exe", domain=domain or domain_from_url(url), url=url)


# -- contract C1: new configuration fields ---------------------------------------------

def test_new_config_fields_and_their_validation(caplog):
    cfg = cfg_of("{}")
    assert cfg.api.admin_token is None and cfg.api.tls_cert is None and cfg.api.tls_key is None
    assert cfg.api.allow_config_edit is False
    assert cfg.agent.watchdog_seconds == 90.0
    assert cfg.time_limits.parent_pin_hash is None and not cfg.time_limits.has_pin
    assert "focus" in cfg.time_limits.pin_required_for and "focus_off" in cfg.time_limits.pin_required_for
    cfg = cfg_of("api: {token: aaaaaaaaaaaaaaaaaaaa, read_token: bbbb, admin_token: cccc, tls_cert: c.pem, tls_key: k.pem}\n"
                 "agent: {watchdog_seconds: 0}")
    assert cfg.api.admin_token == "cccc" and cfg.api.tls_cert == "c.pem" and cfg.agent.watchdog_seconds == 0
    for bad in ("api: {token: same, admin_token: same}", "api: {read_token: same, admin_token: same}",
                "api: {tls_cert: c.pem}", "api: {tls_key: k.pem}", "agent: {watchdog_seconds: -1}"):
        with pytest.raises(ConfigError):
            cfg_of(bad)
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        cfg_of("api: {enabled: true, host: 0.0.0.0, token: short}")
    assert "api.token is short" in caplog.text
    assert cfg_of(f"time_limits: {{parent_pin_hash: '{hash_pin('1234', iterations=1000)}'}}").time_limits.has_pin


# -- finding 4: off / no / yes as a limit ---------------------------------------------------

def test_unquoted_off_is_no_limit_and_yes_is_an_error(caplog):
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        cfg = cfg_of("""
time_limits:
  users:
    - name: kid
      daily_limit_minutes: {weekdays: 60, weekend: off}
      weekly_limit_minutes: no
""")
    kid = cfg.user_policy("kid")
    assert kid.daily_limits[0] == 60 and kid.daily_limits[5] is None and kid.daily_limits[6] is None
    assert kid.weekly_limit_minutes is None
    assert "taken as no limit" in caplog.text
    for value in ("yes", "on", "true"):
        with pytest.raises(ConfigError, match="not a number of minutes"):
            cfg_of(f"time_limits: {{users: [{{name: kid, daily_limit_minutes: {value}}}]}}")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        assert cfg_of("time_limits: {users: [{name: kid, weekly_limit_minutes: 0}]}").user_policy("kid").weekly_limit_minutes == 0
    assert "no computer time at all" in caplog.text


# -- finding 3: sites as they are written -----------------------------------------------------

@pytest.mark.parametrize("written,site", [
    ("www.youtube.com", "youtube.com"), ("https://youtube.com/", "youtube.com"), ("*.youtube.com", "youtube.com"),
    ("YouTube.com/watch?v=1", "youtube.com"), ("m.youtube.com", "m.youtube.com"), ("docs.google.com", "docs.google.com"),
    ("münchen.de", "xn--mnchen-3ya.de"), ("192.168.1.10:8123", "192.168.1.10"),
])
def test_sites_are_normalised_at_load(written, site):
    assert normalize_site(written) == site
    cfg = cfg_of(f"""
time_limits:
  rules:
    - name: R
      sites: ["{written}"]
      blocked: true
  users:
    - name: kid
      focus: {{allow_sites: ["{written}"]}}
""")
    kid = cfg.user_policy("kid")
    assert kid.rules[0].sites == [site] and kid.focus.allow_sites == [site]


def test_a_rule_for_one_host_matches_through_the_address():
    cfg = cfg_of("""
time_limits:
  rules:
    - {name: Docs, sites: [docs.google.com], daily_limit_minutes: 30}
    - {name: Tube, sites: [www.youtube.com], blocked: true}
  users: [{name: kid}]
""")
    docs, tube = cfg.user_policy("kid").rules
    # the agent groups by registrable domain: the domain alone says google.com
    assert rule_matches(docs, browsing("https://docs.google.com/document/d/1"))
    assert not rule_matches(docs, browsing("https://mail.google.com/"))
    assert not rule_matches(docs, Activity(category="browsing", app="chrome.exe", domain="google.com"))  # no address known
    assert rule_matches(tube, browsing("https://www.youtube.com/watch?v=1"))
    assert rule_matches(tube, browsing("https://m.youtube.com/"))


def test_example_config_allows_google_docs_in_focus_mode():
    from pclink import example_config
    cfg = cfg_of(example_config.EMBEDDED)
    kevin = cfg.user_policy("kevin")
    assert focus_allows(kevin.focus, browsing("https://docs.google.com/document/d/x"))
    assert not focus_allows(kevin.focus, browsing("https://www.google.com/search?q=games"))
    assert not cfg.api.allow_config_edit


def test_warning_for_a_site_that_can_never_match(caplog):
    text = """
activity: {url_method: %s}
time_limits:
  rules: [{name: Docs, sites: [docs.google.com], blocked: true}]
  users: [{name: kid}]
"""
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        cfg_of(text % "uia")
    assert "can never match" not in caplog.text
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        cfg_of(text % "title")
    assert "'docs.google.com' can never match" in caplog.text


def test_site_category_of_a_single_host():
    classifier = ActivityClassifier(ActivityConfig(site_categories={"google.com": "search", "docs.google.com": "education"}))
    assert classifier.site_category("google.com", "https://docs.google.com/document/d/1") == "education"
    assert classifier.site_category("google.com", "https://www.google.com/") == "search"
    assert classifier.site_category("google.com") == "search" and classifier.site_category(None) == "browsing"


# -- finding 5: values that used to pass validation and break later ----------------------------

def test_invalid_title_pattern_is_a_config_error_and_never_stops_the_classifier():
    with pytest.raises(ConfigError, match="game_title_patterns"):
        cfg_of("activity: {game_title_patterns: ['(Minecraft']}")
    classifier = ActivityClassifier(ActivityConfig(game_title_patterns=["(Minecraft", "^Roblox"]))
    assert [p.pattern for p in classifier.title_patterns] == ["^Roblox"]


def test_default_policy_is_validated_at_load(caplog):
    with pytest.raises(ConfigError, match="default_policy"):
        cfg_of("time_limits: {manage_unknown_users: true, default_policy: {daily_limit_minutes: 60, action: logout}}")
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        cfg_of("time_limits: {default_policy: {action: logout}}")      # not used: a warning is enough
    assert "default_policy is not valid" in caplog.text


def test_one_failing_user_does_not_stop_the_others():
    cfg = cfg_of("time_limits: {manage_unknown_users: true, users: [{name: kid, daily_limit_minutes: 60}]}")
    store = Store(":memory:")

    def build(user):
        raise ConfigError("broken default policy")

    tk = TimeKeeper(cfg.time_limits, store, build)
    now = datetime(2026, 10, 13, 10, 0)
    sessions = {"aaa": [sess("aaa", 1)], "kid": [sess("kid", 2)], "zzz": [sess("zzz", 3)]}
    tk.tick(now, sessions)
    tk.tick(now + timedelta(seconds=5), sessions)
    assert store.usage("kid", now.date()).active == 5


def test_retention_tick_and_history_are_brought_into_range(caplog):
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        cfg = cfg_of("""
storage: {retention_days: 3}
activity: {history_days: 2}
time_limits: {tick_seconds: 600, shared_budget: true}
""")
    assert cfg.storage.retention_days == 8 and cfg.time_limits.tick_seconds == 60 and cfg.activity.history_days == 7
    assert "retention_days" in caplog.text and "tick_seconds" in caplog.text and "history_days" in caplog.text
    assert cfg_of("activity: {history_days: 2}").activity.history_days == 2     # only a shared budget needs the week
    # the weekly budget survives the purge of such a configuration
    tk, store, cfg = make_tk("storage: {retention_days: 3}\ntime_limits: {users: [{name: kid, weekly_limit_minutes: 300}]}")
    monday = date(2026, 10, 12)
    for i in range(6):
        store.add_usage("kid", monday + timedelta(days=i), active=3600)
    store.flush()
    now = datetime(2026, 10, 17, 12, 0)
    store.purge(cfg.storage.retention_days, now.date())
    st = tk.status("kid", now)
    assert st.weekly_used_seconds == 6 * 3600 and st.limiting_factor == "weekly"
    # a long tick no longer looks like sleep
    tk, store, cfg = make_tk("time_limits: {tick_seconds: 600, users: [{name: kid, daily_limit_minutes: 600}]}")
    start = datetime(2026, 10, 13, 10, 0)
    for i in range(61):
        tk.tick(start + timedelta(seconds=cfg.time_limits.tick_seconds * i), {"kid": [sess()]})
    assert store.usage("kid", start.date()).active == 3600


def test_pin_kinds_are_checked(caplog):
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        cfg = cfg_of("time_limits: {parent_pin: '1234', pin_required_for: [grants, Unblock, nonsense]}")
    assert cfg.time_limits.pin_required_for == ["grant", "unblock", "nonsense"]
    assert "'grants' is not a kind of action; read as 'grant'" in caplog.text and "guards nothing" in caplog.text
    assert ParentPin(cfg.time_limits).requires("grant")


def test_pin_hashes_are_checked(caplog):
    with caplog.at_level(logging.ERROR, logger="pclink.config"):
        cfg = cfg_of("time_limits: {parent_pin_sha256: abc}")
    assert "not a SHA-256 digest" in caplog.text
    pin = ParentPin(cfg.time_limits)
    assert pin.enabled and not pin.try_unlock("1234")            # still refuses everything, as before
    for bad in ("abc", "pbkdf2_sha256$0$00$00", "pbkdf2_sha256$999999999$" + "ab" * 16 + "$" + "cd" * 32):
        with pytest.raises(ConfigError, match="parent_pin_hash"):
            cfg_of(f"time_limits: {{parent_pin_hash: '{bad}'}}")
    assert cfg_of(f"time_limits: {{parent_pin_sha256: {sha256_pin('1234').upper()}}}").time_limits.parent_pin_sha256 == sha256_pin("1234")


@pytest.mark.parametrize("written", ["0123", "0000", "1_000", "0x10", "+12"])
def test_a_pin_that_yaml_changed_is_an_error(written):
    with pytest.raises(ConfigError, match="quotes"):
        Config.from_yaml(f"time_limits:\n  parent_pin: {written}\n")


def test_pins_that_yaml_leaves_alone_still_load(tmp_path, caplog):
    assert Config.from_yaml('time_limits:\n  parent_pin: "0123"\n').time_limits.parent_pin == "0123"
    with caplog.at_level(logging.WARNING, logger="pclink.config"):
        assert Config.from_yaml("time_limits:\n  parent_pin: 1234  # the PIN\n").time_limits.parent_pin == "1234"
    assert "not quoted" in caplog.text
    assert Config.from_yaml("time_limits:\n  parent_pin: 0900\n").time_limits.parent_pin == "0900"  # a string to YAML
    for bad in ("1.5", "yes", "[1, 2]"):
        with pytest.raises(ConfigError, match="parent_pin"):
            Config.from_yaml(f"time_limits:\n  parent_pin: {bad}\n")
    path = tmp_path / "config.yaml"
    path.write_text("time_limits:\n  parent_pin: 0123\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="config.yaml.*quotes"):
        Config.load(str(path))
    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        Config.load(str(path))


# -- contract C4: slugs -------------------------------------------------------------------

def test_slugs_of_names_without_an_ascii_form():
    assert slugify("Kevin") == "kevin" and slugify("Núñez") == "nunez" and slugify("") == "x" and slugify("!!!") == "x"
    a, b = slugify("Петя"), slugify("Вася")
    assert a != b and a == slugify("петя") == slugify(" Петя ")
    assert len(a) == 9 and a[0] == "u" and all(c in "0123456789abcdef" for c in a[1:])
    assert slugify("太郎") not in ("x", a, b)


def test_two_users_with_the_same_slug_are_rejected():
    with pytest.raises(ConfigError, match="'kid.a' and 'kid_a'"):
        cfg_of("time_limits: {users: [{name: kid.a}, {name: kid_a}]}")
    cfg = cfg_of("time_limits: {users: [{name: Петя}, {name: Вася}]}")
    assert len({p.slug for p in cfg.time_limits.users}) == 2


# -- finding 10: what a site is ----------------------------------------------------------------

@pytest.mark.parametrize("host,expected", [
    ("games.example.co.rs", "example.co.rs"), ("www.example.com.xx", "example.com.xx"), ("a.b.uni.ac.zz", "uni.ac.zz"),
    ("kevin.github.io", "kevin.github.io"), ("www.kevin.github.io", "kevin.github.io"), ("github.io", "github.io"),
    ("shop.blogspot.com", "shop.blogspot.com"), ("www.bbc.co.uk", "bbc.co.uk"), ("m.youtube.com", "youtube.com"),
    ("t.co", "t.co"), ("www.example.de", "example.de"),
])
def test_registrable_domain_of_country_and_hosting_suffixes(host, expected):
    assert registrable_domain(host) == expected


def test_idn_and_ip_hosts_are_sites():
    assert domain_from_url("https://münchen.de/rathaus") == "xn--mnchen-3ya.de"
    assert domain_from_url("https://www.MÜNCHEN.de/", "host") == "www.xn--mnchen-3ya.de"
    assert domain_from_url("https://xn--mnchen-3ya.de/") == "xn--mnchen-3ya.de"
    assert domain_from_url("пример.рф") == "xn--e1afmkfd.xn--p1ai"
    assert domain_from_url("http://142.250.74.14/x") == "142.250.74.14" and domain_from_url("https://[::1]/") == "::1"
    assert domain_from_url("https://youtube.com%2eevil.com/") is None and domain_from_url("ein wörtchen") is None
    rule = cfg_of("time_limits: {rules: [{name: R, sites: [münchen.de, 142.250.74.14], blocked: true}], "
                  "users: [{name: kid}]}").user_policy("kid").rules[0]
    assert rule_matches(rule, browsing("https://www.münchen.de/")) and rule_matches(rule, browsing("http://142.250.74.14/"))


# -- finding 9 / contract C2: the PIN ---------------------------------------------------------

def test_pin_hash_is_salted_and_the_old_form_still_works():
    first, second = hash_pin("1234"), hash_pin("1234")
    assert first != second and first.startswith("pbkdf2_sha256$200000$") and len(first.split("$")) == 4
    assert verify_pin("1234", first) and verify_pin(" 1234 ", second) and not verify_pin("1235", first)
    assert verify_pin("1234", sha256_pin("1234")) and not verify_pin("1234", "abc") and not verify_pin("1234", None)
    assert not verify_pin("1234", "pbkdf2_sha256$x$y$z") and not verify_pin("1234", "pbkdf2_sha256$999999999$00$00")
    quick = hash_pin("4321", iterations=1000)
    pin = ParentPin(TimeLimitsConfig(parent_pin_hash=quick, parent_pin_sha256=sha256_pin("9999")))
    assert pin.enabled and pin.try_unlock("4321")
    assert not ParentPin(TimeLimitsConfig(parent_pin_hash=quick)).try_unlock("9999")   # the salted form wins
    assert ParentPin(TimeLimitsConfig(parent_pin_sha256=sha256_pin("9999"))).try_unlock("9999")


def make_pin(store=None, **kwargs):
    cfg = TimeLimitsConfig(parent_pin="1234", pin_max_attempts=2, pin_lockout_seconds=300, **kwargs)
    clock = {"t": 1_000_000.0}
    return ParentPin(cfg, clock=lambda: clock["t"], store=store), clock, cfg


def lock_out(pin):
    assert not pin.try_unlock("0") and not pin.try_unlock("0") and pin.locked_out()
    return pin.status()["lockout_seconds_left"]


def test_lockouts_in_a_row_get_longer_until_a_correct_pin():
    store = Store(":memory:")
    pin, clock, cfg = make_pin(store)
    seen = []
    for _ in range(6):
        seen.append(lock_out(pin))
        clock["t"] += seen[-1] + 1
    assert seen == [300, 600, 1200, 2400, MAX_LOCKOUT_SECONDS, MAX_LOCKOUT_SECONDS]
    # a restart does not shorten the next one
    pin = ParentPin(cfg, clock=lambda: clock["t"], store=store)
    assert lock_out(pin) == MAX_LOCKOUT_SECONDS
    clock["t"] += MAX_LOCKOUT_SECONDS + 1
    assert pin.try_unlock("1234") and store.get_setting(Store.GLOBAL, "pin_lockouts") == 0
    pin.lock()
    assert lock_out(pin) == 300
    # a lockout configured longer than the cap is kept as it is
    long_pin, long_clock, _ = make_pin(pin_timeout_seconds=60)
    long_pin.cfg.pin_lockout_seconds = 7200
    assert lock_out(long_pin) == 7200


def test_a_changed_pin_ends_the_lockout():
    store = Store(":memory:")
    pin, clock, cfg = make_pin(store)
    lock_out(pin)
    pin.update_config(cfg)                                    # the same PIN: nothing changes
    assert pin.locked_out()
    pin.update_config(TimeLimitsConfig(parent_pin="5678", pin_max_attempts=2))
    assert not pin.locked_out() and not pin.unlocked() and pin.try_unlock("5678")
    assert store.get_setting(Store.GLOBAL, "pin_lockouts") == 0


def test_a_pin_in_a_command_authorises_that_command_only():
    pin, clock, cfg = make_pin()
    assert pin.authorize("grant", pin="1234") == (True, "")
    assert not pin.unlocked() and pin.authorize("grant") == (False, "parent pin required")
    assert pin.authorize("focus") == (False, "parent pin required")     # the new kind is guarded by default
    assert pin.authorize("grant", pin="0")[0] is False and pin.authorize("grant", pin="0") == (False, "locked out")
    assert pin.authorize("grant", pin="1234") == (False, "locked out")


# -- contract C3 and finding 8: focus --------------------------------------------------------

FOCUS = """
time_limits:
  users:
    - name: kid
      daily_limit_minutes: 60
      focus: {schedule: "%s", default_minutes: 45}
"""


def test_focus_period_is_clamped_and_absurd_stored_values_are_tolerated():
    tk, store, cfg = make_tk(FOCUS % "14:00-15:00")
    policy, now = cfg.user_policy("kid"), datetime(2026, 10, 13, 10, 0)
    assert tk.start_focus("kid", now, 10 ** 9) == now.timestamp() + MAX_FOCUS_MINUTES * 60
    assert tk.start_focus("kid", now, -5) == now.timestamp() + 60
    assert tk.start_focus("kid", now, float("nan")) == now.timestamp() + 45 * 60
    assert tk.start_focus("kid", now, "soon") == now.timestamp() + 45 * 60
    assert tk.start_focus("kid", now, None) == now.timestamp() + 45 * 60
    for stored in (1e18, float("inf"), "abc", True, [1]):
        store.set_setting("kid", "focus_until", stored)
        active, until, _source = tk.focus_state("kid", policy, now)
        assert not active or until <= now.timestamp() + MAX_FOCUS_MINUTES * 60
        tk.status("kid", now)                                    # and the tick's status never raises over it
    store.set_setting("kid", "focus_until", 1e18)
    assert tk.focus_state("kid", policy, now) == (True, now.timestamp() + MAX_FOCUS_MINUTES * 60, "manual")
    assert tk.focus_state("kid", policy, now + timedelta(minutes=MAX_FOCUS_MINUTES + 1))[0] is False   # it does end


def test_stopping_focus_ends_only_the_current_scheduled_window():
    tk, store, cfg = make_tk(FOCUS % "14:00-15:00, 17:00-18:00")
    policy, day = cfg.user_policy("kid"), datetime(2026, 10, 13)
    at = lambda h, m=0: day + timedelta(hours=h, minutes=m)  # noqa: E731
    assert tk.focus_state("kid", policy, at(14, 30))[0]
    tk.stop_focus("kid", at(14, 30))
    assert not tk.focus_state("kid", policy, at(14, 45))[0]
    assert not tk.focus_state("kid", policy, at(16))[0]
    assert tk.focus_state("kid", policy, at(17, 30)) == (True, at(18).timestamp(), "schedule")
    assert tk.focus_state("kid", policy, at(38, 30))[0]              # and tomorrow's first window as well
    tk.stop_focus("kid", at(12))                                     # outside a window: nothing to skip
    assert tk.focus_state("kid", policy, at(14, 30))[0]


def test_a_stopped_focus_window_that_wraps_midnight_stays_stopped():
    tk, store, cfg = make_tk(FOCUS % "22:00-01:00")
    policy, day = cfg.user_policy("kid"), datetime(2026, 10, 13)
    at = lambda h, m=0: day + timedelta(hours=h, minutes=m)  # noqa: E731
    assert tk.focus_state("kid", policy, at(23)) == (True, at(25).timestamp(), "schedule")   # ends at 01:00, not midnight
    tk.stop_focus("kid", at(23))
    assert not tk.focus_state("kid", policy, at(23, 30))[0]
    assert not tk.focus_state("kid", policy, at(24, 30))[0]          # after midnight: the same window
    assert tk.focus_state("kid", policy, at(46, 30))[0]              # the next evening it is on again


# -- finding 7: a night without a bedtime ---------------------------------------------------

SCHOOL_NIGHTS = """
time_limits:
  users:
    - name: kid
      bedtime: {sun: "21:00", mon: "21:00", tue: "21:00", wed: "21:00", thu: "21:00"}
      wake_time: "07:00"
"""


def test_a_night_without_a_bedtime_does_not_end_at_midnight():
    tk, store, cfg = make_tk(SCHOOL_NIGHTS)
    policy = cfg.user_policy("kid")
    friday, saturday, sunday, monday = (date(2026, 10, 16) + timedelta(days=i) for i in range(4))
    assert bedtime_windows(policy, saturday, 0, 0) == [(0, 1440)]
    assert bedtime_windows(policy, sunday, 0, 0) == [(0, 21 * 60)]            # Saturday night simply runs on
    assert bedtime_windows(policy, monday, 0, 0) == [(7 * 60, 21 * 60)]       # after a school night: from the wake time
    assert bedtime_windows(policy, friday, 0, 0) == [(7 * 60, 1440)]          # Thursday's night lasts until Friday's wake time
    st = tk.status("kid", datetime(2026, 10, 17, 23, 0))                      # Saturday 23:00
    assert st.in_bedtime_window and st.bedtime_remaining_seconds == (60 + 21 * 60) * 60
    st = tk.status("kid", datetime(2026, 10, 18, 0, 5))                       # Sunday 00:05
    assert st.in_bedtime_window and not st.out_of_time
    assert tk.status("kid", datetime(2026, 10, 18, 21, 5)).limiting_factor == "bedtime"
    assert tk.status("kid", datetime(2026, 10, 19, 0, 5)).limiting_factor == "bedtime"    # Monday, before the wake time
    assert tk.status("kid", datetime(2026, 10, 16, 3, 0)).limiting_factor == "bedtime"    # Friday 03:00: Thursday's night
    assert tk.status("kid", datetime(2026, 10, 17, 3, 0)).limiting_factor is None         # Saturday 03:00: Friday's


# -- finding 11: lock is repeated silently ---------------------------------------------------

def test_lock_is_logged_once_and_repeated_silently():
    tk, store, cfg = make_tk("time_limits: {users: [{name: kid, daily_limit_minutes: 1, action: lock, grace_seconds: 10}]}")
    now = datetime(2026, 10, 13, 16, 0)
    store.add_usage("kid", now.date(), active=120)
    kinds = []
    for i in range(0, 400, 5):
        kinds += [a.kind for a in tk.tick(now + timedelta(seconds=i), {"kid": [sess()]})]
    assert kinds.count("lock") >= 10
    assert len(store.recent_events(100, user="kid", kind="lock")) == 1
    # a logoff is still recorded every time it has to be done
    tk, store, cfg = make_tk("time_limits: {users: [{name: kid, daily_limit_minutes: 1, grace_seconds: 10}]}")
    store.add_usage("kid", now.date(), active=120)
    for i in range(0, 100, 5):
        tk.tick(now + timedelta(seconds=i), {"kid": [sess()]})
    assert len(store.recent_events(100, user="kid", kind="logoff")) >= 2


# -- finding 14: the service clock ---------------------------------------------------------------

def test_limit_changes_are_judged_on_the_service_clocks_day():
    tk, store, cfg = make_tk("time_limits: {users: [{name: kid, daily_limit_minutes: {weekdays: 60, weekend: 120}}]}")
    saturday = datetime(2026, 10, 17, 12, 0)
    tk.status("kid", saturday)
    assert tk._daily_limits_from("kid") == [120, 120, 60, 60, 60, 60, 60]
    tk.tick(saturday + timedelta(days=2), {})
    assert tk._daily_limits_from("kid")[0] == 60


# -- finding 2: a shared budget never fails open ------------------------------------------------

SHARED = "time_limits: {shared_budget: true, users: [{name: kid, daily_limit_minutes: 60, weekly_limit_minutes: 200}]}"


def peer_payload(used, day, history=None, user="kid"):
    return {"user": user, "date": day.isoformat(), "used_minutes": used,
            "history": history if history is not None else [{"date": day.isoformat(), "active_minutes": used}]}


def feed(fleet, dev, payload, name=None):
    info = {"users": {payload["user"]: payload}}
    if name is not None:
        info["name"] = name
    return fleet.update_peers({dev: info})


def test_shared_budget_is_remembered_across_a_restart_without_a_network():
    cfg = cfg_of(SHARED)
    store = Store(":memory:")
    now = datetime(2026, 10, 14, 16, 0)                       # a Wednesday
    today, monday = now.date(), date(2026, 10, 12)
    fleet = FleetTracker(cfg.fleet, "pc-b", own_name="pc-b")
    tk = TimeKeeper(cfg.time_limits, store)
    tk.external_usage = fleet.external_usage
    feed(fleet, "pc-a", peer_payload(60, today, [
        {"date": monday.isoformat(), "active_minutes": 100}, {"date": today.isoformat(), "active_minutes": 60}]))
    st = tk.status("kid", now)
    assert st.out_of_time and st.external_used_seconds == 3600 and st.weekly_used_seconds == 160 * 60
    # the PC is restarted with the cable pulled: nothing is heard from the other PC
    fleet = FleetTracker(cfg.fleet, "pc-b", own_name="pc-b")
    tk = TimeKeeper(cfg.time_limits, store)
    tk.external_usage = fleet.external_usage
    assert not fleet.ready()
    st = tk.status("kid", now)
    assert st.out_of_time and st.limiting_factor == "daily" and st.external_used_seconds == 3600
    assert st.weekly_used_seconds == 160 * 60
    # the next day starts fresh, the week does not
    st = tk.status("kid", now + timedelta(days=1))
    assert st.external_used_seconds == 0 and st.weekly_used_seconds == 160 * 60 and not st.out_of_time
    # a provider that reports less than before does not give time back either
    tk.external_usage = lambda user, day, week_start: (0.0, 0.0)
    assert tk.status("kid", now).external_used_seconds == 3600


def test_usage_on_other_pcs_is_not_written_on_every_tick():
    tk, store, cfg = make_tk(SHARED)
    now = datetime(2026, 10, 14, 16, 0)
    used = {"s": 600.0}
    tk.external_usage = lambda user, day, week_start: (used["s"], used["s"])
    tk.status("kid", now)
    assert store.get_setting("kid", f"external:{now.date().isoformat()}") == 600
    used["s"] = 630.0
    assert tk.status("kid", now).external_used_seconds == 630           # the live figure counts ...
    assert store.get_setting("kid", f"external:{now.date().isoformat()}") == 600   # ... but is not written yet
    used["s"] = 660.0
    tk.status("kid", now)
    assert store.get_setting("kid", f"external:{now.date().isoformat()}") == 660


# -- finding 6: what a peer can do ------------------------------------------------------------

def make_fleet(device="pc-b", own_name="pc-b"):
    return FleetTracker(FleetConfig(), device, own_name=own_name)


def test_missing_or_lower_figures_never_reduce_a_peers_usage():
    fleet = make_fleet()
    day = date(2026, 10, 13)
    monday = date(2026, 10, 12)
    feed(fleet, "pc-a", peer_payload(60, day, [
        {"date": monday.isoformat(), "active_minutes": 30}, {"date": day.isoformat(), "active_minutes": 60}]))
    assert fleet.external_usage("kid", day, monday) == (3600.0, 5400.0)
    assert fleet.update_peers({"pc-a": {"users": {}}}) == 1                    # the user is not mentioned
    assert fleet.external_usage("kid", day, monday) == (3600.0, 5400.0)
    assert fleet.update_peers({}) == 0                                         # nor is the PC
    assert fleet.external_usage("kid", day, monday) == (3600.0, 5400.0)
    feed(fleet, "pc-a", peer_payload(5, day, []))                              # lower figures, no history
    assert fleet.external_usage("kid", day, monday) == (3600.0, 5400.0)
    assert fleet.update_peers({"pc-a": {"users": {"kid": {"date": day.isoformat(), "used_minutes": 0, "history": []}}}}) == 1
    assert fleet.external_usage("kid", day, monday) == (3600.0, 5400.0)
    feed(fleet, "pc-a", peer_payload(75, day))                                 # more is taken at once
    assert fleet.external_usage("kid", day, monday) == (4500.0, 6300.0)


def test_forged_peers_are_capped_and_made_up_dates_ignored():
    fleet = make_fleet()
    day = date.today()
    for i in range(5):
        feed(fleet, f"evil{i}", peer_payload(99999, day))
    assert fleet.external_usage("kid", day, day) == (86400.0, 86400.0)        # a day has 24 hours
    far = [{"date": (day + timedelta(days=30 + i)).isoformat(), "active_minutes": 600} for i in range(40)]
    feed(fleet, "pc-a", peer_payload(10, day, far))
    assert set(fleet.peers()["pc-a"].usage["kid"]) == {day.isoformat()}


def test_this_pc_under_an_earlier_device_id_is_not_a_peer():
    fleet = make_fleet("new-id", own_name="Family-PC")
    day = date(2026, 10, 13)
    feed(fleet, "old-id", peer_payload(60, day), name="family-pc")
    feed(fleet, "den", peer_payload(20, day), name="Den")
    assert fleet.external_usage("kid", day, day) == (1200.0, 1200.0)
    assert "old-id" in fleet.peers()                                           # kept, just not counted


# -- finding 12: what is never closed --------------------------------------------------------

def test_the_agents_name_no_longer_protects_a_program():
    for name in ("python.exe", "pythonw.exe", "pclink.exe", "pclink-agent.exe", "taskmgr.exe"):
        assert name not in rules.NEVER_CLOSE and closable(name)
    assert not closable("explorer.exe") and not closable("LogonUI.exe") and not closable(None)
    cfg = cfg_of("time_limits: {users: [{name: kid, daily_limit_minutes: 60, focus: {allow_apps: [winword.exe]}}]}")
    policy = cfg.user_policy("kid")
    assert not focus_allows(policy.focus, Activity(category="gaming", app="python.exe"))
    assert not focus_allows(policy.focus, Activity(category="app", app="pclink-agent.exe"))
    assert focus_allows(policy.focus, Activity(category="app", app="explorer.exe"))
    engine = RuleEngine(Store(":memory:"))
    now = datetime(2026, 10, 13, 16, 0)
    game = Activity(category="gaming", app="pclink-agent.exe")
    assert [d.source for d in engine.tick("kid", policy, now, game, focus_active=True)] == ["focus"]
    # the real agent is told apart by its process id (the orchestrator says so), not by its name
    assert engine.tick("kid", policy, now, game, focus_active=True, agent_foreground=True) == []


# -- "no limit" is not "no override" ----------------------------------------------------------

def test_no_limit_can_be_stored_for_every_kind_of_limit():
    tk, store, cfg = make_tk("""
time_limits:
  parent_pin: "1234"
  users:
    - name: kid
      daily_limit_minutes: 60
      weekly_limit_minutes: 300
      rules: [{name: Games, apps: [game.exe], daily_limit_minutes: 30, weekly_limit_minutes: 100}]
""")
    now = datetime(2026, 10, 13, 12, 0)                       # a Tuesday
    today = now.date()
    assert tk.is_loosening_limit("kid", "today", NO_LIMIT, today) and tk.is_loosening_limit("kid", "weekday", NO_LIMIT, today)
    tk.set_limit_today("kid", today, NO_LIMIT)
    assert tk.effective_limit_minutes("kid", today) is None and tk.status("kid", now).remaining_seconds is None
    assert tk.effective_limit_minutes("kid", today + timedelta(days=1)) == 60
    assert not tk.is_loosening_limit("kid", "today", 500, today)          # nothing is looser than no limit
    tk.set_limit_today("kid", today, None)                                # remove the override
    assert tk.effective_limit_minutes("kid", today) == 60
    tk.set_limit_today("kid", today, 0)
    assert tk.effective_limit_minutes("kid", today) == 0
    tk.set_limit_today("kid", today, None)
    tk.set_weekday_limit("kid", -7)
    assert store.get_setting("kid", "weekday_limit") == NO_LIMIT and tk.effective_limit_minutes("kid", today) is None
    assert tk.effective_limit_minutes("kid", date(2026, 10, 17)) == 60   # the weekend keeps its limit
    assert not tk.is_loosening_limit("kid", "weekday", 90, today)
    tk.set_weekday_limit("kid", None)
    tk.set_weekend_limit("kid", NO_LIMIT)
    assert tk.effective_limit_minutes("kid", date(2026, 10, 17)) is None
    tk.set_weekly_limit("kid", 0)
    assert tk.weekly_limit_minutes("kid") is None
    tk.set_weekly_limit("kid", NO_LIMIT)
    assert tk.weekly_limit_minutes("kid") is None and store.get_setting("kid", "weekly_limit") == 0
    tk.set_weekly_limit("kid", None)
    assert tk.weekly_limit_minutes("kid") == 300
    engine, rule = RuleEngine(store), cfg.user_policy("kid").rules[0]
    assert engine.is_loosening_rule_limit("kid", rule, NO_LIMIT, today)
    engine.set_rule_limit_today("kid", rule.slug, today, NO_LIMIT)
    assert engine.rule_limit_today("kid", rule, today) is None
    engine.set_rule_limit_today("kid", rule.slug, today, None)
    assert engine.rule_limit_today("kid", rule, today) == 30
    engine.set_rule_weekly_limit("kid", rule.slug, 0)
    assert engine.rule_weekly_limit("kid", rule) is None
    engine.set_rule_weekly_limit("kid", rule.slug, None)
    assert engine.rule_weekly_limit("kid", rule) == 100


# -- finding 13: the store --------------------------------------------------------------------

class _Boom(dict):
    def items(self):
        raise RuntimeError("not a database error")


def test_flush_rolls_back_whatever_goes_wrong():
    store = Store(":memory:")
    day = date(2026, 10, 13)
    store.add_usage("kid", day, active=60)
    store.add_app("kid", day, "app:game", 60)
    good = store._apps[("kid", day.isoformat())]
    store._apps[("kid", day.isoformat())] = _Boom(good)
    store.flush()                                                 # logged, not raised
    assert not store._db.in_transaction and store._dirty_usage
    store._apps[("kid", day.isoformat())] = good
    store.flush()
    assert not store._dirty_usage and not store._dirty_apps
    assert store._db.execute("SELECT active FROM usage WHERE user='kid'").fetchone() == (60.0,)
    # a dirty day whose bucket was purged in the meantime is skipped, not fatal
    store._dirty_usage.add(("ghost", "2020-01-01"))
    store._dirty_sites.add(("ghost", "2020-01-01"))
    store.add_usage("kid", day, active=5)
    store.flush()
    assert store._db.execute("SELECT active FROM usage WHERE user='kid'").fetchone() == (65.0,)


def test_schema_version_and_migration_hook(tmp_path, monkeypatch):
    path = str(tmp_path / "pclink.db")
    store = Store(path)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.set_setting("kid", "bonus:2026-10-13", 600)
    store.close()
    # an old database (no version) is taken as it is
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 0")
    db.commit()
    db.close()
    ran = []

    def to_next(db):
        ran.append(True)
        db.execute("ALTER TABLE events ADD COLUMN source TEXT")

    monkeypatch.setattr(store_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setitem(store_module._MIGRATIONS, SCHEMA_VERSION + 1, to_next)
    store = Store(path)
    assert ran == [True] and store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    assert "source" in [row[1] for row in store._db.execute("PRAGMA table_info(events)")]
    assert store.get_setting("kid", "bonus:2026-10-13") == 600
    store.close()
    store = Store(path)                                            # not run a second time
    assert ran == [True]
    store.close()
    # a database from a newer version is opened, not downgraded
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", SCHEMA_VERSION)
    store = Store(path)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    store.close()


def test_failed_migration_changes_nothing(tmp_path, monkeypatch):
    path = str(tmp_path / "pclink.db")
    Store(path).close()

    def broken(db):
        db.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("migration bug")

    monkeypatch.setattr(store_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setitem(store_module._MIGRATIONS, SCHEMA_VERSION + 1, broken)
    with pytest.raises(RuntimeError):
        Store(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchone() is None
    db.close()
    assert not [f for f in os.listdir(tmp_path) if "corrupt" in f]   # a bug of ours is no reason to drop the data


def test_corrupt_database_is_moved_aside(tmp_path, caplog):
    path = tmp_path / "pclink.db"
    path.write_bytes(b"this is not a database " * 200)
    with caplog.at_level(logging.ERROR, logger="pclink.store"):
        store = Store(str(path))
    assert store.recovered_from and os.path.basename(store.recovered_from).startswith("pclink.db.corrupt-")
    assert open(store.recovered_from, "rb").read().startswith(b"this is not a database")
    assert "is corrupt" in caplog.text
    store.set_setting("kid", "blocked", True)
    store.add_usage("kid", date(2026, 10, 13), active=30)
    store.close()
    again = Store(str(path))
    assert again.recovered_from is None and again.get_setting("kid", "blocked") is True
    assert again.usage("kid", date(2026, 10, 13)).active == 30
    again.close()
    # a damaged page inside a real database is found as well
    data = bytearray(path.read_bytes())
    data[100:len(data)] = b"\xff" * (len(data) - 100)
    for leftover in ("-wal", "-shm"):
        try:
            os.remove(str(path) + leftover)
        except OSError:
            pass
    path.write_bytes(bytes(data))
    store = Store(str(path))
    assert store.recovered_from and store.get_setting("kid", "blocked") is None
    store.close()
    assert len([f for f in os.listdir(tmp_path) if ".corrupt-" in f and not f.endswith(("-wal", "-shm"))]) == 2


def test_top_apps_stays_cheap_with_many_names():
    store = Store(":memory:")
    day = date(2026, 10, 13)
    for i in range(2000):
        store.add_app("kid", day, f"app:{i}", float(i))
    started = time.perf_counter()
    for _ in range(200):
        top = store.top_apps("kid", day, 10)
    per_call = (time.perf_counter() - started) / 200
    assert top[0] == ("app:1999", 1999.0) and len(top) == 10
    assert per_call < 0.01                                           # measured: well under a millisecond


def test_agent_report_url_reaches_the_rules():
    classifier = ActivityClassifier(ActivityConfig())
    report = AgentReport(session_id=2, ts=0.0, foreground=ForegroundInfo(name="chrome.exe", title="Doc"),
                         url="https://docs.google.com/document/d/1", domain="google.com")
    activity = classifier.classify(report)
    assert activity.domain == "google.com" and rules._activity_hosts(activity) == ["google.com", "docs.google.com"]
