"""Per-user screen-time accounting and enforcement decisions.

This module is deliberately free of any Windows dependency: it receives the
current sessions and produces *actions* (warn / logoff / lock / shutdown /
block / unblock) that the orchestrator carries out.  That keeps the rules
unit-testable and makes the enforcement logic live in the SYSTEM service where
a child cannot tamper with it.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .config import TimeLimitsConfig, UserPolicy
from .models import SessionInfo
from .store import Store
from .util import human_minutes, window_status

log = logging.getLogger(__name__)

SETTING_LIMIT_ENABLED = "limit_enabled"
SETTING_BLOCKED = "blocked"
SETTING_WEEKDAY_LIMIT = "weekday_limit"
SETTING_WEEKEND_LIMIT = "weekend_limit"
SETTING_GLOBAL_ENABLED = "enabled"

ENFORCE_REPEAT_SECONDS = 30.0  # minimum spacing between repeated enforcement actions


def bonus_key(day: date) -> str:
    return f"bonus:{day.isoformat()}"


def limit_key(day: date) -> str:
    return f"limit:{day.isoformat()}"


@dataclass
class Action:
    kind: str  # warn | logoff | lock | shutdown | block | unblock
    user: str
    sessions: list[int] = field(default_factory=list)
    title: str = ""
    message: str = ""
    minutes_left: Optional[int] = None
    level: str = "info"  # info | warning | critical


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

    @property
    def remaining_minutes(self) -> Optional[float]:
        if self.remaining_seconds is None:
            return None
        return round(max(0.0, self.remaining_seconds) / 60, 1)


@dataclass
class _Runtime:
    day: date
    last_tick: Optional[float] = None
    warned: set = field(default_factory=set)
    grace_started: Optional[float] = None
    enforced: bool = False
    last_enforced: Optional[float] = None
    block_desired: Optional[bool] = None


class TimeKeeper:
    def __init__(self, cfg: TimeLimitsConfig, store: Store, build_default_policy=None):
        self.cfg = cfg
        self.store = store
        self._policies: dict[str, UserPolicy] = {p.key: p for p in cfg.users}
        self._build_default_policy = build_default_policy
        self._runtime: dict[str, _Runtime] = {}
        stored = store.get_setting(Store.GLOBAL, SETTING_GLOBAL_ENABLED)
        self.global_enabled: bool = bool(cfg.enabled if stored is None else stored)

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

    def bonus_seconds(self, user: str, day: date) -> int:
        return int(self.store.get_setting(user, bonus_key(day), 0) or 0)

    def add_bonus_minutes(self, user: str, day: date, minutes: float) -> int:
        total = self.bonus_seconds(user, day) + int(round(minutes * 60))
        self.store.set_setting(user, bonus_key(day), total)
        self.store.log_event(user, "bonus", f"{minutes:+} min -> {total // 60} min")
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
        limit = self.effective_limit_minutes(key, day)
        bonus = self.bonus_seconds(key, day)
        remaining = None if limit is None else limit * 60 + bonus - totals.active
        weekday = day.weekday()
        windows = policy.allowed_windows.get(weekday, [])
        next_windows = policy.allowed_windows.get((weekday + 1) % 7, [])
        minute = now.hour * 60 + now.minute + now.second / 60
        in_window, win_remaining_min = window_status(minute, windows, next_windows)
        win_remaining = None if win_remaining_min is None else win_remaining_min * 60
        manual = self.manually_blocked(key)
        limit_on = self.limit_enabled(key)
        enforcing = self.global_enabled and limit_on
        out_of_time = manual or (remaining is not None and remaining <= 0) or (bool(windows) and not in_window)
        candidates = [v for v in (remaining, win_remaining) if v is not None]
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
        )

    # -- the tick ------------------------------------------------------------
    def session_counts(self, session: SessionInfo, policy: UserPolicy) -> bool:
        """Does this session consume budget right now?"""
        if session.state != "active":
            return False
        if session.locked and not policy.count_locked:
            return False
        if not policy.count_idle and session.idle_seconds is not None and session.idle_seconds >= policy.idle_threshold_seconds:
            return False
        return True

    def tick(self, now: datetime, sessions_by_user: dict[str, list[SessionInfo]]) -> list[Action]:
        actions: list[Action] = []
        now_ts = now.timestamp()
        today = now.date()
        users = set(self._policies) | set(sessions_by_user)
        for user in sorted(users):
            policy = self.policy_for(user)
            if policy is None:
                continue
            sessions = sessions_by_user.get(user, [])
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
            if counting and dt > 0:
                self.store.add_usage(user, today, active=dt)
            if policy.unlimited:
                continue

            st = self.status(user, now)
            logged_in = [s.session_id for s in sessions if s.logged_in]

            # login blocking (desired state is re-evaluated every tick)
            if st.block_desired != rt.block_desired:
                rt.block_desired = st.block_desired
                actions.append(Action("block" if st.block_desired else "unblock", user))

            if st.enforcing and st.out_of_time and logged_in:
                if rt.enforced:
                    if rt.last_enforced is None or now_ts - rt.last_enforced >= ENFORCE_REPEAT_SECONDS:
                        rt.last_enforced = now_ts
                        actions.append(self._enforcement(policy, user, logged_in, st))
                elif rt.grace_started is None:
                    rt.grace_started = now_ts
                    actions.append(
                        Action(
                            "warn", user, logged_in, title="Time is up",
                            message=self._grace_message(policy, st), minutes_left=0, level="critical",
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
                    reason = "Your computer time" if st.remaining_seconds == eff else "Your allowed hours"
                    actions.append(
                        Action(
                            "warn", user, [s.session_id for s in counting],
                            title=f"{minutes_left} minute{'s' if minutes_left != 1 else ''} left",
                            message=f"{reason} end{'s' if reason.startswith('Your computer') else ''} in about "
                                    f"{minutes_left} minute{'s' if minutes_left != 1 else ''}. Please save your work.",
                            minutes_left=minutes_left, level="warning",
                        )
                    )
        return actions

    def _grace_message(self, policy: UserPolicy, st: TimeStatus) -> str:
        what = {"logoff": "logged off", "lock": "locked", "shutdown": "shut down"}[policy.action]
        if st.manually_blocked:
            why = "Your computer access has been paused."
        elif st.remaining_seconds is not None and st.remaining_seconds <= 0:
            why = f"You have used your computer time for today ({human_minutes(st.used_seconds)})."
        else:
            why = "You are outside your allowed computer hours."
        return f"{why} You will be {what} in {policy.grace_seconds} seconds. Save your work now."

    def _enforcement(self, policy: UserPolicy, user: str, sessions: list[int], st: TimeStatus) -> Action:
        self.store.log_event(user, policy.action, f"used={int(st.used_seconds)}s limit={st.limit_minutes}")
        return Action(policy.action, user, sessions, title="Time is up", message="Your computer time is over.", level="critical")

    def reset_runtime(self, user: str) -> None:
        self._runtime.pop(user.lower(), None)
