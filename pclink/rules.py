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
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .config import AppRule, FocusConfig, UserPolicy
from .models import Activity
from .store import Store
from .util import NO_LIMIT, domain_from_url, window_status, windows_cover

log = logging.getLogger(__name__)

NOTIFY_REPEAT_SECONDS = 30.0

# Never terminate these even when a rule or focus mode says "close": killing the
# shell would break the session, not enforce a rule.  Only Windows' own shell and
# system processes are listed.  The agent is *not* protected by its name (anything
# can be called python.exe or pclink-agent.exe): it never closes its own process,
# and the orchestrator tells RuleEngine.tick when the agent's own window is in
# front (``agent_foreground``).
NEVER_CLOSE = {
    "explorer.exe", "winlogon.exe", "csrss.exe", "dwm.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe",
    "runtimebroker.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "applicationframehost.exe", "lockapp.exe", "textinputhost.exe",
    "systemsettings.exe", "consent.exe", "credentialuihost.exe", "securityhealthsystray.exe",
    "logonui.exe", "userinit.exe",
}
_NEVER_CLOSE_NAMES = frozenset(n[:-4] for n in NEVER_CLOSE)


def _repeat_due(now_ts: float, last: Optional[float]) -> bool:
    """Is it time to notify again?  A ``last`` in the future means the clock was
    set back; treat that as due instead of staying silent until it catches up."""
    return last is None or now_ts < last or now_ts - last >= NOTIFY_REPEAT_SECONDS


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


def _activity_hosts(activity: Activity) -> list[str]:
    """The names a site rule is matched against: the domain the agent reported
    (the registrable domain with ``group_sites_by: registrable``) and, when the agent
    could read the address, the full host - so a rule for ``docs.google.com`` matches
    although the statistics say ``google.com``."""
    hosts = [activity.domain] if activity.domain else []
    if activity.url:
        host = domain_from_url(activity.url, "host")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _sites_match(activity: Activity, sites: list[str]) -> bool:
    return bool(sites) and any(_site_matches(host, sites) for host in _activity_hosts(activity))


def rule_matches(rule: AppRule, activity: Activity) -> bool:
    if not activity.is_active:
        return False
    app = _norm_exe(activity.app or "")
    if app and rule.apps and app in {_norm_exe(a) for a in rule.apps}:
        return True
    if rule.sites and _sites_match(activity, rule.sites):
        return True
    if rule.categories and activity.category in {c.lower() for c in rule.categories}:
        return True
    return False


def focus_allows(focus: FocusConfig, activity: Activity, extra_allow: Optional[list[str]] = None) -> bool:
    """Is this activity permitted while focus mode is on?"""
    if not activity.is_active:
        return True
    app = _norm_exe(activity.app or "")
    if app in _NEVER_CLOSE_NAMES:
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
        if activity.site_unknown:
            # a web app or --app window whose address could not be read: whatever its title or
            # start address suggests, it is not an allow-listed site (nor is it allowed because
            # the browser itself is allow-listed), unless the parent allowed such windows
            return bool(focus.allow_unknown_sites)
        if _sites_match(activity, allow_sites):
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
    weekly_limit_minutes: Optional[int] = None
    weekly_used_seconds: float = 0.0
    weekly_remaining_seconds: Optional[float] = None

    @property
    def remaining_minutes(self) -> Optional[float]:
        if self.remaining_seconds is None:
            return None
        return round(max(0.0, self.remaining_seconds) / 60, 1)

    @property
    def weekly_remaining_minutes(self) -> Optional[float]:
        if self.weekly_remaining_seconds is None:
            return None
        return round(max(0.0, self.weekly_remaining_seconds) / 60, 1)

    @property
    def budget_remaining_seconds(self) -> Optional[float]:
        """What is left of the tighter of the daily and the weekly budget (None: neither is set)."""
        left = [v for v in (self.remaining_seconds, self.weekly_remaining_seconds) if v is not None]
        return min(left) if left else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "enabled": self.enabled, "active_now": self.matched, "blocked": self.blocked,
            "limit_minutes": self.limit_minutes, "used_minutes": round(self.used_seconds / 60, 1),
            "remaining_minutes": self.remaining_minutes, "in_allowed_hours": self.in_hours,
            "weekly_limit_minutes": self.weekly_limit_minutes,
            "weekly_used_minutes": round(self.weekly_used_seconds / 60, 1),
            "weekly_remaining_minutes": self.weekly_remaining_minutes,
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
        left = self.budget_remaining_seconds
        if left is not None and left <= 0:
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


_covers = windows_cover


def rule_loosens(old: AppRule, new: AppRule) -> bool:
    """Does replacing ``old`` with ``new`` allow anything ``old`` did not?

    More minutes on some day or per week, longer hours, fewer apps / sites /
    categories covered, no longer blocked, or a warning instead of closing.
    Callers ask for the parent PIN before a change that loosens."""
    if old.blocked and not new.blocked:
        return True
    if old.action == "close" and new.action != "close":
        return True
    if not (set(old.apps) <= set(new.apps) and set(old.sites) <= set(new.sites)
            and set(old.categories) <= set(new.categories)):
        return True
    if new.blocked:
        return False  # everything the old rule covered is now forbidden outright
    inf = float("inf")
    for day in range(7):
        old_limit, new_limit = old.daily_limits.get(day), new.daily_limits.get(day)
        if (inf if new_limit is None else new_limit) > (inf if old_limit is None else old_limit):
            return True
        if not _covers(old.allowed_windows.get(day, []), new.allowed_windows.get(day, [])):
            return True
    old_week = inf if old.weekly_limit_minutes is None else old.weekly_limit_minutes
    return (inf if new.weekly_limit_minutes is None else new.weekly_limit_minutes) > old_week


class RuleEngine:
    def __init__(self, store: Store, week_start_index: int = 0):
        self.store = store
        self.week_start_index = week_start_index   # 0 = Monday (time_limits.week_start)
        self._rt: dict[str, _UserRt] = {}

    def week_start(self, day: date) -> date:
        return day - timedelta(days=(day.weekday() - self.week_start_index) % 7)

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
        """Today's limit of the rule in minutes, None for no limit: the override stored
        for the day (see :meth:`set_rule_limit_today`), else the configured one."""
        override = self.store.get_setting(user, f"rule_limit:{rule.slug}:{day.isoformat()}")
        if override is not None:
            return None if int(override) < 0 else int(override)
        return rule.daily_limits.get(day.weekday())

    def is_loosening_rule_limit(self, user: str, rule: AppRule, minutes: Optional[int], day: date) -> bool:
        """Would storing ``minutes`` as today's override give the user *more* of this rule?

        ``None`` (drop the override) and a negative value (stored as "no limit
        today") both count as unlimited, which is loosening unless the rule
        has no limit today anyway.  Callers gate loosening behind the parent PIN.
        """
        current = self.rule_limit_today(user, rule, day)
        if current is None:
            return False  # already unlimited: nothing can loosen it further
        if minutes is None or int(minutes) < 0:
            return True
        return int(minutes) > current

    def set_rule_limit_today(self, user: str, slug: str, day: date, minutes: Optional[int]) -> None:
        """Store an override of the rule's limit for ``day``.

        Three different things can be asked for:

        * ``minutes >= 0`` - that many minutes today (0: none at all);
        * ``NO_LIMIT`` (any negative number) - *no limit today*, whatever the
          configuration says;
        * ``None`` - remove the override: back to the configured limit.
        """
        key = f"rule_limit:{slug}:{day.isoformat()}"
        if minutes is None:
            self.store.delete_setting(user, key)
        else:
            self.store.set_setting(user, key, NO_LIMIT if int(minutes) < 0 else int(minutes))
        self.store.log_event(user, "rule_limit_today", f"{slug}={minutes}")

    def rule_weekly_limit(self, user: str, rule: AppRule) -> Optional[int]:
        """The rule's weekly budget in minutes: the value set from Home Assistant, else the configured one."""
        override = self.store.get_setting(user, f"rule_weekly_limit:{rule.slug}")
        if override is not None:
            return None if int(override) <= 0 else int(override)
        return rule.weekly_limit_minutes

    def is_loosening_rule_weekly_limit(self, user: str, rule: AppRule, minutes: Optional[int]) -> bool:
        """Like :meth:`is_loosening_rule_limit` for the weekly budget (``None`` / ``<= 0`` = no weekly limit)."""
        current = self.rule_weekly_limit(user, rule)
        if current is None:
            return False
        return minutes is None or int(minutes) <= 0 or int(minutes) > current

    def set_rule_weekly_limit(self, user: str, slug: str, minutes: Optional[int]) -> None:
        """Store a weekly budget for the rule: ``minutes > 0`` is the budget, ``0`` (or
        ``NO_LIMIT`` / any negative number) means *no weekly budget* whatever the
        configuration says, and ``None`` removes the override (back to the configured one)."""
        key = f"rule_weekly_limit:{slug}"
        if minutes is None:
            self.store.delete_setting(user, key)
        else:
            self.store.set_setting(user, key, int(minutes))
        self.store.log_event(user, "rule_weekly_limit", f"{slug}={minutes}")

    # -- status --------------------------------------------------------------------
    def rule_status(self, user: str, rule: AppRule, now: datetime, activity: Optional[Activity]) -> RuleStatus:
        day = now.date()
        enabled = self.rule_enabled(user, rule.slug)
        matched = bool(activity and rule_matches(rule, activity))
        used = self.store.rule_seconds(user, day, rule.slug)
        limit = self.rule_limit_today(user, rule, day)
        remaining = None if limit is None else limit * 60 - used
        weekly_limit = self.rule_weekly_limit(user, rule)
        weekly_used = self.store.rule_seconds_range(user, self.week_start(day), day, rule.slug)
        weekly_remaining = None if weekly_limit is None else weekly_limit * 60 - weekly_used
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
            elif weekly_remaining is not None and weekly_remaining <= 0:
                exceeded, reason = True, "weekly limit used up"
        return RuleStatus(
            slug=rule.slug, name=rule.name, enabled=enabled, matched=matched, blocked=rule.blocked,
            limit_minutes=limit, used_seconds=used, remaining_seconds=remaining, in_hours=in_hours,
            hours_remaining_seconds=None if hours_left is None else hours_left * 60,
            exceeded=exceeded, reason=reason, action=rule.action, icon=rule.icon,
            weekly_limit_minutes=weekly_limit, weekly_used_seconds=weekly_used,
            weekly_remaining_seconds=weekly_remaining,
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
             enforcing: bool = True, agent_foreground: bool = False,
             unknown_site_pending: bool = False) -> list[RuleDecision]:
        """Return the decisions for this user for this tick (possibly none).

        ``agent_foreground``: the window in front belongs to the session's own agent
        (its tray menu), told by its process id - nothing is closed or warned about
        then, whatever the process is called.

        ``unknown_site_pending``: the window's site has been unknown only for a moment
        (the agent's first read of a web app may fail or come late); focus mode waits
        before acting on it.  A window that stays unknown is closed all the same."""
        rt = self._rt.get(user)
        today = now.date()
        if rt is None or rt.day != today:
            rt = _UserRt(day=today)
            self._rt[user] = rt
        if activity is None or not activity.is_active or not enforcing or agent_foreground:
            return []
        now_ts = now.timestamp()
        decisions: list[RuleDecision] = []
        # a web app window without a readable address is closed like a tab (Ctrl+W closes the
        # window), not by ending the whole browser; the usual escalation follows if that fails
        is_browser_site = activity.domain is not None or activity.site_unknown
        app_label = (activity.app or "").rsplit(".exe", 1)[0] or activity.app or "this app"

        for rule in policy.rules:
            st = self.rule_status(user, rule, now, activity)
            if not st.enabled or not st.matched:
                continue
            if st.exceeded:
                notify = _repeat_due(now_ts, rt.last_notify.get(rule.slug))
                if notify:
                    rt.last_notify[rule.slug] = now_ts
                what = activity.domain or app_label
                if rule.blocked:
                    msg = rule.message or f"{what} is not allowed."
                elif not st.in_hours:
                    msg = rule.message or f"{what} is not allowed at this time of day."
                elif st.reason.startswith("weekly"):
                    msg = rule.message or f"Your time for {rule.name} this week ({st.weekly_limit_minutes} min) is used up."
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
            # approaching the limit (or the end of the allowed hours) -> warn once per threshold
            budget = st.budget_remaining_seconds
            hours_left = st.hours_remaining_seconds
            if (budget is not None or hours_left is not None) and rule.warn_at_minutes:
                eff = budget if budget is not None else math.inf
                weekly = budget is not None and (st.remaining_seconds is None or budget < st.remaining_seconds)
                if hours_left is not None:
                    eff = min(eff, hours_left)
                by_hours = hours_left is not None and eff == hours_left and (budget is None or hours_left < budget)
                applicable = {t for t in rule.warn_at_minutes if eff <= t * 60}
                warned = rt.warned.setdefault(rule.slug, set())
                warned &= applicable
                fresh = applicable - warned
                if fresh:
                    warned |= applicable
                    minutes_left = max(1, math.ceil(eff / 60))
                    plural = 's' if minutes_left != 1 else ''
                    if by_hours:
                        text = (f"{rule.name} is only allowed for about {minutes_left} more minute{plural} "
                                f"(the end of its allowed hours).")
                    else:
                        text = (f"You have about {minutes_left} minute{plural} of {rule.name} "
                                f"left {'this week' if weekly else 'today'}.")
                    decisions.append(RuleDecision(
                        kind="warn", source="rule", slug=rule.slug,
                        title=f"{rule.name}: {minutes_left} min left",
                        message=text,
                        level="warning", app=activity.app, minutes_left=minutes_left,
                    ))

        if (focus_active and not (unknown_site_pending and activity.site_unknown)
                and not focus_allows(policy.focus, activity, focus_extra_allow)):
            notify = _repeat_due(now_ts, rt.last_notify.get("focus"))
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
