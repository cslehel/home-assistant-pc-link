"""Per-user screen-time accounting and enforcement decisions.

This module is deliberately free of any Windows dependency: it receives the
current sessions and produces *actions* (warn / logoff / lock / shutdown /
block / unblock) that the orchestrator carries out.  That keeps the rules
unit-testable and makes the enforcement logic live in the SYSTEM service where
a child cannot tamper with it.

Budgets a user can run out of, all evaluated every tick:

* the **daily** limit (+ bonus minutes, + minutes rolled over from yesterday);
* the **weekly** limit;
* the **allowed hours** windows;
* the **bedtime** schedule (separate, extendable from HA, auto-extended while a
  parent is home);
* a **manual** pause from HA.

Focus (homework) mode can pause the daily/weekly budget; per-app rules are in
``pclink/rules.py``.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .config import RewardConfig, TimeLimitsConfig, UserPolicy
from .models import SessionInfo
from .store import Store
from .util import Window, clamp, human_minutes, window_status

log = logging.getLogger(__name__)

SETTING_LIMIT_ENABLED = "limit_enabled"
SETTING_BLOCKED = "blocked"
SETTING_WEEKDAY_LIMIT = "weekday_limit"
SETTING_WEEKEND_LIMIT = "weekend_limit"
SETTING_WEEKLY_LIMIT = "weekly_limit"
SETTING_GLOBAL_ENABLED = "enabled"
SETTING_PARENT_PRESENT = "parent_present"
SETTING_FOCUS_UNTIL = "focus_until"
SETTING_FOCUS_ALLOW = "focus_allow"
SETTING_BREAKS_ENABLED = "breaks_enabled"

ENFORCE_REPEAT_SECONDS = 30.0  # minimum spacing between repeated enforcement actions

# external usage provider: (user, day) -> (seconds_today_elsewhere, seconds_this_week_elsewhere)
ExternalUsage = Callable[[str, date, date], tuple[float, float]]


def bonus_key(day: date) -> str:
    return f"bonus:{day.isoformat()}"


def limit_key(day: date) -> str:
    return f"limit:{day.isoformat()}"


def rollover_key(day: date) -> str:
    return f"rollover:{day.isoformat()}"


def bedtime_ext_key(day: date) -> str:
    return f"bedtime_ext:{day.isoformat()}"


def reward_key(slug: str, day: date) -> str:
    return f"reward:{slug}:{day.isoformat()}"


def earned_key(day: date) -> str:
    return f"earned:{day.isoformat()}"


def focus_skip_key(day: date) -> str:
    return f"focus_skip:{day.isoformat()}"


@dataclass
class Action:
    kind: str  # warn | logoff | lock | shutdown | block | unblock
    user: str
    sessions: list[int] = field(default_factory=list)
    title: str = ""
    message: str = ""
    minutes_left: Optional[int] = None
    level: str = "info"  # info | warning | critical
    reason: str = ""     # daily | weekly | hours | bedtime | manual


@dataclass
class TimeStatus:
    user: str
    managed: bool
    enforcing: bool
    limit_enabled: bool
    used_seconds: float
    limit_minutes: Optional[int]
    bonus_seconds: int
    remaining_seconds: Optional[float]
    in_window: bool
    window_remaining_seconds: Optional[float]
    effective_remaining_seconds: Optional[float]
    out_of_time: bool
    manually_blocked: bool
    grace_active: bool
    grace_ends_at: Optional[float]
    block_desired: bool
    weekday_limit: Optional[int]
    weekend_limit: Optional[int]
    limit_today_override: Optional[int]
    # -- weekly / rollover ------------------------------------------------------
    weekly_limit_minutes: Optional[int] = None
    weekly_used_seconds: float = 0.0
    weekly_remaining_seconds: Optional[float] = None
    rollover_seconds: int = 0
    external_used_seconds: float = 0.0
    # -- bedtime ----------------------------------------------------------------
    has_bedtime: bool = False
    bedtime_minute: Optional[int] = None          # effective (incl. extension), minute of day, may exceed 1440
    wake_minute: Optional[int] = None
    in_bedtime_window: bool = True
    bedtime_remaining_seconds: Optional[float] = None
    bedtime_extension_minutes: int = 0
    bedtime_auto_extension_minutes: int = 0
    # -- focus / misc ------------------------------------------------------------
    focus_active: bool = False
    focus_until: Optional[float] = None
    focus_source: Optional[str] = None
    focus_pauses_limit: bool = False
    earned_seconds: int = 0
    limiting_factor: Optional[str] = None       # daily | weekly | hours | bedtime | manual

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
    def past_bedtime(self) -> bool:
        return self.has_bedtime and not self.in_bedtime_window


@dataclass
class _Runtime:
    day: date
    last_tick: Optional[float] = None
    warned: set = field(default_factory=set)
    grace_started: Optional[float] = None
    enforced: bool = False
    last_enforced: Optional[float] = None
    block_desired: Optional[bool] = None


def bedtime_windows(policy: UserPolicy, day: date, ext_minutes: int, prev_ext_minutes: int) -> list[Window]:
    """Allowed windows for ``day`` derived from the bedtime schedule.

    Allowed = [wake, bedtime + extension).  A bedtime at or before the wake
    time (e.g. bed 01:00, wake 07:00) means "past midnight"; the part after
    midnight spills into the next day, as does an extension that crosses it.
    """
    if not policy.has_bedtime:
        return []
    wd = day.weekday()
    prev = (day - timedelta(days=1)).weekday()
    out: list[Window] = []
    # spill-over from yesterday's bedtime past midnight
    pbed = policy.bedtime.get(prev)
    if pbed is not None:
        pwake = policy.wake_time.get(prev, 0)
        end = pbed + prev_ext_minutes
        if pbed <= pwake:
            end = pbed + 1440 + prev_ext_minutes
        if end > 1440:
            out.append((0, min(1440, end - 1440)))
    bed = policy.bedtime.get(wd)
    if bed is None:
        # no bedtime today: allowed all day (apart from the spill-over, which only widens)
        return [(0, 1440)]
    wake = policy.wake_time.get(wd, 0)
    end = bed + ext_minutes
    if bed <= wake:
        end = bed + 1440 + ext_minutes  # bedtime is after midnight
    out.append((wake, min(1440, end)))
    return sorted(set(w for w in out if w[1] > w[0]))


class TimeKeeper:
    def __init__(self, cfg: TimeLimitsConfig, store: Store, build_default_policy=None):
        self.cfg = cfg
        self.store = store
        self._policies: dict[str, UserPolicy] = {p.key: p for p in cfg.users}
        self._build_default_policy = build_default_policy
        self._runtime: dict[str, _Runtime] = {}
        self.external_usage: Optional[ExternalUsage] = None
        stored = store.get_setting(Store.GLOBAL, SETTING_GLOBAL_ENABLED)
        self.global_enabled: bool = bool(cfg.enabled if stored is None else stored)

    def update_config(self, cfg: TimeLimitsConfig, build_default_policy=None) -> None:
        """Hot reload: swap policies, keep runtime counters."""
        self.cfg = cfg
        self._policies = {p.key: p for p in cfg.users}
        if build_default_policy is not None:
            self._build_default_policy = build_default_policy

    # -- policies -------------------------------------------------------------
    def policy_for(self, user: str) -> Optional[UserPolicy]:
        key = user.lower()
        policy = self._policies.get(key)
        if policy is None and self.cfg.manage_unknown_users and self._build_default_policy is not None:
            policy = self._build_default_policy(user)
            self._policies[key] = policy
        return policy

    def managed_users(self) -> list[str]:
        return [k for k, p in self._policies.items() if not p.unlimited]

    def all_policy_users(self) -> list[str]:
        return list(self._policies)

    def is_managed(self, user: str) -> bool:
        policy = self.policy_for(user)
        return policy is not None and not policy.unlimited

    # -- HA-controlled settings ----------------------------------------------
    def set_global_enabled(self, enabled: bool) -> None:
        self.global_enabled = bool(enabled)
        self.store.set_setting(Store.GLOBAL, SETTING_GLOBAL_ENABLED, self.global_enabled)
        self.store.log_event(None, "global_enabled", str(self.global_enabled))

    @property
    def parent_present(self) -> bool:
        return bool(self.store.get_setting(Store.GLOBAL, SETTING_PARENT_PRESENT, False))

    def set_parent_present(self, present: bool) -> None:
        if self.parent_present == bool(present):
            return
        self.store.set_setting(Store.GLOBAL, SETTING_PARENT_PRESENT, bool(present))
        self.store.log_event(None, "parent_present", str(bool(present)))
        if present:
            for user in self.managed_users():
                self._reset_after_grant(user)

    def limit_enabled(self, user: str) -> bool:
        return bool(self.store.get_setting(user, SETTING_LIMIT_ENABLED, True))

    def set_limit_enabled(self, user: str, enabled: bool) -> None:
        self.store.set_setting(user, SETTING_LIMIT_ENABLED, bool(enabled))
        self.store.log_event(user, "limit_enabled", str(bool(enabled)))

    def manually_blocked(self, user: str) -> bool:
        return bool(self.store.get_setting(user, SETTING_BLOCKED, False))

    def set_manually_blocked(self, user: str, blocked: bool) -> None:
        self.store.set_setting(user, SETTING_BLOCKED, bool(blocked))
        self.store.log_event(user, "manual_block", str(bool(blocked)))
        if not blocked:
            self._reset_after_grant(user)

    def bonus_seconds(self, user: str, day: date) -> int:
        return int(self.store.get_setting(user, bonus_key(day), 0) or 0)

    def add_bonus_minutes(self, user: str, day: date, minutes: float, reason: str = "") -> int:
        total = self.bonus_seconds(user, day) + int(round(minutes * 60))
        self.store.set_setting(user, bonus_key(day), total)
        self.store.log_event(user, "bonus", f"{minutes:+} min -> {total // 60} min" + (f" ({reason})" if reason else ""))
        self._reset_after_grant(user)
        return total

    def set_bonus_minutes(self, user: str, day: date, minutes: float) -> None:
        self.store.set_setting(user, bonus_key(day), int(round(minutes * 60)))
        self.store.log_event(user, "bonus_set", f"{minutes} min")
        self._reset_after_grant(user)

    def set_limit_today(self, user: str, day: date, minutes: Optional[int]) -> None:
        if minutes is None:
            self.store.delete_setting(user, limit_key(day))
        else:
            self.store.set_setting(user, limit_key(day), int(minutes))
        self.store.log_event(user, "limit_today", str(minutes))
        self._reset_after_grant(user)

    def set_weekday_limit(self, user: str, minutes: Optional[int]) -> None:
        if minutes is None:
            self.store.delete_setting(user, SETTING_WEEKDAY_LIMIT)
        else:
            self.store.set_setting(user, SETTING_WEEKDAY_LIMIT, int(minutes))
        self._reset_after_grant(user)

    def set_weekend_limit(self, user: str, minutes: Optional[int]) -> None:
        if minutes is None:
            self.store.delete_setting(user, SETTING_WEEKEND_LIMIT)
        else:
            self.store.set_setting(user, SETTING_WEEKEND_LIMIT, int(minutes))
        self._reset_after_grant(user)

    def set_weekly_limit(self, user: str, minutes: Optional[int]) -> None:
        if minutes is None:
            self.store.delete_setting(user, SETTING_WEEKLY_LIMIT)
        else:
            self.store.set_setting(user, SETTING_WEEKLY_LIMIT, int(minutes))
        self.store.log_event(user, "weekly_limit", str(minutes))
        self._reset_after_grant(user)

    # -- bedtime ------------------------------------------------------------------
    def bedtime_extension_minutes(self, user: str, day: date) -> int:
        return int(self.store.get_setting(user, bedtime_ext_key(day), 0) or 0)

    def set_bedtime_extension(self, user: str, day: date, minutes: int) -> None:
        self.store.set_setting(user, bedtime_ext_key(day), int(max(0, minutes)))
        self.store.log_event(user, "bedtime_extension", f"{int(minutes)} min")
        self._reset_after_grant(user)

    def extend_bedtime(self, user: str, day: date, minutes: int) -> int:
        total = self.bedtime_extension_minutes(user, day) + int(minutes)
        self.set_bedtime_extension(user, day, total)
        return total

    def _bedtime_ext(self, user: str, policy: UserPolicy, day: date) -> tuple[int, int, int]:
        """(manual, automatic, total) extension minutes for ``day``."""
        manual = self.bedtime_extension_minutes(user, day)
        auto = policy.bedtime_parent_extension_minutes if (self.parent_present and policy.bedtime_parent_extension_minutes > 0) else 0
        return manual, auto, manual + auto

    # -- focus mode ------------------------------------------------------------------
    def focus_state(self, user: str, policy: UserPolicy, now: datetime) -> tuple[bool, Optional[float], Optional[str]]:
        """(active, until_ts, source) where source is manual | schedule."""
        now_ts = now.timestamp()
        until = self.store.get_setting(user, SETTING_FOCUS_UNTIL)
        if until is not None:
            if now_ts < float(until):
                return True, float(until), "manual"
            self.store.delete_setting(user, SETTING_FOCUS_UNTIL)
            self.store.delete_setting(user, SETTING_FOCUS_ALLOW)
        if policy.focus.scheduled and not self.store.get_setting(user, focus_skip_key(now.date()), False):
            weekday = now.weekday()
            minute = now.hour * 60 + now.minute + now.second / 60
            windows = policy.focus.schedule.get(weekday, [])
            if windows:  # (no windows today means "no focus", unlike allowed-hours semantics)
                inside, remaining = window_status(minute, windows)
                if inside:
                    until_ts = now_ts + (remaining or 0) * 60
                    return True, until_ts, "schedule"
        return False, None, None

    def focus_extra_allow(self, user: str) -> list[str]:
        value = self.store.get_setting(user, SETTING_FOCUS_ALLOW, [])
        return [str(v) for v in (value or [])]

    def start_focus(self, user: str, now: datetime, minutes: Optional[int] = None,
                    allow: Optional[list[str]] = None) -> float:
        policy = self.policy_for(user)
        default = policy.focus.default_minutes if policy else 60
        mins = int(minutes if minutes else default)
        until = now.timestamp() + max(1, mins) * 60
        self.store.set_setting(user, SETTING_FOCUS_UNTIL, until)
        if allow:
            self.store.set_setting(user, SETTING_FOCUS_ALLOW, [str(a).lower() for a in allow])
        else:
            self.store.delete_setting(user, SETTING_FOCUS_ALLOW)
        self.store.log_event(user, "focus_start", f"{mins} min")
        return until

    def stop_focus(self, user: str, now: datetime) -> None:
        policy = self.policy_for(user)
        self.store.delete_setting(user, SETTING_FOCUS_UNTIL)
        self.store.delete_setting(user, SETTING_FOCUS_ALLOW)
        if policy is not None and policy.focus.scheduled:
            active, _until, source = self.focus_state(user, policy, now)
            if active and source == "schedule":
                self.store.set_setting(user, focus_skip_key(now.date()), True)
        self.store.log_event(user, "focus_stop", "")

    # -- breaks -----------------------------------------------------------------------
    def breaks_enabled(self, user: str, policy: UserPolicy) -> bool:
        value = self.store.get_setting(user, SETTING_BREAKS_ENABLED)
        return policy.breaks.enabled if value is None else bool(value)

    def set_breaks_enabled(self, user: str, enabled: bool) -> None:
        self.store.set_setting(user, SETTING_BREAKS_ENABLED, bool(enabled))
        self.store.log_event(user, "breaks_enabled", str(bool(enabled)))

    # -- rewards / earned minutes -------------------------------------------------------
    def reward_claims(self, user: str, slug: str, day: date) -> int:
        return int(self.store.get_setting(user, reward_key(slug, day), 0) or 0)

    def earned_seconds(self, user: str, day: date) -> int:
        return int(self.store.get_setting(user, earned_key(day), 0) or 0)

    def earn_minutes(self, user: str, day: date, minutes: float, reason: str = "") -> int:
        """Grant minutes for something done (a chore, homework...)."""
        total = self.add_bonus_minutes(user, day, minutes, reason=reason or "earned")
        self.store.set_setting(user, earned_key(day), self.earned_seconds(user, day) + int(round(minutes * 60)))
        self.store.log_event(user, "earned", f"{minutes} min" + (f" ({reason})" if reason else ""))
        return total

    def claim_reward(self, user: str, reward: RewardConfig, day: date) -> tuple[bool, str]:
        claims = self.reward_claims(user, reward.slug, day)
        if reward.daily_max and claims >= reward.daily_max:
            return False, f"'{reward.name}' already claimed {claims}x today"
        self.store.set_setting(user, reward_key(reward.slug, day), claims + 1)
        self.earn_minutes(user, day, reward.minutes, reason=reward.name)
        return True, ""

    def _reset_after_grant(self, user: str) -> None:
        rt = self._runtime.get(user.lower())
        if rt:
            rt.grace_started = None
            rt.enforced = False

    # -- computed status -----------------------------------------------------
    def effective_limit_minutes(self, user: str, day: date) -> Optional[int]:
        policy = self.policy_for(user)
        if policy is None or policy.unlimited:
            return None
        override = self.store.get_setting(user, limit_key(day))
        if override is not None:
            return int(override)
        weekday = day.weekday()
        group_key = SETTING_WEEKEND_LIMIT if weekday >= 5 else SETTING_WEEKDAY_LIMIT
        group_override = self.store.get_setting(user, group_key)
        if group_override is not None:
            return int(group_override)
        return policy.daily_limits.get(weekday)

    def weekly_limit_minutes(self, user: str) -> Optional[int]:
        policy = self.policy_for(user)
        if policy is None or policy.unlimited:
            return None
        override = self.store.get_setting(user, SETTING_WEEKLY_LIMIT)
        if override is not None:
            return None if int(override) <= 0 else int(override)
        return policy.weekly_limit_minutes

    def week_start(self, day: date) -> date:
        return day - timedelta(days=(day.weekday() - self.cfg.week_start_index) % 7)

    def rollover_seconds(self, user: str, day: date) -> int:
        """Unused minutes carried over from yesterday (computed once per day, then stored)."""
        policy = self.policy_for(user)
        if policy is None or policy.unlimited or policy.rollover_max_minutes <= 0:
            return 0
        key = rollover_key(day)
        stored = self.store.get_setting(user, key)
        if stored is not None:
            return int(stored)
        yesterday = day - timedelta(days=1)
        y_limit = self.effective_limit_minutes(user, yesterday)
        carry = 0
        if y_limit is not None:
            y_bonus = self.bonus_seconds(user, yesterday)
            y_roll = int(self.store.get_setting(user, rollover_key(yesterday), 0) or 0)
            y_used = self.store.usage(user, yesterday).active
            carry = int(clamp(y_limit * 60 + y_bonus + y_roll - y_used, 0, policy.rollover_max_minutes * 60))
        self.store.set_setting(user, key, carry)
        if carry:
            self.store.log_event(user, "rollover", f"{carry // 60} min carried from {yesterday.isoformat()}")
        return carry

    def _external(self, user: str, day: date) -> tuple[float, float]:
        if self.external_usage is None or not self.cfg.shared_budget:
            return 0.0, 0.0
        try:
            today_s, week_s = self.external_usage(user, day, self.week_start(day))
            return max(0.0, float(today_s)), max(0.0, float(week_s))
        except Exception:  # noqa: BLE001
            log.debug("external usage provider failed", exc_info=True)
            return 0.0, 0.0

    def status(self, user: str, now: datetime) -> TimeStatus:
        key = user.lower()
        policy = self.policy_for(user)
        day = now.date()
        totals = self.store.usage(key, day)
        rt = self._runtime.get(key)
        if policy is None or policy.unlimited:
            return TimeStatus(
                user=key, managed=False, enforcing=False, limit_enabled=True,
                used_seconds=totals.active, limit_minutes=None, bonus_seconds=0, remaining_seconds=None,
                in_window=True, window_remaining_seconds=None, effective_remaining_seconds=None,
                out_of_time=False, manually_blocked=False, grace_active=False, grace_ends_at=None,
                block_desired=False, weekday_limit=None, weekend_limit=None, limit_today_override=None,
            )
        ext_today, ext_week = self._external(key, day)
        used_today = totals.active + ext_today
        limit = self.effective_limit_minutes(key, day)
        bonus = self.bonus_seconds(key, day)
        rollover = self.rollover_seconds(key, day)
        remaining = None if limit is None else limit * 60 + bonus + rollover - used_today
        # weekly ---------------------------------------------------------------------
        weekly_limit = self.weekly_limit_minutes(key)
        week_start = self.week_start(day)
        week_used = self.store.usage_range(key, week_start, day).active + ext_week
        weekly_remaining = None if weekly_limit is None else weekly_limit * 60 - week_used
        # allowed hours -------------------------------------------------------------------
        weekday = day.weekday()
        windows = policy.allowed_windows.get(weekday, [])
        next_windows = policy.allowed_windows.get((weekday + 1) % 7, [])
        minute = now.hour * 60 + now.minute + now.second / 60
        in_window, win_remaining_min = window_status(minute, windows, next_windows)
        win_remaining = None if win_remaining_min is None else win_remaining_min * 60
        # bedtime ------------------------------------------------------------------------
        ext_manual, ext_auto, ext_total = self._bedtime_ext(key, policy, day)
        bed_windows = bedtime_windows(policy, day, ext_total, self._bedtime_ext(key, policy, day - timedelta(days=1))[2])
        tomorrow = day + timedelta(days=1)
        bed_windows_next = bedtime_windows(policy, tomorrow, self._bedtime_ext(key, policy, tomorrow)[2], ext_total)
        if policy.has_bedtime:
            in_bed, bed_remaining_min = window_status(minute, bed_windows, bed_windows_next)
        else:
            in_bed, bed_remaining_min = True, None
        bed_remaining = None if bed_remaining_min is None else bed_remaining_min * 60
        bed_minute = policy.bedtime.get(weekday)
        if bed_minute is not None:
            wake_minute = policy.wake_time.get(weekday, 0)
            if bed_minute <= wake_minute:
                bed_minute += 1440
            bed_minute += ext_total
        # focus ---------------------------------------------------------------------------
        focus_active, focus_until, focus_source = self.focus_state(key, policy, now)
        focus_pauses = bool(focus_active and not policy.focus.counts_toward_limit)
        # verdict --------------------------------------------------------------------
        manual = self.manually_blocked(key)
        limit_on = self.limit_enabled(key)
        enforcing = self.global_enabled and limit_on
        factor: Optional[str] = None
        if manual:
            factor = "manual"
        elif bool(windows) and not in_window:
            factor = "hours"
        elif policy.has_bedtime and not in_bed:
            factor = "bedtime"
        elif not focus_pauses and remaining is not None and remaining <= 0:
            factor = "daily"
        elif not focus_pauses and weekly_remaining is not None and weekly_remaining <= 0:
            factor = "weekly"
        out_of_time = factor is not None
        candidates = [v for v in (win_remaining, bed_remaining) if v is not None]
        if not focus_pauses:
            candidates += [v for v in (remaining, weekly_remaining) if v is not None]
        effective = min(candidates) if candidates else None
        if manual:
            effective = 0.0
        return TimeStatus(
            user=key, managed=True, enforcing=enforcing, limit_enabled=limit_on,
            used_seconds=totals.active, limit_minutes=limit, bonus_seconds=bonus, remaining_seconds=remaining,
            in_window=in_window, window_remaining_seconds=win_remaining, effective_remaining_seconds=effective,
            out_of_time=out_of_time, manually_blocked=manual,
            grace_active=bool(rt and rt.grace_started is not None),
            grace_ends_at=(rt.grace_started + policy.grace_seconds) if rt and rt.grace_started is not None else None,
            block_desired=bool(enforcing and policy.block_login and out_of_time),
            weekday_limit=self.store.get_setting(key, SETTING_WEEKDAY_LIMIT),
            weekend_limit=self.store.get_setting(key, SETTING_WEEKEND_LIMIT),
            limit_today_override=self.store.get_setting(key, limit_key(day)),
            weekly_limit_minutes=weekly_limit, weekly_used_seconds=week_used, weekly_remaining_seconds=weekly_remaining,
            rollover_seconds=rollover, external_used_seconds=ext_today,
            has_bedtime=policy.has_bedtime, bedtime_minute=bed_minute,
            wake_minute=policy.wake_time.get(weekday, 0) if policy.has_bedtime else None,
            in_bedtime_window=in_bed, bedtime_remaining_seconds=bed_remaining,
            bedtime_extension_minutes=ext_manual, bedtime_auto_extension_minutes=ext_auto,
            focus_active=focus_active, focus_until=focus_until, focus_source=focus_source,
            focus_pauses_limit=focus_pauses, earned_seconds=self.earned_seconds(key, day),
            limiting_factor=factor,
        )

    # -- the tick ------------------------------------------------------------
    def session_counts(self, session: SessionInfo, policy: UserPolicy) -> bool:
        """Does this session consume budget right now?

        Uses the presence verdict the orchestrator attached to the session
        (``pclink/presence.py``) and falls back to the raw idle timer.
        """
        if session.state != "active":
            return False
        if session.locked and not policy.count_locked:
            return False
        idle = session.idle_seconds
        beyond_threshold = idle is not None and idle >= policy.idle_threshold_seconds
        state = session.presence
        if state is None:
            return policy.count_idle or not beyond_threshold
        if state == "away":
            if session.presence_reason.startswith("no input"):
                # plain inactivity: honour the user's idle threshold, not the global away threshold
                return policy.counts_away() if beyond_threshold else True
            return policy.counts_away()
        if state == "idle":
            return policy.count_idle or not beyond_threshold
        if state == "passive":
            return policy.count_passive
        return True

    def tick(self, now: datetime, sessions_by_user: dict[str, list[SessionInfo]]) -> list[Action]:
        actions: list[Action] = []
        now_ts = now.timestamp()
        today = now.date()
        users = set(self._policies) | set(sessions_by_user)
        for user in sorted(users):
            policy = self.policy_for(user)
            sessions = sessions_by_user.get(user, [])
            if policy is None:
                # Not listed and not managed: still keep "time used" statistics.
                self._accrue_unmanaged(user, sessions, now_ts, today)
                continue
            rt = self._runtime.get(user)
            if rt is None or rt.day != today:
                rt = _Runtime(day=today, block_desired=rt.block_desired if rt else None,
                              last_tick=rt.last_tick if rt else None)
                self._runtime[user] = rt
            dt = 0.0
            if rt.last_tick is not None:
                dt = max(0.0, min(now_ts - rt.last_tick, 120.0))
            rt.last_tick = now_ts

            counting = [s for s in sessions if self.session_counts(s, policy)]
            if policy.unlimited:
                if counting and dt > 0:
                    self.store.add_usage(user, today, active=dt)
                continue
            focus_active, _until, _source = self.focus_state(user, policy, now)
            focus_pauses = focus_active and not policy.focus.counts_toward_limit
            if counting and dt > 0 and not focus_pauses:
                self.store.add_usage(user, today, active=dt)
            st = self.status(user, now)
            logged_in = [s.session_id for s in sessions if s.logged_in]

            # login blocking (desired state is re-evaluated every tick)
            if st.block_desired != rt.block_desired:
                rt.block_desired = st.block_desired
                actions.append(Action("block" if st.block_desired else "unblock", user, reason=st.limiting_factor or ""))

            if st.enforcing and st.out_of_time and logged_in:
                if rt.enforced:
                    if rt.last_enforced is None or now_ts - rt.last_enforced >= ENFORCE_REPEAT_SECONDS:
                        rt.last_enforced = now_ts
                        actions.append(self._enforcement(policy, user, logged_in, st))
                elif rt.grace_started is None:
                    rt.grace_started = now_ts
                    actions.append(
                        Action(
                            "warn", user, logged_in, title=self._grace_title(st),
                            message=self._grace_message(policy, st), minutes_left=0, level="critical",
                            reason=st.limiting_factor or "",
                        )
                    )
                elif now_ts - rt.grace_started >= policy.grace_seconds:
                    rt.enforced = True
                    rt.last_enforced = now_ts
                    rt.grace_started = None
                    actions.append(self._enforcement(policy, user, logged_in, st))
                continue

            rt.grace_started = None
            if not st.out_of_time:
                rt.enforced = False
            if st.enforcing and counting and st.effective_remaining_seconds is not None:
                eff = st.effective_remaining_seconds
                applicable = {t for t in policy.warn_at_minutes if eff <= t * 60}
                # forget thresholds the user has climbed back above (bonus granted)
                rt.warned = {t for t in rt.warned if t in applicable}
                fresh = applicable - rt.warned
                if fresh:
                    rt.warned |= applicable
                    minutes_left = max(1, math.ceil(eff / 60))
                    reason, verb = self._warn_reason(st, eff)
                    plural = "s" if minutes_left != 1 else ""
                    actions.append(
                        Action(
                            "warn", user, [s.session_id for s in counting],
                            title=f"{minutes_left} minute{plural} left",
                            message=f"{reason} {verb} in about {minutes_left} minute{plural}. Please save your work.",
                            minutes_left=minutes_left, level="warning", reason=self._factor_for(st, eff),
                        )
                    )
        return actions

    # -- messages -------------------------------------------------------------------
    @staticmethod
    def _factor_for(st: TimeStatus, eff: float) -> str:
        if st.remaining_seconds is not None and st.remaining_seconds == eff:
            return "daily"
        if st.weekly_remaining_seconds is not None and st.weekly_remaining_seconds == eff:
            return "weekly"
        if st.bedtime_remaining_seconds is not None and st.bedtime_remaining_seconds == eff:
            return "bedtime"
        return "hours"

    def _warn_reason(self, st: TimeStatus, eff: float) -> tuple[str, str]:
        factor = self._factor_for(st, eff)
        return {
            "daily": ("Your computer time", "ends"),
            "weekly": ("Your computer time for this week", "ends"),
            "bedtime": ("Bedtime", "is"),
            "hours": ("Your allowed hours", "end"),
        }[factor]

    @staticmethod
    def _grace_title(st: TimeStatus) -> str:
        return {"bedtime": "Bedtime", "hours": "Outside allowed hours", "manual": "Access paused",
                "weekly": "Weekly time is up"}.get(st.limiting_factor or "", "Time is up")

    def _accrue_unmanaged(self, user: str, sessions: list[SessionInfo], now_ts: float, today: date) -> None:
        rt = self._runtime.get(user)
        if rt is None or rt.day != today:
            rt = _Runtime(day=today, last_tick=rt.last_tick if rt else None)
            self._runtime[user] = rt
        dt = 0.0
        if rt.last_tick is not None:
            dt = max(0.0, min(now_ts - rt.last_tick, 120.0))
        rt.last_tick = now_ts
        if dt > 0 and any(s.state == "active" and not s.locked for s in sessions):
            self.store.add_usage(user, today, active=dt)

    def _grace_message(self, policy: UserPolicy, st: TimeStatus) -> str:
        what = {"logoff": "logged off", "lock": "locked", "shutdown": "shut down"}[policy.action]
        factor = st.limiting_factor
        if factor == "manual":
            why = "Your computer access has been paused."
        elif factor == "daily":
            why = f"You have used your computer time for today ({human_minutes(st.used_seconds)})."
        elif factor == "weekly":
            why = f"You have used your computer time for this week ({human_minutes(st.weekly_used_seconds)})."
        elif factor == "bedtime":
            why = "It is bedtime."
        else:
            why = "You are outside your allowed computer hours."
        return f"{why} You will be {what} in {policy.grace_seconds} seconds. Save your work now."

    def _enforcement(self, policy: UserPolicy, user: str, sessions: list[int], st: TimeStatus) -> Action:
        self.store.log_event(user, policy.action, f"used={int(st.used_seconds)}s limit={st.limit_minutes} reason={st.limiting_factor}")
        title = self._grace_title(st)
        return Action(policy.action, user, sessions, title=title, message="Your computer time is over.",
                      level="critical", reason=st.limiting_factor or "")

    def reset_runtime(self, user: str) -> None:
        self._runtime.pop(user.lower(), None)
