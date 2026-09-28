"""Per-app / per-website limits, blocklists and focus (homework) mode.

The activity tracker already knows which app and which site a user is on;
this module turns that into rules:

* **limits** - "30 min of YouTube per weekday", "Roblox only 16:00-19:00";
* **blocklists** - "never TikTok";
* **focus mode** - for a period only allow-listed apps/sites are permitted.

Like the TimeKeeper this is pure logic: it accounts per-rule usage in the
store and returns *decisions* (close the app / close the tab / warn) that the
orchestrator delivers to the per-session agent.  The agent has no say in the
matter; if it is killed, the user simply gets logged off by the normal time
limit instead.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from .config import AppRule, FocusConfig, UserPolicy
from .models import Activity
from .store import Store
from .util import window_status

log = logging.getLogger(__name__)

NOTIFY_REPEAT_SECONDS = 30.0

# Never terminate these even when a rule or focus mode says "close": killing the
# shell or the agent's own interpreter would break the session, not enforce a rule.
NEVER_CLOSE = {
    "explorer.exe", "winlogon.exe", "csrss.exe", "dwm.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe",
    "runtimebroker.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "applicationframehost.exe", "lockapp.exe", "textinputhost.exe",
    "systemsettings.exe", "taskmgr.exe", "consent.exe", "credentialuihost.exe", "securityhealthsystray.exe",
    "pythonw.exe", "python.exe", "pclink.exe", "pclink-agent.exe", "logonui.exe", "userinit.exe",
}


def _norm_exe(name: str) -> str:
    n = (name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def _site_matches(domain: Optional[str], sites: list[str]) -> bool:
    if not domain:
        return False
    d = domain.lower()
    for site in sites:
        s = site.lower().lstrip("*.")
        if d == s or d.endswith("." + s):
            return True
    return False


def rule_matches(rule: AppRule, activity: Activity) -> bool:
    if not activity.is_active:
        return False
    app = _norm_exe(activity.app or "")
    if app and rule.apps and app in {_norm_exe(a) for a in rule.apps}:
        return True
    if rule.sites and _site_matches(activity.domain, rule.sites):
        return True
    if rule.categories and activity.category in {c.lower() for c in rule.categories}:
        return True
    return False


def focus_allows(focus: FocusConfig, activity: Activity, extra_allow: Optional[list[str]] = None) -> bool:
    """Is this activity permitted while focus mode is on?"""
    if not activity.is_active:
        return True
    app = _norm_exe(activity.app or "")
    if app + ".exe" in NEVER_CLOSE or app in {_norm_exe(a) for a in NEVER_CLOSE}:
        return True
    allow_apps = {_norm_exe(a) for a in focus.allow_apps}
    allow_sites = list(focus.allow_sites)
    for item in extra_allow or []:
        item = str(item).lower()
        if item.endswith(".exe") or ("." not in item):
            allow_apps.add(_norm_exe(item))
        else:
            allow_sites.append(item)
    if activity.domain is not None or activity.category not in ("gaming", "app"):
        # a browser: judge by site / category, not by the browser executable
        if _site_matches(activity.domain, allow_sites):
            return True
        if activity.category in {c.lower() for c in focus.allow_categories}:
            return True
        return app in allow_apps and not activity.domain  # browser itself allow-listed and no site known
    if app in allow_apps:
        return True
    return activity.category in {c.lower() for c in focus.allow_categories}


@dataclass
class RuleStatus:
    slug: str
    name: str
    enabled: bool
    matched: bool
    blocked: bool
    limit_minutes: Optional[int]
    used_seconds: float
    remaining_seconds: Optional[float]
    in_hours: bool
    hours_remaining_seconds: Optional[float]
    exceeded: bool
    reason: str
    action: str
    icon: str

    @property
    def remaining_minutes(self) -> Optional[float]:
        if self.remaining_seconds is None:
            return None
        return round(max(0.0, self.remaining_seconds) / 60, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "enabled": self.enabled, "active_now": self.matched, "blocked": self.blocked,
            "limit_minutes": self.limit_minutes, "used_minutes": round(self.used_seconds / 60, 1),
            "remaining_minutes": self.remaining_minutes, "in_allowed_hours": self.in_hours,
            "exceeded": self.exceeded, "reason": self.reason or None, "action": self.action,
        }

    @property
    def state(self) -> str:
        """Single-word state for the HA sensor."""
        if not self.enabled:
            return "off"
        if self.blocked:
            return "blocked"
        if not self.in_hours:
            return "outside_hours"
        if self.remaining_seconds is not None and self.remaining_seconds <= 0:
            return "used_up"
        return "ok"


@dataclass
class RuleDecision:
    """What the orchestrator must do right now for one user."""

    kind: str                 # close | warn
    source: str               # rule | focus
    slug: str
    title: str
    message: str
    level: str = "warning"
    notify: bool = True       # False -> repeat the close silently
    close_tab: bool = False   # browser tab instead of the whole process
    app: Optional[str] = None
    minutes_left: Optional[int] = None


@dataclass
class _UserRt:
    day: Optional[date] = None
    warned: dict[str, set] = field(default_factory=dict)      # slug -> thresholds already warned
    last_notify: dict[str, float] = field(default_factory=dict)


class RuleEngine:
    def __init__(self, store: Store):
        self.store = store
        self._rt: dict[str, _UserRt] = {}

    # -- HA-controlled settings ----------------------------------------------------
    def rule_enabled(self, user: str, slug: str) -> bool:
        return bool(self.store.get_setting(user, f"rule_enabled:{slug}", True))

    def set_rule_enabled(self, user: str, slug: str, enabled: bool) -> None:
        self.store.set_setting(user, f"rule_enabled:{slug}", bool(enabled))
        self.store.log_event(user, "rule_enabled", f"{slug}={bool(enabled)}")
        if enabled:
            rt = self._rt.get(user)
            if rt:
                rt.last_notify.pop(slug, None)

    def rule_limit_today(self, user: str, rule: AppRule, day: date) -> Optional[int]:
        override = self.store.get_setting(user, f"rule_limit:{rule.slug}:{day.isoformat()}")
        if override is not None:
            return None if int(override) < 0 else int(override)
        return rule.daily_limits.get(day.weekday())

    def set_rule_limit_today(self, user: str, slug: str, day: date, minutes: Optional[int]) -> None:
        key = f"rule_limit:{slug}:{day.isoformat()}"
        if minutes is None:
            self.store.delete_setting(user, key)
        else:
            self.store.set_setting(user, key, int(minutes))
        self.store.log_event(user, "rule_limit_today", f"{slug}={minutes}")

    # -- status --------------------------------------------------------------------
    def rule_status(self, user: str, rule: AppRule, now: datetime, activity: Optional[Activity]) -> RuleStatus:
        day = now.date()
        enabled = self.rule_enabled(user, rule.slug)
        matched = bool(activity and rule_matches(rule, activity))
        used = self.store.rule_seconds(user, day, rule.slug)
        limit = self.rule_limit_today(user, rule, day)
        remaining = None if limit is None else limit * 60 - used
        weekday = day.weekday()
        windows = rule.allowed_windows.get(weekday, [])
        minute = now.hour * 60 + now.minute + now.second / 60
        in_hours, hours_left = window_status(minute, windows, rule.allowed_windows.get((weekday + 1) % 7, []))
        reason = ""
        exceeded = False
        if enabled:
            if rule.blocked:
                exceeded, reason = True, "blocked"
            elif not in_hours:
                exceeded, reason = True, "outside allowed hours"
            elif remaining is not None and remaining <= 0:
                exceeded, reason = True, "daily limit used up"
        return RuleStatus(
            slug=rule.slug, name=rule.name, enabled=enabled, matched=matched, blocked=rule.blocked,
            limit_minutes=limit, used_seconds=used, remaining_seconds=remaining, in_hours=in_hours,
            hours_remaining_seconds=None if hours_left is None else hours_left * 60,
            exceeded=exceeded, reason=reason, action=rule.action, icon=rule.icon,
        )

    def statuses(self, user: str, policy: UserPolicy, now: datetime, activity: Optional[Activity]) -> list[RuleStatus]:
        return [self.rule_status(user, r, now, activity) for r in policy.rules]

    # -- accounting -------------------------------------------------------------------
    def account(self, user: str, policy: UserPolicy, day: date, activity: Activity, dt: float) -> list[str]:
        """Add ``dt`` seconds to every rule the activity matches; returns their slugs."""
        if dt <= 0 or not activity.is_active:
            return []
        hit = []
        for rule in policy.rules:
            if rule_matches(rule, activity):
                self.store.add_rule(user, day, rule.slug, dt)
                hit.append(rule.slug)
        return hit

    # -- decisions -------------------------------------------------------------------
    def tick(self, user: str, policy: UserPolicy, now: datetime, activity: Optional[Activity], *,
             focus_active: bool = False, focus_extra_allow: Optional[list[str]] = None,
             enforcing: bool = True) -> list[RuleDecision]:
        """Return the decisions for this user for this tick (possibly none)."""
        rt = self._rt.get(user)
        today = now.date()
        if rt is None or rt.day != today:
            rt = _UserRt(day=today)
            self._rt[user] = rt
        if activity is None or not activity.is_active or not enforcing:
            return []
        now_ts = now.timestamp()
        decisions: list[RuleDecision] = []
        is_browser_site = activity.domain is not None
        app_label = (activity.app or "").rsplit(".exe", 1)[0] or activity.app or "this app"

        for rule in policy.rules:
            st = self.rule_status(user, rule, now, activity)
            if not st.enabled or not st.matched:
                continue
            if st.exceeded:
                notify = now_ts - rt.last_notify.get(rule.slug, -1e9) >= NOTIFY_REPEAT_SECONDS
                if notify:
                    rt.last_notify[rule.slug] = now_ts
                what = activity.domain or app_label
                if rule.blocked:
                    msg = rule.message or f"{what} is not allowed."
                elif not st.in_hours:
                    msg = rule.message or f"{what} is not allowed at this time of day."
                else:
                    msg = rule.message or f"Your daily time for {rule.name} ({st.limit_minutes} min) is used up."
                decisions.append(RuleDecision(
                    kind=rule.action, source="rule", slug=rule.slug, title=f"{rule.name}: not allowed now",
                    message=msg + (" It will be closed." if rule.action == "close" else ""),
                    level="critical" if rule.action == "close" else "warning", notify=notify,
                    close_tab=is_browser_site and bool(rule.sites or rule.categories) and _norm_exe(activity.app or "") not in {_norm_exe(a) for a in rule.apps},
                    app=activity.app, minutes_left=0,
                ))
                continue
            # approaching the limit -> warn once per threshold
            if st.remaining_seconds is not None and rule.warn_at_minutes:
                eff = st.remaining_seconds
                if st.hours_remaining_seconds is not None:
                    eff = min(eff, st.hours_remaining_seconds)
                applicable = {t for t in rule.warn_at_minutes if eff <= t * 60}
                warned = rt.warned.setdefault(rule.slug, set())
                warned &= applicable
                fresh = applicable - warned
                if fresh:
                    warned |= applicable
                    minutes_left = max(1, math.ceil(eff / 60))
                    decisions.append(RuleDecision(
                        kind="warn", source="rule", slug=rule.slug,
                        title=f"{rule.name}: {minutes_left} min left",
                        message=f"You have about {minutes_left} minute{'s' if minutes_left != 1 else ''} of {rule.name} left today.",
                        level="warning", app=activity.app, minutes_left=minutes_left,
                    ))

        if focus_active and not focus_allows(policy.focus, activity, focus_extra_allow):
            notify = now_ts - rt.last_notify.get("focus", -1e9) >= NOTIFY_REPEAT_SECONDS
            if notify:
                rt.last_notify["focus"] = now_ts
            decisions.append(RuleDecision(
                kind=policy.focus.action, source="focus", slug="focus", title="Focus mode",
                message=policy.focus.message + (" It will be closed." if policy.focus.action == "close" else ""),
                level="warning", notify=notify, close_tab=is_browser_site, app=activity.app,
            ))
        return decisions

    def reset(self, user: str) -> None:
        self._rt.pop(user, None)


def closable(app: Optional[str]) -> bool:
    return bool(app) and app.lower() not in NEVER_CLOSE
