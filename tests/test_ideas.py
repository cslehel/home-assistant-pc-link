"""Day boundaries in a fixed time zone, rules edited from Home Assistant, weekly budgets per
rule, the read-only API token, display power and the signature check of an update."""
import shutil
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from pclink import codesign
from pclink import updater as up
from pclink.api import ApiServer
from pclink.codesign import PackageSignatures, Signature
from pclink.config import ApiConfig, Config, ConfigError, UpdatesConfig, parse_rule
from pclink.core import Orchestrator
from pclink.models import Activity
from pclink.rules import RuleEngine, rule_loosens
from pclink.store import Store
from pclink.util import make_clock

from .test_api import FakeBackend, call
from .test_orchestrator import FakeAgentServer, FakeApi, FakeBlocker, FakeSessions, attach, report, session
from .test_updater import release

BASE = """
device: {name: TestPC}
telemetry: {interval_seconds: 0}
time_limits:
  parent_pin: "4321"
  users:
    - name: kevin
      daily_limit_minutes: 120
      rules:
        - name: YouTube
          sites: [youtube.com]
          daily_limit_minutes: 30
          weekly_limit_minutes: 100
          allowed_hours: 14:00-20:00
    - name: dad
      unlimited: true
"""
NOW = datetime(2026, 9, 28, 15, 0, 0)   # a Monday


def make(text=BASE, agent=True, now=NOW):
    cfg = Config.from_dict(yaml.safe_load(text))
    cfg.api.enabled, cfg.api.push_interval_seconds = True, 0   # every tick pushes the state to FakeApi
    holder = {"now": now}
    sessions, api = FakeSessions(), FakeApi()
    agent_server = FakeAgentServer() if agent else None
    orch = Orchestrator(cfg, store=Store(":memory:"), sessions=sessions, blocker=FakeBlocker(), api=api,
                        clock=lambda: holder["now"], agent_server=agent_server)
    orch._holder = holder
    return orch, sessions, api, agent_server


def catalog_keys(orch):
    return {e["key"] for e in orch.api_entities()["entities"]}


def rule(**fields):
    return parse_rule({"name": "R", "sites": ["a.com"], **fields}, "test")


# -- 1. time zone --------------------------------------------------------------------------------

def test_time_zone_is_validated_and_drives_the_clock():
    assert Config.from_dict({}).time_limits.time_zone is None
    assert Config.from_dict({"time_limits": {"time_zone": " local "}}).time_limits.time_zone is None
    with pytest.raises(ConfigError, match="time_zone"):
        Config.from_dict({"time_limits": {"time_zone": "Mars/Olympus"}})
    local = make_clock(None)()          # the PC's local time - with its zone (DST-correct real seconds)
    assert local.tzinfo is not None and abs(local.timestamp() - datetime.now().timestamp()) < 5
    tokyo, honolulu = make_clock("Asia/Tokyo")(), make_clock("Pacific/Honolulu")()
    assert tokyo.utcoffset() == timedelta(hours=9) and honolulu.utcoffset() == timedelta(hours=-10)
    assert abs(tokyo.timestamp() - honolulu.timestamp()) < 5       # the same moment ...
    assert (tokyo.date() - honolulu.date()).days in (0, 1)         # ... on each zone's own calendar
    assert tokyo.replace(tzinfo=None) - honolulu.replace(tzinfo=None) > timedelta(hours=18)


def test_service_clock_follows_the_configured_zone_and_a_reload(monkeypatch):
    import pclink.core as core_mod
    monkeypatch.setattr(core_mod, "local_iana_zone", lambda: "Europe/Lisbon")
    cfg = Config.from_dict({"device": {"name": "TestPC"}, "time_limits": {"time_zone": "Asia/Tokyo"}})
    orch = Orchestrator(cfg, store=Store(":memory:"), sessions=FakeSessions(), blocker=FakeBlocker(), api=FakeApi())
    assert orch.clock().utcoffset() == timedelta(hours=9)
    orch.tick()   # the whole tick runs on zone-aware times
    assert orch.pc_payload([])["service_info"]["time_zone"] == "Asia/Tokyo"
    # without a configured zone: the one the PC had when the service first needed it (pinned)
    orch.reload_config(Config.from_dict({"device": {"name": "TestPC"}}))
    assert str(orch.clock().tzinfo) == "Europe/Lisbon"
    # and when the PC's zone cannot be told, local time as before - aware of the PC's own zone
    from pclink.util import LOCAL_TZ
    monkeypatch.setattr(core_mod, "local_iana_zone", lambda: None)
    orch = Orchestrator(Config.from_dict({"device": {"name": "TestPC"}}), store=Store(":memory:"),
                        sessions=FakeSessions(), blocker=FakeBlocker(), api=FakeApi())
    assert orch.clock().tzinfo is LOCAL_TZ


def test_usage_is_booked_on_the_configured_zones_day():
    # 23:30 UTC on the 28th is already the 29th in Tokyo: that is the day the minutes belong to
    tokyo = timezone(timedelta(hours=9))
    orch, sessions, api, _ = make(now=datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc).astimezone(tokyo))
    sessions.sessions = [session()]
    orch.tick()
    orch._holder["now"] += timedelta(seconds=30)
    orch.tick()
    assert orch.store.usage("kevin", date(2026, 9, 29)).active == pytest.approx(30)
    payload = api.users["kevin"]
    assert payload["date"] == "2026-09-29"
    orch.api_command({"action": "focus", "user": "kevin", "minutes": 10, "pin": "4321"})  # focus pauses the budget: PIN
    orch.tick()
    until = api.users["kevin"]["focus_details"]["until"]
    assert until.endswith("+09:00") and until.startswith("2026-09-29T08:40")


# -- 5. weekly budget per rule -------------------------------------------------------------------------

def test_rule_weekly_budget_counts_the_week_and_is_enforced():
    cfg = Config.from_dict(yaml.safe_load(BASE))
    policy, store = cfg.user_policy("kevin"), Store(":memory:")
    engine = RuleEngine(store, cfg.time_limits.week_start_index)
    yt = policy.rules[0]
    assert yt.weekly_limit_minutes == 100 and yt.has_weekly_limit
    watching = Activity(category="browsing", app="chrome.exe", domain="youtube.com")
    wednesday = NOW + timedelta(days=2)
    store.add_rule("kevin", NOW.date() - timedelta(days=1), "youtube", 50 * 60)   # Sunday: last week
    store.add_rule("kevin", NOW.date(), "youtube", 30 * 60)
    store.add_rule("kevin", NOW.date() + timedelta(days=1), "youtube", 30 * 60)
    store.add_rule("kevin", wednesday.date(), "youtube", 20 * 60)
    st = engine.rule_status("kevin", yt, wednesday, watching)
    assert st.weekly_used_seconds == 80 * 60 and st.weekly_remaining_minutes == 20 and st.remaining_minutes == 10
    assert st.state == "ok" and not st.exceeded
    assert engine.tick("kevin", policy, wednesday, watching) == []
    store.add_rule("kevin", wednesday.date(), "youtube", 6 * 60)   # 4 min left today, 14 this week
    warn = engine.tick("kevin", policy, wednesday, watching)[0]
    assert warn.kind == "warn" and warn.minutes_left == 4 and "today" in warn.message
    thursday = wednesday + timedelta(days=1)
    store.add_rule("kevin", thursday.date(), "youtube", 14 * 60)   # the week is used up, today is not
    st = engine.rule_status("kevin", yt, thursday, watching)
    assert st.exceeded and st.reason == "weekly limit used up" and st.state == "used_up" and st.remaining_minutes == 16
    decision = engine.tick("kevin", policy, thursday, watching)[0]
    assert decision.kind == "close" and "this week" in decision.message
    monday = NOW + timedelta(days=7)
    assert not engine.rule_status("kevin", yt, monday, watching).exceeded   # a new week
    # the week can start on another day
    assert RuleEngine(store, 6).week_start(wednesday.date()) == NOW.date() - timedelta(days=1)


def test_rule_weekly_limit_from_home_assistant_needs_the_pin_only_to_loosen():
    orch, sessions, api, _ = make()
    sessions.sessions = [session()]
    orch.tick()
    assert {"user_kevin_rule_youtube_weekly_limit", "user_kevin_rule_youtube_weekly_remaining"} <= catalog_keys(orch)
    assert orch.dispatch("user_kevin_rule_youtube_weekly_limit", "60") == {"ok": True}
    assert orch.dispatch("user_kevin_rule_youtube_weekly_limit", "90")["error"] == "parent pin required"
    assert orch.dispatch("user_kevin_rule_youtube_weekly_limit", "0")["error"] == "parent pin required"   # 0 = no limit
    assert orch.api_command({"action": "rule_limit", "user": "kevin", "rule": "YouTube", "scope": "weekly",
                             "minutes": 90, "pin": "4321"})["ok"]
    orch.tick()
    rules = api.users["kevin"]["rules"]
    assert rules["youtube"]["weekly_limit"] == 90 and rules["youtube"]["weekly_remaining_minutes"] == 90
    # without minutes: back to the configured 100, which is more than 90
    back = {"action": "rule_limit", "user": "kevin", "rule": "YouTube", "scope": "weekly"}
    orch.api_command({"action": "pin_lock"})   # a PIN sent with a command unlocks for a while
    assert orch.api_command(back)["ok"] is False
    assert orch.api_command({**back, "pin": "4321"})["ok"]
    assert orch.rules.rule_weekly_limit("kevin", orch.cfg.user_policy("kevin").rules[0]) == 100
    with pytest.raises(ConfigError):
        rule(weekly_limit_minutes=0)
    assert rule(weekly_limit_minutes="5h").weekly_limit_minutes == 300   # enough on its own


# -- 2. rules edited from Home Assistant ---------------------------------------------------------------

def test_rule_loosens():
    base = rule(daily_limit_minutes={"weekdays": 30, "weekend": 60}, weekly_limit_minutes=200, allowed_hours="14:00-20:00")
    same = dict(daily_limit_minutes={"weekdays": 30, "weekend": 60}, weekly_limit_minutes=200, allowed_hours="14:00-20:00")
    assert not rule_loosens(base, rule(**same))
    assert not rule_loosens(base, rule(**{**same, "daily_limit_minutes": 20}))
    assert not rule_loosens(base, rule(**{**same, "allowed_hours": "15:00-19:00"}))
    assert not rule_loosens(base, rule(**{**same, "allowed_hours": "14:00-17:00, 17:00-20:00"}))
    assert not rule_loosens(base, rule(**same, sites=["a.com", "b.com"]))
    assert not rule_loosens(base, rule(blocked=True))
    assert rule_loosens(base, rule(**{**same, "daily_limit_minutes": {"weekdays": 30, "weekend": 61}}))
    assert rule_loosens(base, rule(**{**same, "daily_limit_minutes": None}))
    assert rule_loosens(base, rule(**{**same, "weekly_limit_minutes": None}))
    assert rule_loosens(base, rule(**{**same, "allowed_hours": "13:59-20:00"}))
    assert rule_loosens(base, rule(**{**same, "allowed_hours": None}))
    assert rule_loosens(base, rule(**same, action="warn"))
    assert rule_loosens(base, rule(**same, sites=["b.com"]))
    assert rule_loosens(rule(blocked=True), rule(daily_limit_minutes=1))
    assert rule_loosens(rule(allowed_hours="never"), rule(allowed_hours="10:00-11:00"))
    assert not rule_loosens(rule(allowed_hours="10:00-11:00"), rule(allowed_hours="never"))


def test_rules_are_created_changed_and_removed_from_home_assistant():
    orch, sessions, api, agent = make()
    sessions.sessions = [session()]
    agent.connected.add(2)
    orch.tick()
    cmd = orch.api_command
    # a new rule only restricts: no PIN
    made = cmd({"action": "rule_set", "user": "kevin", "rule": "Roblox", "apps": "RobloxPlayerBeta.exe",
                "daily_limit_minutes": {"weekdays": 20, "weekend": 45}, "allowed_hours": "16:00-19:00"})
    assert made["ok"] and made["created"] and made["rule"] == "roblox"
    assert ("user/kevin", "rules_changed") in api.events
    orch.tick()
    assert {"user_kevin_rule_roblox_enabled", "user_kevin_rule_roblox_limit_today"} <= catalog_keys(orch)
    info = api.users["kevin"]["rules"]["roblox"]
    assert info["limit_minutes"] == 20 and info["source"] == "Home Assistant"
    assert info["definition"]["allowed_hours"] == "16:00-19:00" and info["definition"]["apps"] == ["robloxplayerbeta.exe"]
    assert [e["key"] for e in orch.api_entities()["entities"] if e["key"].endswith("rule_roblox")]
    # it is enforced like a configured rule
    orch.store.add_rule("kevin", NOW.date(), "roblox", 20 * 60)
    attach(orch, sessions, 2, report(name="RobloxPlayerBeta.exe"))
    orch.tick()
    assert agent.messages("close")
    # tightening is free, loosening needs the PIN; only the given fields change
    assert cmd({"action": "rule_set", "user": "kevin", "rule": "roblox", "daily_limit_minutes": 10})["ok"]
    assert cmd({"action": "rule_set", "user": "kevin", "rule": "Roblox", "daily_limit_minutes": 60})["error"] == "parent pin required"
    changed = cmd({"action": "rule_set", "user": "kevin", "rule": "Roblox", "daily_limit_minutes": 60, "pin": "4321"})
    assert changed["ok"] and not changed["created"] and changed["definition"]["allowed_hours"] == "16:00-19:00"
    cmd({"action": "pin_lock"})   # a PIN sent with a command unlocks for a while
    # a configured rule can be changed too, and gets its configuration back
    assert cmd({"action": "rule_set", "user": "kevin", "rule": "YouTube", "allowed_hours": "all"})["error"] == "parent pin required"
    assert cmd({"action": "rule_set", "user": "kevin", "rule": "YouTube", "allowed_hours": "15:00-16:00", "weekly_limit_minutes": "none",
                "pin": "4321"})["ok"]
    orch.tick()
    yt = api.users["kevin"]["rules"]["youtube"]
    assert yt["source"] == "config, changed in Home Assistant" and yt["weekly_limit_minutes"] is None and yt["limit_minutes"] == 30
    assert "user_kevin_rule_youtube_weekly_limit" not in catalog_keys(orch)   # gone: no weekly budget any more
    cmd({"action": "pin_lock"})
    assert cmd({"action": "rule_remove", "user": "kevin", "rule": "YouTube"})["error"] == "parent pin required"   # 14-20 h again
    restored = cmd({"action": "rule_remove", "user": "kevin", "rule": "YouTube", "pin": "4321"})
    assert restored["ok"] and restored["restored"]
    assert cmd({"action": "rule_remove", "user": "kevin", "rule": "YouTube", "pin": "4321"})["error"].startswith("YouTube comes from config.yaml")
    # removing a rule made here lifts a restriction
    cmd({"action": "pin_lock"})
    assert cmd({"action": "rule_remove", "user": "kevin", "rule": "Roblox"})["error"] == "parent pin required"
    assert cmd({"action": "rule_remove", "user": "kevin", "rule": "Roblox", "pin": "4321"})["ok"]
    orch.tick()
    assert "roblox" not in api.users["kevin"]["rules"]
    assert "user_kevin_rule_roblox_enabled" not in catalog_keys(orch)
    assert "user_kevin_rule_youtube_weekly_limit" in catalog_keys(orch)


def test_rule_edits_are_validated_and_survive_a_restart():
    orch, sessions, api, _ = make()
    cmd = orch.api_command
    assert cmd({"action": "rule_set", "user": "kevin", "rule": "Nothing"})["ok"] is False          # matches nothing
    assert cmd({"action": "rule_set", "user": "kevin", "rule": "X", "sites": ["x.com"]})["ok"] is False   # restricts nothing
    assert "invalid time" in cmd({"action": "rule_set", "user": "kevin", "rule": "X", "sites": "x.com", "allowed_hours": "25:00-26:00"})["error"]
    assert cmd({"action": "rule_set", "user": "kevin", "rule": "X", "sites": "x.com", "rule_action": "warn", "blocked": True, "icon": "mdi:x"})["ok"]
    assert "close or warn" in cmd({"action": "rule_set", "user": "kevin", "rule": "X", "rule_action": "explode"})["error"]
    assert cmd({"action": "rule_set", "user": "dad", "rule": "X", "sites": "x.com", "blocked": True})["ok"] is False
    assert cmd({"action": "rule_set", "user": "kevin", "sites": "x.com", "blocked": True})["ok"] is False
    assert cmd({"action": "rule_remove", "user": "kevin", "rule": "nope"})["error"] == "unknown rule nope"
    # a second service on the same database sees the rule
    again = Orchestrator(orch.cfg, store=orch.store, sessions=FakeSessions(), blocker=FakeBlocker(), api=FakeApi(),
                         clock=lambda: NOW)
    assert [r.slug for r in again.user_rules_policy("kevin").rules] == ["youtube", "x"]
    assert again.user_rules_policy("kevin").rules[1].blocked and again.user_rules_policy("kevin").rules[1].action == "warn"
    assert again.user_rules_policy("dad").rules == []


# -- 8. display power ------------------------------------------------------------------------------------

def test_display_switch_and_command():
    orch, sessions, api, agent = make()
    sessions.sessions = [session(), session("dad", sid=3)]
    agent.connected.update({2, 3})
    attach(orch, sessions, 2, report(display_state="off"))
    attach(orch, sessions, 3, report(sid=3))
    orch.tick()
    assert {"user_kevin_display", "user_dad_display"} <= catalog_keys(orch)
    assert api.users["kevin"]["display_on"] is False
    assert api.users["dad"]["display_on"] is None      # Windows did not say
    assert orch.dispatch("user_kevin_display", "ON") == {"ok": True, "sessions": 1, "error": None}
    assert agent.messages("display") == [(2, {"type": "display", "on": True})]
    assert orch.api_command({"action": "display", "on": False})["sessions"] == 2   # no user: every seat
    assert orch.api_command({"action": "display", "user": "dad", "on": False})["sessions"] == 1
    agent.connected.clear()
    assert orch.api_command({"action": "display", "user": "kevin", "on": False})["ok"] is False
    orch2, sessions2, api2, _ = make(BASE + "agent: {display_control: false}\n")
    sessions2.sessions = [session()]
    orch2.tick()
    assert "user_kevin_display" not in catalog_keys(orch2)
    assert "switched off" in orch2.api_command({"action": "display", "user": "kevin", "on": False})["error"]


def test_agent_display_helper_is_harmless_off_windows():
    from pclink.agent import win_activity

    if sys.platform != "win32":
        assert win_activity.set_display_power(False) is False and win_activity.set_display_power(True) is False


# -- 4. read-only API token --------------------------------------------------------------------------------

def test_read_token_ends_open_reads_on_the_pc_and_never_writes():
    backend = FakeBackend()
    srv = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=0, token="s3cret", read_token="look"), backend,
                    html="<html><body>setup</body></html>")
    srv.start()
    try:
        for path in ("/api/health", "/api/status", "/api/users", "/api/users/kevin", "/api/entities", "/api/events", "/api/ws"):
            assert call(srv, "GET", path)[0] == 401, path
            assert call(srv, "GET", path, token="wrong")[0] == 401, path
        assert "setup" in call(srv, "GET", "/")[1]                      # the page itself: it asks for the token
        assert call(srv, "GET", "/api/users", token="look")[1]["kevin"]["managed"]
        assert call(srv, "GET", "/api/status?token=look")[0] == 200
        assert call(srv, "GET", "/api/status", token="s3cret")[0] == 200
        assert call(srv, "POST", "/api/command", {"action": "add_minutes", "user": "kevin"}, token="look")[0] == 401
        assert call(srv, "GET", "/api/config", token="look")[0] == 401
        assert call(srv, "GET", "/api/users/kevin/screenshot", token="look")[0] == 401
        assert call(srv, "POST", "/api/reload", token="look")[0] == 401
        assert not backend.commands
        assert call(srv, "POST", "/api/command", {"action": "add_minutes", "user": "kevin"}, token="s3cret")[0] == 200
    finally:
        srv.stop()


def test_read_token_configuration():
    cfg = Config.from_dict({"api": {"enabled": True, "token": "a", "read_token": 12345}})
    assert cfg.api.read_token == "12345"
    with pytest.raises(ConfigError, match="read_token"):
        Config.from_dict({"api": {"token": "same", "read_token": "same"}})
    from pclink.core import _restart_sections
    # the tokens are handed to the running API server on a reload: no restart needed for them
    assert _restart_sections(Config.from_dict({}), Config.from_dict({"api": {"read_token": "x"}})) == []


# -- 3. code signature of an update --------------------------------------------------------------------------

ACME = dict(thumbprint="AA" * 20, subject="CN=Acme Ltd, O=Acme, C=HU", root="DD" * 20, ca="CA" * 20)   # ca: the issuing CA
EVIL = dict(thumbprint="BB" * 20, subject="CN=Somebody Else, C=XX", root="EE" * 20)


def package(signer=None, catalog=True, installed=None, **overrides):
    """What ``read_package`` would report: every file signed by ``signer`` (None: unsigned)."""
    sig = Signature("valid", **signer) if signer else Signature("unsigned")
    files = {name: sig for name in codesign.EXECUTABLES + (codesign.INSTALLER,)}
    if catalog:
        files[codesign.CATALOG] = sig
    files.update(overrides)
    return PackageSignatures(files=files, catalog_matches=(True if catalog and signer else None),
                             installed=Signature("valid", **installed) if installed else Signature("unsigned"))


def test_signature_policy():
    judge = codesign.judge
    # nothing signed, nothing demanded: as before
    assert judge(package(catalog=False)) == (True, "not signed (no signature is required)")
    assert judge(package(ACME))[0] and "Acme Ltd" in judge(package(ACME))[1]
    assert not judge(PackageSignatures())[0]   # could not be read at all: that is a "no" (tests/test_fix_security.py)
    # a broken signature or a package that differs from its catalog is refused whatever the settings
    ok, why = judge(package(ACME, **{"pclink.exe": Signature("invalid", detail="HashMismatch")}))
    assert not ok and "pclink.exe is not valid" in why
    tampered = package(ACME)
    tampered.catalog_matches = False
    assert not judge(tampered)[0] and "catalog" in judge(tampered)[1]
    # a signed copy stays with its publisher (same certificate, or a renewed one: same subject, issuing CA and root)
    assert judge(package(ACME, installed=ACME))[0]
    assert judge(package({**ACME, "thumbprint": "CC" * 20}, installed=ACME))[0]
    assert "is not signed" in judge(package(installed=ACME))[1]
    assert "Somebody Else" in judge(package(EVIL, installed=ACME))[1]
    assert "no signed catalog" in judge(package(ACME, catalog=False, installed=ACME))[1]
    unchecked = package(ACME, installed=ACME)
    unchecked.catalog_matches = None
    assert not judge(unchecked)[0]
    assert "could not be checked" in judge(package(ACME, installed=ACME, **{"pclink-agent.exe": Signature()}))[1]
    one_swapped = package(ACME, installed=ACME, **{"pclink-agent.exe": Signature("valid", **EVIL)})
    assert not judge(one_swapped)[0] and "pclink-agent.exe" in judge(one_swapped)[1]
    # the installer script runs as SYSTEM: it needs the publisher's signature like the executables
    assert "install.ps1 is not signed" in judge(package(ACME, installed=ACME, **{"install.ps1": Signature("unsigned")}))[1]
    assert "Somebody Else" in judge(package(ACME, installed=ACME, **{"install.ps1": Signature("valid", **EVIL)}))[1]
    assert not judge(package(**{"install.ps1": Signature("invalid", detail="HashMismatch")}))[0]   # even with nothing demanded
    without = package(ACME, installed=ACME)
    del without.files["install.ps1"]
    assert "install.ps1 is missing" in judge(without)[1]
    # require_signature: an unsigned copy no longer takes unsigned updates
    assert not judge(package(), require=True)[0] and judge(package(EVIL), require=True)[0]
    # trusted_signers pins the certificate, also against the installed copy's own publisher
    assert judge(package(ACME), trusted=[ACME["thumbprint"].lower()])[0]
    assert not judge(package(EVIL), trusted=[ACME["thumbprint"]])[0]
    assert not judge(package(ACME, installed=ACME), trusted=[EVIL["thumbprint"]])[0]
    assert not judge(package({**ACME, "thumbprint": "CC" * 20}, installed=ACME), trusted=[ACME["thumbprint"]])[0]


def test_update_with_a_wrong_signature_is_not_installed(tmp_path, monkeypatch):
    ran = []

    def fake_download(asset, target):
        import zipfile
        with zipfile.ZipFile(target, "w") as zf:
            for name in ("install.ps1", "pclink.exe", "pclink-agent.exe"):
                zf.writestr(f"PCLink/{name}", "x")
            zf.writestr("PCLink/VERSION", "0.4.0\n")   # the package says which version it is

    def updater(found, **cfg):
        upd = up.SelfUpdate(UpdatesConfig(**cfg), str(tmp_path / "update"), installed="0.3.0", can_install=True,
                            fetch=lambda url: release(), signatures=lambda folder: found)
        monkeypatch.setattr(upd, "_download", fake_download)
        monkeypatch.setattr(upd, "_run_installer", lambda installer, stage: ran.append(installer))
        upd._check()
        upd._install()
        return upd.snapshot()["pclink_update_info"]

    info = updater(package(EVIL, installed=ACME))
    assert info["status"] == "error" and "Somebody Else" in info["error"] and info["signature"] == info["error"].split("failed: ", 1)[1]
    assert not ran and not (tmp_path / "update").exists()
    assert updater(package(), require_signature=True)["status"] == "error" and not ran
    info = updater(package(ACME, installed=ACME))
    assert info["status"] == "installing" and "Acme Ltd" in info["signature"] and len(ran) == 1
    assert updater(package(catalog=False))["status"] == "installing" and len(ran) == 2


def test_trusted_signers_configuration():
    cfg = Config.from_dict({"updates": {"trusted_signers": "aa:bb " + "cc" * 18, "require_signature": True}})
    assert cfg.updates.trusted_signers == ["AABB" + "CC" * 18] and cfg.updates.require_signature
    with pytest.raises(ConfigError, match="thumbprint"):
        Config.from_dict({"updates": {"trusted_signers": ["not a thumbprint"]}})


@pytest.mark.skipif(sys.platform != "win32", reason="reads Authenticode signatures with Windows' own tools")
def test_real_signatures_are_read(tmp_path):
    import os

    notepad = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")
    if not os.path.isfile(notepad):
        pytest.skip("no notepad.exe to read a signature from")
    folder = tmp_path / "PCLink"
    folder.mkdir()
    shutil.copy(notepad, folder / "pclink.exe")           # signed by Microsoft
    (folder / "pclink-agent.exe").write_bytes(b"MZ not a real program")
    (folder / "install.ps1").write_text("Write-Host 'hello'\n", encoding="utf-8")
    found = codesign.read_package(str(folder), notepad)
    assert found.installed.valid and "Microsoft" in found.installed.signer and len(found.installed.thumbprint) == 40
    assert found.files["pclink.exe"].valid and found.files["pclink.exe"].thumbprint == found.installed.thumbprint
    assert not found.files["pclink-agent.exe"].valid and codesign.CATALOG not in found.files
    assert found.catalog_matches is None and found.files["install.ps1"].status == "unsigned"
    ok, why = codesign.judge(found)   # the installed copy is signed, the "update" is not signed throughout
    assert not ok and "pclink-agent.exe" in why


# -- shared budgets: Home Assistant passes on the other PCs' usage ---------------------

SHARED = BASE.replace("time_limits:\n", "time_limits:\n  shared_budget: true\n")


def test_usage_of_other_pcs_from_home_assistant_counts_against_a_shared_budget():
    orch, sessions, api, _ = make(SHARED)
    sessions.sessions = [session()]
    orch.tick()
    assert orch.pc_payload([])["shared_budget"] is True
    assert orch.timekeeper.status("kevin", NOW).remaining_minutes == 120
    today, sunday = NOW.date().isoformat(), (NOW.date() - timedelta(days=1)).isoformat()
    peers = {"den": {"name": "Den", "users": {"Kevin": {"date": today, "used_minutes": 50, "status": "active",
                                                         "history": [{"date": sunday, "active_minutes": 40}]}}},
             "testpc": {"users": {"kevin": {"date": today, "used_minutes": 999}}},       # this PC itself: ignored
             "attic": "nonsense"}
    assert orch.api_command({"action": "peer_usage", "peers": peers}) == {"ok": True, "peers": 1, "shared_budget": True}
    st = orch.timekeeper.status("kevin", NOW)
    assert st.remaining_minutes == 70 and st.external_used_seconds == 50 * 60
    orch.tick()
    assert api.users["kevin"]["weekly_details"]["other_pcs_today_minutes"] == 50
    assert orch.fleet.peers()["den"].name == "Den"
    # merged, never replaced: a message that does not mention a PC or a user takes nothing back
    assert orch.api_command({"action": "peer_usage", "peers": {}})["ok"]
    assert orch.api_command({"action": "peer_usage", "peers": {"den": {"users": {}}}})["ok"]
    assert orch.timekeeper.status("kevin", NOW).remaining_minutes == 70
    assert orch.api_command({"action": "peer_usage", "peers": {"den": {"users": {"kevin": {"date": today, "used_minutes": 60}}}}})["ok"]
    assert orch.timekeeper.status("kevin", NOW).remaining_minutes == 60
    # bogus figures are no credit: what another PC reported for a day never goes down again
    orch.api_command({"action": "peer_usage", "peers": {"den": {"users": {"kevin": {"date": today, "used_minutes": -500}}}}})
    assert orch.timekeeper.status("kevin", NOW).remaining_minutes == 60
    assert orch.api_command({"action": "peer_usage"})["ok"] is False


def test_shared_budget_needs_the_api():
    def service(text):
        return Orchestrator(Config.from_dict(yaml.safe_load(text)), store=Store(":memory:"), sessions=FakeSessions(),
                            blocker=FakeBlocker(), clock=lambda: NOW)

    api_only = service(SHARED + "api: {enabled: true, token: t}\n")
    assert api_only.fleet is not None and api_only.pc_payload([])["shared_budget"] is True
    assert api_only.fleet.ready() is False            # nothing known yet: no rollover is worked out on a guess
    api_only.api_command({"action": "peer_usage", "peers": {}})
    assert api_only.fleet.ready() is True             # Home Assistant has reported (there are no other PCs)
    neither = service(SHARED)
    assert neither.fleet is None and neither.pc_payload([])["shared_budget"] is False
    assert neither.api_command({"action": "peer_usage", "peers": {}}) == {"ok": False, "error": "fleet.enabled is false"}
