from datetime import datetime

import yaml

from pclink.config import Config
from pclink.models import Activity
from pclink.rules import RuleEngine, focus_allows, rule_matches
from pclink.store import Store

YAML = """
time_limits:
  rules:
    - name: YouTube
      sites: [youtube.com, youtu.be]
      daily_limit_minutes: 30
      allowed_hours: 14:00-20:00
      warn_at_minutes: [5, 1]
    - name: TikTok
      sites: [tiktok.com]
      blocked: true
      message: "No TikTok on this PC."
    - name: Social
      categories: [social]
      daily_limit_minutes: 45
      action: warn
  users:
    - name: kevin
      daily_limit_minutes: 120
      rules:
        - name: Roblox
          apps: [RobloxPlayerBeta.exe]
          sites: [roblox.com]
          daily_limit_minutes: {weekdays: 30, weekend: 90}
      focus:
        allow_apps: [winword.exe]
        allow_sites: [khanacademy.org]
        allow_categories: [education]
"""


def make():
    cfg = Config.from_dict(yaml.safe_load(YAML))
    store = Store(":memory:")
    return cfg, cfg.user_policy("kevin"), RuleEngine(store), store


def browsing(domain, category="browsing", app="chrome.exe"):
    return Activity(category=category, app=app, domain=domain)


def test_rule_matching_by_app_site_and_category():
    cfg, policy, eng, store = make()
    rules = {r.slug: r for r in policy.rules}
    assert rule_matches(rules["youtube"], browsing("www.youtube.com"))  # host-grouped domains match by suffix
    assert rule_matches(rules["youtube"], browsing("youtube.com"))
    assert not rule_matches(rules["youtube"], browsing("notyoutube.com"))
    assert rule_matches(rules["youtube"], browsing("m.youtube.com"))
    assert rule_matches(rules["roblox"], Activity(category="gaming", app="robloxplayerbeta.exe"))
    assert rule_matches(rules["roblox"], browsing("roblox.com"))
    assert rule_matches(rules["social"], browsing("reddit.com", category="social"))
    assert not rule_matches(rules["social"], browsing("reddit.com"))
    assert not rule_matches(rules["youtube"], Activity(category="idle"))


def test_shared_rules_merge_with_user_rules():
    cfg, policy, eng, store = make()
    assert [r.name for r in policy.rules] == ["YouTube", "TikTok", "Social", "Roblox"]
    assert policy.rules[3].daily_limits[5] == 90


def test_accounting_and_limit_enforcement():
    cfg, policy, eng, store = make()
    now = datetime(2026, 9, 28, 15, 0, 0)  # Monday 15:00, inside YouTube hours
    act = browsing("youtube.com", category="video")
    eng.account("kevin", policy, now.date(), act, 25 * 60)
    st = {s.slug: s for s in eng.statuses("kevin", policy, now, act)}
    assert st["youtube"].used_seconds == 25 * 60 and st["youtube"].remaining_minutes == 5
    assert st["youtube"].state == "ok" and st["youtube"].matched
    # 5-minute warning fires once
    decisions = eng.tick("kevin", policy, now, act)
    assert [d.kind for d in decisions] == ["warn"] and decisions[0].minutes_left == 5
    assert eng.tick("kevin", policy, now, act) == []
    # use it up -> close the tab, notify once per 30 s
    eng.account("kevin", policy, now.date(), act, 6 * 60)
    decisions = eng.tick("kevin", policy, now, act)
    assert len(decisions) == 1 and decisions[0].kind == "close" and decisions[0].close_tab and decisions[0].notify
    again = eng.tick("kevin", policy, now, act)
    assert again and again[0].kind == "close" and not again[0].notify
    st = {s.slug: s for s in eng.statuses("kevin", policy, now, act)}
    assert st["youtube"].state == "used_up"


def test_blocked_rule_and_outside_hours():
    cfg, policy, eng, store = make()
    now = datetime(2026, 9, 28, 15, 0, 0)
    d = eng.tick("kevin", policy, now, browsing("tiktok.com", category="social"))
    assert d and d[0].kind == "close" and "No TikTok" in d[0].message
    late = datetime(2026, 9, 28, 21, 0, 0)
    d = eng.tick("kevin", policy, late, browsing("youtube.com", category="video"))
    assert d and d[0].kind == "close" and "time of day" in d[0].message
    st = {s.slug: s for s in eng.statuses("kevin", policy, late, None)}
    assert st["youtube"].state == "outside_hours"


def test_rule_can_be_disabled_and_limit_overridden_from_ha():
    cfg, policy, eng, store = make()
    now = datetime(2026, 9, 28, 15, 0, 0)
    eng.set_rule_enabled("kevin", "tiktok", False)
    assert eng.tick("kevin", policy, now, browsing("tiktok.com", category="social")) == []
    eng.set_rule_limit_today("kevin", "youtube", now.date(), 60)
    st = {s.slug: s for s in eng.statuses("kevin", policy, now, None)}
    assert st["youtube"].limit_minutes == 60
    eng.set_rule_limit_today("kevin", "youtube", now.date(), None)
    st = {s.slug: s for s in eng.statuses("kevin", policy, now, None)}
    assert st["youtube"].limit_minutes == 30


def test_warn_only_rule_never_closes():
    cfg, policy, eng, store = make()
    now = datetime(2026, 9, 28, 15, 0, 0)
    act = browsing("reddit.com", category="social")
    eng.account("kevin", policy, now.date(), act, 46 * 60)
    d = eng.tick("kevin", policy, now, act)
    assert d and d[0].kind == "warn" and d[0].source == "rule"


def test_focus_mode_allowlist():
    cfg, policy, eng, store = make()
    now = datetime(2026, 9, 28, 15, 0, 0)
    assert focus_allows(policy.focus, Activity(category="app", app="winword.exe"))
    assert focus_allows(policy.focus, browsing("khanacademy.org", category="education"))
    assert focus_allows(policy.focus, browsing("wikipedia.org", category="education"))
    assert not focus_allows(policy.focus, browsing("youtube.com", category="video"))
    assert not focus_allows(policy.focus, Activity(category="gaming", app="game.exe"))
    assert focus_allows(policy.focus, Activity(category="app", app="explorer.exe"))  # shell is never closed
    assert focus_allows(policy.focus, browsing("docs.google.com"), extra_allow=["google.com"])
    d = eng.tick("kevin", policy, now, Activity(category="gaming", app="game.exe"), focus_active=True)
    assert d and d[0].source == "focus" and d[0].kind == "close" and not d[0].close_tab
    d = eng.tick("kevin", policy, now, browsing("youtube.com", category="video"), focus_active=True)
    assert d and d[0].source == "focus" and d[0].close_tab
    assert eng.tick("kevin", policy, now, Activity(category="app", app="winword.exe"), focus_active=True) == []
    assert eng.tick("kevin", policy, now, Activity(category="gaming", app="game.exe"), focus_active=True, enforcing=False) == []
