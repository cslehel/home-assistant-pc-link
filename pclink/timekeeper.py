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
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .config import NORMAL_PROFILE, RewardConfig, TimeLimitsConfig, UserPolicy
from .models import SessionInfo
from .store import Store
from .util import NO_LIMIT, Window, clamp, human_minutes, window_status, windows_cover

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
SETTING_PROFILE = "profile"                     # the active profile's name (absent: the normal policy)
SETTING_FOCUS_SKIP_UNTIL = "focus_skip_until"  # the scheduled focus window that was ended by hand ends then

ENFORCE_REPEAT_SECONDS = 30.0  # minimum spacing between repeated enforcement actions
MAX_TICK_GAP_SECONDS = 120.0   # a longer hole between ticks (sleep, hibernate, stale state) is not screen time
MAX_FOCUS_MINUTES = 1440       # the longest focus period that can be started by hand
EXTERNAL_SAVE_STEP_SECONDS = 60.0   # usage on other PCs is written down when it grew by this much
TICK_ERROR_LOG_SECONDS = 300.0      # one failing user is logged this often, not on every tick
AGENT_GRACE_SECONDS = 60.0     # focus mode pauses the budget this long without an agent report (login, relaunch)
FOCUS_TRUST_SECONDS = 600.0    # ... and, once it stopped doing so, again only after this long with reports throughout
STATUS_FAILURES_OUT_OF_TIME = 3   # a user whose status could never be worked out is out of time after this many ticks
MAX_AUTO_POLICIES = 200        # default policies built on demand for unlisted users (manage_unknown_users)
LIMIT_SCOPES = ("today", "weekday", "weekend", "weekly")
# the limiting factors more minutes can lift (the others: hours, bedtime, manual, error)
BONUS_FACTORS = ("daily", "weekly")

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
    """Older versions skipped scheduled focus for a whole day with this key; it is no
    longer written or read (see SETTING_FOCUS_SKIP_UNTIL) and old ones are purged."""
    return f"focus_skip:{day.isoformat()}"


def external_key(day: date) -> str:
    """Seconds used on the other PCs on ``day``, as last known (shared budgets)."""
    return f"external:{day.isoformat()}"


def external_week_key(week_start: date) -> str:
    """... and in the week that began on ``week_start``."""
    return f"external_week:{week_start.isoformat()}"


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
    agentless_since: Optional[float] = None   # counting during focus without any agent report since
    focus_agentless: bool = False             # ... for longer than AGENT_GRACE_SECONDS: focus no longer pauses the budget
    reported_since: Optional[float] = None    # ... and since when every counting seat reports again (see FOCUS_TRUST_SECONDS)
    last_status: Optional["TimeStatus"] = None   # the last status that could be worked out, and when (wall time):
    last_status_at: Optional[float] = None       # enforced from while status() fails
    status_failures: int = 0                  # status() failures in a row

def wall_seconds_ahead(now: datetime, minutes: Optional[float]) -> Optional[float]:
    """``minutes`` on the wall clock from ``now`` (as ``window_status`` counts them) in real
    seconds.  The two differ on the nights the clocks change: at 01:30 on the spring-forward
    night "until 04:00" is 150 wall minutes but only 90 real ones, so a warning worked out
    from minutes of the day would come an hour late.  The service clock always carries a
    zone (configured, pinned, or the PC's local one - ``util.make_clock``); a naive ``now``
    (an injected test clock) is taken as it is (minutes * 60)."""
    if minutes is None:
        return None
    if now.tzinfo is None:
        return minutes * 60
    minute = now.hour * 60 + now.minute + now.second / 60
    end = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo) + timedelta(minutes=minute + minutes)
    now_ts = now.timestamp()
    ahead = end.timestamp() - now_ts
    if ahead < 0:   # now is in the repeated hour of a fall-back night: the end is its second occurrence
        ahead = end.replace(fold=1).timestamp() - now_ts
    return max(0.0, ahead)


def _unlimited(minutes: Optional[float]) -> float:
    """A limit as a comparable number (None = no limit = infinitely many minutes)."""
    return math.inf if minutes is None else float(minutes)


def bedtime_windows(policy: UserPolicy, day: date, ext_minutes: int, prev_ext_minutes: int) -> list[Window]:
    """Allowed windows for ``day`` derived from the bedtime schedule.

    Allowed = [wake, bedtime + extension).  A bedtime at or before the wake
    time (e.g. bed 01:00, wake 07:00) means "past midnight"; the part after
    midnight spills into the next day, as does an extension that crosses it.

    The hours before the wake time belong to the night before: they are closed
    only when *yesterday* had a bedtime.  After an evening without one (bedtime
    on school nights only, say) the night simply runs on until today's bedtime -
    it does not end at midnight because today happens to have a wake time.
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
    wake = policy.wake_time.get(wd, 0)
    if bed is None:
        # no bedtime today: allowed for the rest of the day - from the wake time when
        # last night had a bedtime (that night does not end at midnight either)
        if pbed is None:
            return [(0, 1440)]
        out.append((wake, 1440))
        return sorted(set(w for w in out if w[1] > w[0]))
    end = bed + ext_minutes
    if bed <= wake:
        end = bed + 1440 + ext_minutes  # bedtime is after midnight
    out.append((wake if pbed is not None else 0, min(1440, end)))
    return sorted(set(w for w in out if w[1] > w[0]))


class TimeKeeper:
    def __init__(self, cfg: TimeLimitsConfig, store: Store, build_default_policy=None):
        self.cfg = cfg
        self.store = store
        self._policies: dict[str, UserPolicy] = {p.key: p for p in cfg.users}
        self._build_default_policy = build_default_policy
        self._runtime: dict[str, _Runtime] = {}
        self._auto_policies: set[str] = set()   # keys in _policies that were built from default_policy
        self.external_usage: Optional[ExternalUsage] = None
        # optional: () -> bool, "the other PCs' usage is known" (FleetTracker.ready); see _external_ready
        self.external_ready: Optional[Callable[[], bool]] = None
        # optional: (user, day) -> {"bonus_seconds", "bedtime_extension_minutes"} granted on the
        # other PCs (FleetTracker.external_extras); used with shared_budget only
        self.external_extras: Optional[Callable[[str, date], dict]] = None
        self._external_warned: Optional[float] = None
        self._today: Optional[date] = None      # "today" by the service clock, as of the last tick / status
        self._tick_errors: dict[str, float] = {}  # user -> monotonic time its failing tick was last logged
        # optional: the service clock ("now"); a grant asks it whether it can help (_reset_after_grant).
        # Without one the moment of the last tick is taken.
        self.clock: Optional[Callable[[], datetime]] = None
        self._last_now: Optional[datetime] = None
        stored = store.get_setting(Store.GLOBAL, SETTING_GLOBAL_ENABLED)
        self.global_enabled: bool = bool(cfg.enabled if stored is None else stored)

    def update_config(self, cfg: TimeLimitsConfig, build_default_policy=None) -> None:
        """Hot reload: swap policies, keep runtime counters.

        A user who was login-blocked and is no longer managed after the reload
        (removed or made ``unlimited``) gets one "unblock" action on the next tick.

        ``time_limits.enabled`` applies as at start: unless the "time limits" switch was
        set from Home Assistant (that is stored and wins), the reloaded value is in force."""
        self.cfg = cfg
        self._policies = {p.key: p for p in cfg.users}
        self._auto_policies = set()
        if build_default_policy is not None:
            self._build_default_policy = build_default_policy
        if self.store.get_setting(Store.GLOBAL, SETTING_GLOBAL_ENABLED) is None:
            self.global_enabled = bool(cfg.enabled)

    # -- policies -------------------------------------------------------------
    def policy_for(self, user: str, create: bool = True) -> Optional[UserPolicy]:
        """The policy for ``user``, or None when the user is not managed.

        With ``manage_unknown_users`` an unlisted user gets a policy built from
        ``default_policy``.  ``create=False`` is for read-only lookups of a name
        that came from outside (an API path): it only returns what already
        exists and never adds an entry.  The number of on-demand policies that
        are remembered is capped as well."""
        policy = self._base_policy(user, create)
        if policy is None or not policy.profiles:
            return policy
        # the active profile (holiday, school day ...) applied; None / unknown: the policy as configured
        name = self.store.get_setting(user, SETTING_PROFILE)
        return policy.profiles.get(name, policy) if isinstance(name, str) else policy

    def _base_policy(self, user: str, create: bool = True) -> Optional[UserPolicy]:
        """The policy as configured, whatever profile is active."""
        key = user.lower()
        policy = self._policies.get(key)
        if policy is None and create and self.cfg.manage_unknown_users and self._build_default_policy is not None:
            policy = self._build_default_policy(user)
            if len(self._auto_policies) < MAX_AUTO_POLICIES:
                self._policies[key] = policy
                self._auto_policies.add(key)
        return policy

    # -- profiles (holiday / school day) ---------------------------------------------
    def profile_names(self, user: str) -> list[str]:
        """The profiles configured for ``user``, sorted (without "normal")."""
        policy = self._base_policy(user)
        return sorted(policy.profiles) if policy is not None and not policy.unlimited else []

    def active_profile(self, user: str) -> Optional[str]:
        """The profile in force, None for the normal policy.  It is a stored setting: it
        survives restarts and reloads; a name that no longer exists counts as None."""
        name = self.store.get_setting(user, SETTING_PROFILE)
        return name if isinstance(name, str) and name in self.profile_names(user) else None

    def set_profile(self, user: str, name: Optional[str]) -> None:
        """Switch ``user`` to a profile; ``None`` or "normal" returns to the normal policy.
        ``ValueError`` for a name the user does not have."""
        wanted = self._profile_name(user, name)
        before = self.active_profile(user)
        if wanted == before:
            return
        loosens = self._profile_loosens(user, wanted, self._today or date.today())
        if wanted is None:
            self.store.delete_setting(user, SETTING_PROFILE)
        else:
            self.store.set_setting(user, SETTING_PROFILE, wanted)
        self.store.log_event(user, "profile", wanted or NORMAL_PROFILE)
        if loosens:
            self._reset_after_grant(user)

    def is_loosening_profile(self, user: str, name: Optional[str], now: datetime) -> bool:
        """Would switching to ``name`` give the user more than the profile in force does:
        more minutes on some day of the week or per week, more to carry over, wider
        allowed hours or a later bedtime?  (Every day of the week is compared, not only
        today: what is not looser today but is tomorrow needs the PIN just the same.)
        Callers gate a loosening switch behind the parent PIN; an unknown name counts
        as loosening."""
        try:
            wanted = self._profile_name(user, name)
        except ValueError:
            return True
        return self._profile_loosens(user, wanted, now.date())

    def _profile_name(self, user: str, name: Optional[str]) -> Optional[str]:
        wanted = str(name).strip().lower() if name is not None else ""
        if wanted in ("", NORMAL_PROFILE):
            return None
        if wanted not in self.profile_names(user):
            raise ValueError(f"{user} has no profile {name!r}")
        return wanted

    def _profile_loosens(self, user: str, wanted: Optional[str], day: date) -> bool:
        base = self._base_policy(user)
        if base is None or base.unlimited:
            return False
        old = base.profiles.get(self.active_profile(user) or "", base)
        new = base.profiles.get(wanted or "", base)
        if new is old:
            return False
        if _unlimited(new.weekly_limit_minutes) > _unlimited(old.weekly_limit_minutes):
            return True
        if new.rollover_max_minutes > old.rollover_max_minutes:
            return True
        for offset in range(7):
            d = day + timedelta(days=offset)
            wd = d.weekday()
            if _unlimited(new.daily_limits.get(wd)) > _unlimited(old.daily_limits.get(wd)):
                return True
            if not windows_cover(old.allowed_windows.get(wd, []), new.allowed_windows.get(wd, [])):
                return True
            if not windows_cover(bedtime_windows(old, d, 0, 0), bedtime_windows(new, d, 0, 0)):
                return True
        return False

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
            for user in list(self._policies):
                policy = self.policy_for(user)
                # only users whose bedtime is extended by a parent being home gained anything
                if policy and not policy.unlimited and policy.has_bedtime and policy.bedtime_parent_extension_minutes > 0:
                    self._reset_after_grant(user)

    def limit_enabled(self, user: str) -> bool:
        return bool(self.store.get_setting(user, SETTING_LIMIT_ENABLED, True))

    def set_limit_enabled(self, user: str, enabled: bool) -> None:
        self.store.set_setting(user, SETTING_LIMIT_ENABLED, bool(enabled))
        self.store.log_event(user, "limit_enabled", str(bool(enabled)))

    def manually_blocked(self, user: str) -> bool:
        return bool(self.store.get_setting(user, SETTING_BLOCKED, False))

    def set_manually_blocked(self, user: str, blocked: bool) -> None:
        was_blocked = self.manually_blocked(user)
        self.store.set_setting(user, SETTING_BLOCKED, bool(blocked))
        self.store.log_event(user, "manual_block", str(bool(blocked)))
        if was_blocked and not blocked:
            self._reset_after_grant(user)

    def local_bonus_seconds(self, user: str, day: date) -> int:
        """The bonus granted on *this* PC for ``day`` (what the setters below change)."""
        return int(self.store.get_setting(user, bonus_key(day), 0) or 0)

    def bonus_seconds(self, user: str, day: date) -> int:
        """The bonus that counts for ``day``: this PC's and, with a shared budget, what
        was granted on the other PCs."""
        return self.local_bonus_seconds(user, day) + int(self._extras(user, day)["bonus_seconds"])

    def week_bonus_seconds(self, user: str, day: date) -> int:
        """The bonus granted from the first day of ``day``'s week up to ``day``.  A bonus is
        more time against the weekly limit as well: "30 more minutes" must not be swallowed
        by a weekly limit that is used up (each day's bonus as stored for that day)."""
        start = self.week_start(day)
        return sum(self.bonus_seconds(user, start + timedelta(days=i)) for i in range((day - start).days + 1))

    def _extras(self, user: str, day: date) -> dict:
        """Bonus and bedtime extension granted on the other PCs (shared budgets):
        ``{"bonus_seconds", "bedtime_extension_minutes"}``.  A provider that fails or
        answers nonsense counts as no extras - a missing bonus is the safe side.  The
        bonus may be negative (a parent took time away on another PC); the status keeps
        the day's allowance from going below nothing."""
        out = {"bonus_seconds": 0.0, "bedtime_extension_minutes": 0}
        if self.external_extras is None or not self.cfg.shared_budget:
            return out
        try:
            got = self.external_extras(user, day) or {}
            bonus, ext = float(got.get("bonus_seconds", 0) or 0), float(got.get("bedtime_extension_minutes", 0) or 0)
        except Exception:  # noqa: BLE001
            log.debug("external extras provider failed for %s", user, exc_info=True)
            return out
        if math.isfinite(bonus) and bonus != 0:
            out["bonus_seconds"] = clamp(bonus, -86400.0, 86400.0)
        if math.isfinite(ext) and ext > 0:
            out["bedtime_extension_minutes"] = int(min(ext, 1440))
        return out

    # The setters below restart the grace period / forget an enforcement only
    # when the change really gives the user more time.  Re-sending the same
    # value or a lower one needs no parent PIN, so it must not buy another
    # grace period either.
    def add_bonus_minutes(self, user: str, day: date, minutes: float, reason: str = "") -> int:
        """Add to this PC's bonus for ``day``; returns this PC's new total in seconds."""
        before = self.local_bonus_seconds(user, day)
        total = before + int(round(minutes * 60))
        self.store.set_setting(user, bonus_key(day), total)
        self.store.log_event(user, "bonus", f"{minutes:+} min -> {total // 60} min" + (f" ({reason})" if reason else ""))
        if total > before:
            self._reset_after_grant(user)
        return total

    def set_bonus_minutes(self, user: str, day: date, minutes: float) -> None:
        """Set this PC's bonus for ``day`` (what other PCs granted is not touched)."""
        before = self.local_bonus_seconds(user, day)
        total = int(round(minutes * 60))
        self.store.set_setting(user, bonus_key(day), total)
        self.store.log_event(user, "bonus_set", f"{minutes} min")
        if total > before:
            self._reset_after_grant(user)

    # Every limit override below can hold three different things, and "no limit" is
    # not the same as "no override":
    #
    # * ``minutes >= 0`` - that many minutes (for a daily limit 0 means no time at all);
    # * ``NO_LIMIT`` (stored as -1; any negative number is read the same way) - *no
    #   limit*, whatever the configuration says.  For the weekly limit 0 means this too;
    # * ``None`` - remove the override: the configured limit applies again.
    @staticmethod
    def _override_value(minutes: int) -> int:
        return NO_LIMIT if int(minutes) < 0 else int(minutes)

    def set_limit_today(self, user: str, day: date, minutes: Optional[int]) -> None:
        """Override the daily limit for ``day`` only: minutes, ``NO_LIMIT`` (no limit
        that day) or ``None`` (remove the override)."""
        before = self._daily_limits_from(user, day)
        if minutes is None:
            self.store.delete_setting(user, limit_key(day))
        else:
            self.store.set_setting(user, limit_key(day), self._override_value(minutes))
        self.store.log_event(user, "limit_today", str(minutes))
        self._reset_if_raised(user, before, self._daily_limits_from(user, day))

    def set_weekday_limit(self, user: str, minutes: Optional[int]) -> None:
        """Override the daily limit of Monday to Friday: minutes, ``NO_LIMIT`` or ``None`` (remove)."""
        before = self._daily_limits_from(user)
        if minutes is None:
            self.store.delete_setting(user, SETTING_WEEKDAY_LIMIT)
        else:
            self.store.set_setting(user, SETTING_WEEKDAY_LIMIT, self._override_value(minutes))
        self._reset_if_raised(user, before, self._daily_limits_from(user))

    def set_weekend_limit(self, user: str, minutes: Optional[int]) -> None:
        """Override the daily limit of Saturday and Sunday: minutes, ``NO_LIMIT`` or ``None`` (remove)."""
        before = self._daily_limits_from(user)
        if minutes is None:
            self.store.delete_setting(user, SETTING_WEEKEND_LIMIT)
        else:
            self.store.set_setting(user, SETTING_WEEKEND_LIMIT, self._override_value(minutes))
        self._reset_if_raised(user, before, self._daily_limits_from(user))

    def set_weekly_limit(self, user: str, minutes: Optional[int]) -> None:
        """Override the weekly limit: ``minutes > 0`` is the budget; ``0`` or ``NO_LIMIT``
        means *no weekly limit* although the configuration has one; ``None`` removes
        the override (back to the configured weekly limit)."""
        before = self.weekly_limit_minutes(user)
        if minutes is None:
            self.store.delete_setting(user, SETTING_WEEKLY_LIMIT)
        else:
            self.store.set_setting(user, SETTING_WEEKLY_LIMIT, max(0, int(minutes)))
        self.store.log_event(user, "weekly_limit", str(minutes))
        if _unlimited(self.weekly_limit_minutes(user)) > _unlimited(before):
            self._reset_after_grant(user)

    def _daily_limits_from(self, user: str, day: Optional[date] = None) -> list[Optional[int]]:
        """The effective daily limit for seven consecutive days (from ``day``, default
        today by the service clock - the day of the last tick or status)."""
        start = day or self._today or date.today()
        return [self.effective_limit_minutes(user, start + timedelta(days=i)) for i in range(7)]

    def _reset_if_raised(self, user: str, before: list[Optional[int]], after: list[Optional[int]]) -> None:
        if any(_unlimited(new) > _unlimited(old) for old, new in zip(before, after)):
            self._reset_after_grant(user)

    def is_loosening_limit(self, user: str, scope: str, minutes: Optional[int], day: date) -> bool:
        """Would storing ``minutes`` for ``scope`` give the user more time than now?

        ``scope`` is ``today`` | ``weekday`` | ``weekend`` | ``weekly`` and the
        comparison is against the current value of *that* scope.  ``None``
        (drop the override) counts as loosening, and so does a weekly value
        ``<= 0``, which is stored as "no weekly limit".  Callers gate loosening
        behind the parent PIN; an unknown scope is treated as loosening.
        """
        if scope == "weekly":
            current = self.weekly_limit_minutes(user)
            if current is None:
                return False  # no weekly limit: nothing can loosen it further
            return minutes is None or int(minutes) <= 0 or int(minutes) > current
        if minutes is None or scope not in LIMIT_SCOPES:
            return True
        if scope == "today":
            current_values = [self.effective_limit_minutes(user, day)]
        else:
            # the days of that group, as they stand without a one-day override
            policy = self.policy_for(user)
            key = SETTING_WEEKEND_LIMIT if scope == "weekend" else SETTING_WEEKDAY_LIMIT
            override = self.store.get_setting(user, key)
            weekdays = (5, 6) if scope == "weekend" else (0, 1, 2, 3, 4)
            if policy is None or policy.unlimited:
                current_values = [None]
            elif override is not None:
                current_values = [None if int(override) < 0 else int(override)]
            else:
                current_values = [policy.daily_limits.get(wd) for wd in weekdays]
        wanted = math.inf if int(minutes) < 0 else int(minutes)   # NO_LIMIT
        return any(wanted > _unlimited(current) for current in current_values)

    # -- bedtime ------------------------------------------------------------------
    def local_bedtime_extension_minutes(self, user: str, night: date) -> int:
        """The manual bedtime extension set on *this* PC for ``night``."""
        return int(self.store.get_setting(user, bedtime_ext_key(night), 0) or 0)

    def bedtime_extension_minutes(self, user: str, day: date) -> int:
        """The manual extension that counts: this PC's or, with a shared budget, a
        longer one set on another PC."""
        return max(self.local_bedtime_extension_minutes(user, day),
                   int(self._extras(user, day)["bedtime_extension_minutes"]))

    def set_bedtime_extension(self, user: str, day: date, minutes: int, now: Optional[datetime] = None) -> None:
        before = self.local_bedtime_extension_minutes(user, day)
        self.store.set_setting(user, bedtime_ext_key(day), int(max(0, minutes)))
        self.store.log_event(user, "bedtime_extension", f"{int(minutes)} min")
        if self.local_bedtime_extension_minutes(user, day) > before:
            self._reset_after_grant(user, now)

    def bedtime_day(self, user: str, now: datetime) -> date:
        """The day key a bedtime extension granted at ``now`` belongs to.

        Normally today.  After midnight it is *yesterday* while last night's
        bedtime is the one in play: we are inside the after-midnight part of
        yesterday's (extended) bedtime, or it is still before today's wake
        time.  Extending "today" then would only move tonight's bedtime and do
        nothing for the child who is still up.
        """
        today = now.date()
        policy = self.policy_for(user)
        if policy is None or policy.unlimited or not policy.has_bedtime:
            return today
        yesterday = today - timedelta(days=1)
        bed = policy.bedtime.get(yesterday.weekday())
        if bed is None:
            return today
        minute = now.hour * 60 + now.minute + now.second / 60
        end = bed + self._bedtime_ext(user, policy, yesterday)[2]
        if bed <= policy.wake_time.get(yesterday.weekday(), 0):
            end += 1440  # that bedtime is after midnight
        if minute < end - 1440 or minute < policy.wake_time.get(today.weekday(), 0):
            return yesterday
        return today

    def extend_bedtime(self, user: str, day: date, minutes: int, now: Optional[datetime] = None) -> int:
        """Give the night of ``day`` ``minutes`` more; returns this PC's new extension.

        It is added to the extension that counts - with a shared budget a longer one set
        on another PC, not only this PC's own - so "+30" always moves bedtime by 30
        minutes.  With ``now``, a night whose bedtime has already passed (also after
        midnight, before the wake time) is extended from ``now``: "+30" at 02:00 means
        until 02:30, not 30 minutes after a bedtime that was hours ago."""
        base = self.bedtime_extension_minutes(user, day)
        if now is not None:
            base = max(base, self._extension_reaching(user, day, now))
        total = base + int(minutes)
        self.set_bedtime_extension(user, day, total, now)
        return total

    def _extension_reaching(self, user: str, night: date, now: datetime) -> int:
        """The manual extension of the night of ``night`` that makes it end at ``now`` (0 when
        bedtime is still ahead).  The automatic extension (a parent at home) is counted in."""
        policy = self.policy_for(user)
        if policy is None or policy.unlimited or not policy.has_bedtime:
            return 0
        bed = policy.bedtime.get(night.weekday())
        if bed is None:
            return 0
        if bed <= policy.wake_time.get(night.weekday(), 0):
            bed += 1440   # that bedtime is after midnight
        # minutes from the night's midnight to now (wall time, as bedtimes are)
        minute = (now.date() - night).days * 1440 + now.hour * 60 + now.minute + now.second / 60
        auto = self._bedtime_ext(user, policy, night)[1]
        return max(0, math.ceil(minute - bed - auto))

    def _bedtime_ext(self, user: str, policy: UserPolicy, day: date) -> tuple[int, int, int]:
        """(manual, automatic, total) extension minutes for ``day``."""
        manual = self.bedtime_extension_minutes(user, day)
        auto = policy.bedtime_parent_extension_minutes if (self.parent_present and policy.bedtime_parent_extension_minutes > 0) else 0
        return manual, auto, manual + auto

    # -- focus mode ------------------------------------------------------------------
    def focus_state(self, user: str, policy: UserPolicy, now: datetime) -> tuple[bool, Optional[float], Optional[str]]:
        """(active, until_ts, source) where source is manual | schedule."""
        now_ts = now.timestamp()
        until = self._stored_time(user, SETTING_FOCUS_UNTIL)
        if until is not None:
            # A timer further away than any that can be started (a stored value from an
            # older version, or the clock was set back) ends after the longest period.
            if until > now_ts + MAX_FOCUS_MINUTES * 60:
                until = now_ts + MAX_FOCUS_MINUTES * 60
                try:
                    self.store.set_setting(user, SETTING_FOCUS_UNTIL, until)
                except Exception:  # noqa: BLE001
                    log.debug("could not shorten the focus timer of %s", user, exc_info=True)
            if now_ts < until:
                return True, until, "manual"
            try:
                self.store.delete_setting(user, SETTING_FOCUS_UNTIL)
                self.store.delete_setting(user, SETTING_FOCUS_ALLOW)
            except Exception:  # noqa: BLE001 - an expired timer is harmless; do not break the tick over it
                log.debug("could not clear the expired focus timer for %s", user, exc_info=True)
        if policy.focus.scheduled:
            until_ts = self._scheduled_focus_until(policy, now)
            if until_ts is not None:
                # the window that was ended by hand stays ended; the next one starts as usual
                skip = self._stored_time(user, SETTING_FOCUS_SKIP_UNTIL)
                if skip is not None and not now_ts < skip <= now_ts + 2 * 86400:  # over, or not believable
                    skip = None
                    try:
                        self.store.delete_setting(user, SETTING_FOCUS_SKIP_UNTIL)
                    except Exception:  # noqa: BLE001
                        log.debug("could not clear the focus skip for %s", user, exc_info=True)
                if skip is None:
                    return True, until_ts, "schedule"
        return False, None, None

    def _stored_time(self, user: str, key: str) -> Optional[float]:
        """A timestamp setting, or None when it is not set or not a usable number."""
        value = self.store.get_setting(user, key)
        if value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _scheduled_focus_until(policy: UserPolicy, now: datetime) -> Optional[float]:
        """When the scheduled focus window ``now`` falls in ends (a timestamp), or None
        outside every window.  A window that wraps past midnight ("22:00-01:00") ends
        on the next day, not at midnight."""
        weekday = now.weekday()
        minute = now.hour * 60 + now.minute + now.second / 60
        windows = policy.focus.schedule.get(weekday, [])
        if not windows:  # (no windows today means "no focus", unlike allowed-hours semantics)
            return None
        tomorrow = policy.focus.schedule.get((weekday + 1) % 7, [])
        inside, remaining = window_status(minute, windows, tomorrow or None)
        if not inside:
            return None
        return now.timestamp() + (wall_seconds_ahead(now, remaining) or 0.0)

    def focus_extra_allow(self, user: str) -> list[str]:
        value = self.store.get_setting(user, SETTING_FOCUS_ALLOW, [])
        return [str(v) for v in (value or [])]

    def start_focus(self, user: str, now: datetime, minutes: Optional[int] = None,
                    allow: Optional[list[str]] = None) -> float:
        """Start a focus period of ``minutes`` (default: the user's ``focus.default_minutes``),
        at least 1 and at most ``MAX_FOCUS_MINUTES``; returns when it ends."""
        policy = self.policy_for(user)
        default = policy.focus.default_minutes if policy else 60
        try:
            mins = float(minutes) if minutes else float(default)
        except (TypeError, ValueError):
            mins = float(default)
        if not math.isfinite(mins):
            mins = float(default)
        mins = int(clamp(mins, 1, MAX_FOCUS_MINUTES))
        until = now.timestamp() + mins * 60
        self.store.set_setting(user, SETTING_FOCUS_UNTIL, until)
        if allow:
            self.store.set_setting(user, SETTING_FOCUS_ALLOW, [str(a).lower() for a in allow])
        else:
            self.store.delete_setting(user, SETTING_FOCUS_ALLOW)
        self.store.log_event(user, "focus_start", f"{mins} min")
        return until

    def stop_focus(self, user: str, now: datetime) -> None:
        """End focus mode now.  During a scheduled window only *that* window is ended
        (to its end, also when it runs past midnight); later ones start as planned."""
        policy = self.policy_for(user)
        self.store.delete_setting(user, SETTING_FOCUS_UNTIL)
        self.store.delete_setting(user, SETTING_FOCUS_ALLOW)
        if policy is not None and policy.focus.scheduled:
            until_ts = self._scheduled_focus_until(policy, now)
            if until_ts is not None:
                self.store.set_setting(user, SETTING_FOCUS_SKIP_UNTIL, until_ts)
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

    def _reset_after_grant(self, user: str, now: Optional[datetime] = None) -> None:
        """A grant gave ``user`` more: restart the grace period and forget an enforcement -
        but only when the grant can change anything.  While the allowed hours, bedtime or a
        pause keep the user off, more minutes (or a looser daily limit) do not let the user
        back on, and restarting the countdown would only buy the user another grace period
        (and a parent PIN entry for a bonus that cannot help would do just that).  The grant
        itself is stored all the same; it counts once the hours or bedtime allow it.

        The verdict is the status *after* the grant: no longer out of time, or out of time
        on the daily or weekly budget (what minutes can lift) - "hours", "bedtime",
        "manual" or a status that cannot be worked out ("error") keep the countdown going.
        ``now`` defaults to the clock (:attr:`clock`) or the moment of the last tick."""
        rt = self._runtime.get(user.lower())
        if not rt:
            return
        if now is None:
            now = self._clock_now()
        if now is not None:
            try:
                st = self.status(user, now)
            except Exception:  # noqa: BLE001 - cannot tell: the countdown goes on (fails closed)
                log.debug("status of %s after a grant could not be worked out", user, exc_info=True)
                return
            if st.out_of_time and st.limiting_factor not in BONUS_FACTORS:
                log.info("grant for %s stored; the grace period goes on (%s keeps the user off)",
                         user, st.limiting_factor)
                return
        rt.grace_started = None
        rt.enforced = False

    def _clock_now(self) -> Optional[datetime]:
        """"Now" for a grant: the service clock when one was attached, else the last tick's."""
        if self.clock is not None:
            try:
                return self.clock()
            except Exception:  # noqa: BLE001
                log.debug("the clock attached to the timekeeper failed", exc_info=True)
        return self._last_now

    # -- computed status -----------------------------------------------------
    def effective_limit_minutes(self, user: str, day: date) -> Optional[int]:
        policy = self.policy_for(user)
        if policy is None or policy.unlimited:
            return None
        override = self.store.get_setting(user, limit_key(day))
        if override is not None:
            return None if int(override) < 0 else int(override)   # NO_LIMIT: no limit that day
        weekday = day.weekday()
        group_key = SETTING_WEEKEND_LIMIT if weekday >= 5 else SETTING_WEEKDAY_LIMIT
        group_override = self.store.get_setting(user, group_key)
        if group_override is not None:
            return None if int(group_override) < 0 else int(group_override)
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
            if self.external_usage is not None and self.cfg.shared_budget:
                # A shared budget was also spent on the other PCs.  When their
                # figures are not known (yet), carry nothing rather than a full
                # day, and do not store that: it is worked out again later.
                y_external = self._external_day(user, yesterday)
                if y_external is None:
                    return 0
                y_used += y_external
            carry = int(clamp(y_limit * 60 + y_bonus + y_roll - y_used, 0, policy.rollover_max_minutes * 60))
        try:
            self.store.set_setting(user, key, carry)
        except Exception:  # noqa: BLE001 - worked out again on the next call; never break the tick
            log.debug("could not store the rollover for %s", user, exc_info=True)
            return carry
        if carry:
            self.store.log_event(user, "rollover", f"{carry // 60} min carried from {yesterday.isoformat()}")
        return carry

    def _external_ready(self) -> bool:
        """Are the other PCs' figures known?  Uses ``external_ready`` when set,
        else the ``ready()`` of the object providing ``external_usage`` (the
        FleetTracker), else assumes yes."""
        ready = self.external_ready
        if ready is None:
            ready = getattr(getattr(self.external_usage, "__self__", None), "ready", None)
        if ready is None:
            return True
        try:
            return bool(ready())
        except Exception:  # noqa: BLE001
            return False

    def _external_day(self, user: str, day: date) -> Optional[float]:
        """Seconds used on other PCs on one (past) day, or None when unknown."""
        if not self._external_ready():
            return None
        try:
            day_s, range_s = self.external_usage(user, day, day)
            value = max(float(day_s), float(range_s))
        except Exception:  # noqa: BLE001
            log.warning("external usage provider failed for %s on %s", user, day.isoformat(), exc_info=True)
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return max(value, self._known_external(user, external_key(day)))

    def _known_external(self, user: str, key: str) -> float:
        """Seconds on the other PCs as written down earlier under ``key`` (0 when nothing was)."""
        try:
            value = float(self.store.get_setting(user, key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return min(value, 7 * 86400.0) if math.isfinite(value) and value > 0 else 0.0

    def _remember_external(self, user: str, key: str, seconds: float) -> None:
        """Write down what the other PCs reported (only ever more, and not on every
        tick), so a restart without a network does not start from nothing."""
        known = self._known_external(user, key)
        seconds = min(float(seconds), 7 * 86400.0)
        if seconds <= known or (known > 0 and seconds < known + EXTERNAL_SAVE_STEP_SECONDS):
            return
        try:
            self.store.set_setting(user, key, round(seconds))
        except Exception:  # noqa: BLE001 - tried again when the figure grows; never break the tick
            log.debug("could not store the usage of %s on other PCs", user, exc_info=True)

    def _external(self, user: str, day: date) -> tuple[float, float]:
        """Seconds used on the other PCs today and this week (shared budgets).

        Fails closed: the figures are written to the store as they come in, and what
        counts is the larger of "what the other PCs say now" and "what was last
        known".  A PC that was restarted without a network, a peer that vanished or
        a provider that fails therefore never hands the shared budget back."""
        if self.external_usage is None or not self.cfg.shared_budget:
            return 0.0, 0.0
        week_start = self.week_start(day)
        day_key, week_key = external_key(day), external_week_key(week_start)
        today_s = week_s = 0.0
        try:
            live_today, live_week = self.external_usage(user, day, week_start)
            live_today, live_week = float(live_today), float(live_week)
            if not (math.isfinite(live_today) and math.isfinite(live_week)):
                raise ValueError(f"non-finite external usage ({live_today}, {live_week})")
            today_s, week_s = max(0.0, live_today), max(0.0, live_week)
            self._remember_external(user, day_key, min(today_s, 86400.0))
            self._remember_external(user, week_key, week_s)
        except Exception as exc:  # noqa: BLE001
            now_mono = time.monotonic()
            if self._external_warned is None or now_mono - self._external_warned >= 60:
                self._external_warned = now_mono
                log.warning("external usage provider failed (%s); using the last known values for %s", exc, user,
                            exc_info=True)
        today_s = max(today_s, self._known_external(user, day_key))
        week_s = max(week_s, today_s, self._known_external(user, week_key))
        return today_s, week_s

    def status(self, user: str, now: datetime) -> TimeStatus:
        key = user.lower()
        policy = self.policy_for(user)
        day = now.date()
        self._today = day
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
        # (a negative bonus - taken away, possibly on another PC - can use up the day, no more)
        remaining = None if limit is None else max(0.0, limit * 60 + bonus + rollover) - used_today
        # weekly: the bonus granted this week is more time against the weekly limit too ----
        weekly_limit = self.weekly_limit_minutes(key)
        week_start = self.week_start(day)
        week_used = self.store.usage_range(key, week_start, day).active + ext_week
        weekly_remaining = None if weekly_limit is None else \
            max(0.0, weekly_limit * 60 + self.week_bonus_seconds(key, day)) - week_used
        # allowed hours -------------------------------------------------------------------
        weekday = day.weekday()
        windows = policy.allowed_windows.get(weekday, [])
        next_windows = policy.allowed_windows.get((weekday + 1) % 7, [])
        minute = now.hour * 60 + now.minute + now.second / 60
        in_window, win_remaining_min = window_status(minute, windows, next_windows)
        win_remaining = wall_seconds_ahead(now, win_remaining_min)   # real seconds, also on a DST night
        # bedtime ------------------------------------------------------------------------
        ext_manual, ext_auto, ext_total = self._bedtime_ext(key, policy, day)
        bed_windows = bedtime_windows(policy, day, ext_total, self._bedtime_ext(key, policy, day - timedelta(days=1))[2])
        tomorrow = day + timedelta(days=1)
        bed_windows_next = bedtime_windows(policy, tomorrow, self._bedtime_ext(key, policy, tomorrow)[2], ext_total)
        if policy.has_bedtime:
            in_bed, bed_remaining_min = window_status(minute, bed_windows, bed_windows_next)
        else:
            in_bed, bed_remaining_min = True, None
        bed_remaining = wall_seconds_ahead(now, bed_remaining_min)
        bed_minute = policy.bedtime.get(weekday)
        if bed_minute is not None:
            wake_minute = policy.wake_time.get(weekday, 0)
            if bed_minute <= wake_minute:
                bed_minute += 1440
            bed_minute += ext_total
        # focus ---------------------------------------------------------------------------
        focus_active, focus_until, focus_source = self.focus_state(key, policy, now)
        # focus only pauses the budget while an agent vouches for what is on screen (see tick)
        focus_pauses = bool(focus_active and not policy.focus.counts_toward_limit and not (rt and rt.focus_agentless))
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

    def _elapsed(self, rt: _Runtime, now_ts: float) -> float:
        """Seconds to account for since this user's previous tick (and move the reference).

        A clock that went backwards yields 0.  A hole longer than
        ``MAX_TICK_GAP_SECONDS`` - the PC slept, or the user was not being
        ticked at all - is not screen time: it counts as one ordinary tick."""
        last, rt.last_tick = rt.last_tick, now_ts
        if last is None or now_ts <= last:
            return 0.0
        gap = now_ts - last
        if gap > MAX_TICK_GAP_SECONDS:
            return max(0.0, min(float(self.cfg.tick_seconds), MAX_TICK_GAP_SECONDS))
        return gap

    @staticmethod
    def _watch_focus_agent(rt: _Runtime, focus_pauses: bool, counting: list[SessionInfo], now_ts: float) -> None:
        """Focus mode is enforced through the agent.  A session nobody reports on (agent
        killed or suspended) is not "doing homework": after a short allowance for logins
        and relaunches (AGENT_GRACE_SECONDS) its time counts and the limits apply as usual.

        That verdict sticks: an agent that comes back - the watchdog starts a new one
        for a suspended agent within a minute or two - does not make focus pause the
        budget again at once, or suspending the agent over and over would flip the user
        between "out of time" and "has time" (and the grace period, the login block with
        it) without ever reaching the logoff.  Only FOCUS_TRUST_SECONDS with every
        counting seat reporting throughout, or the end of focus mode, give it back."""
        if not focus_pauses:
            rt.agentless_since = rt.reported_since = None
            rt.focus_agentless = False
            return
        if any(s.report is None for s in counting):
            rt.reported_since = None
            if rt.agentless_since is None or now_ts < rt.agentless_since:
                rt.agentless_since = now_ts
            if now_ts - rt.agentless_since >= AGENT_GRACE_SECONDS:
                rt.focus_agentless = True
            return
        rt.agentless_since = None
        if not rt.focus_agentless:
            return
        if not counting:
            rt.reported_since = None   # nothing on screen (locked, away): no report vouches for anything
        elif rt.reported_since is None or now_ts < rt.reported_since:
            rt.reported_since = now_ts
        elif now_ts - rt.reported_since >= FOCUS_TRUST_SECONDS:
            rt.focus_agentless = False
            rt.reported_since = None

    def _log_user_error(self, user: str, message: str, *args: object) -> None:
        """Log a failure for one user with its traceback - not on every tick (TICK_ERROR_LOG_SECONDS)."""
        now_mono = time.monotonic()
        last = self._tick_errors.get(user)
        if last is None or now_mono - last >= TICK_ERROR_LOG_SECONDS or now_mono < last:
            if len(self._tick_errors) >= 4 * MAX_AUTO_POLICIES:
                self._tick_errors.clear()
            self._tick_errors[user] = now_mono
            log.exception(message, *args)

    def _status_for_tick(self, user: str, rt: _Runtime, policy: UserPolicy, now: datetime, now_ts: float) -> TimeStatus:
        """``status()`` for the tick - failing closed.  When it cannot be worked out (a
        storage error, a provider that raises, a bug) the user is enforced from the last
        status that could be, aged by the time since (a user who had 10 minutes left is out
        of time 10 minutes later); with none to go by, after STATUS_FAILURES_OUT_OF_TIME
        ticks in a row the user counts as out of time.  Never: "unknown, so unlimited"."""
        try:
            st = self.status(user, now)
        except Exception:  # noqa: BLE001
            rt.status_failures += 1
            self._log_user_error(user, "the time status of %s could not be worked out (%d in a row); "
                                 "enforcing from the last known one", user, rt.status_failures)
            return self._fallback_status(user, rt, policy, now_ts)
        if rt.status_failures:
            log.warning("the time status of %s can be worked out again", user)
        rt.status_failures = 0
        rt.last_status, rt.last_status_at = st, now_ts
        return st

    def _fallback_status(self, user: str, rt: _Runtime, policy: UserPolicy, now_ts: float) -> TimeStatus:
        last, at = rt.last_status, rt.last_status_at
        if last is not None and at is not None:
            age = max(0.0, now_ts - at)
            eff = last.effective_remaining_seconds
            if last.out_of_time or eff is None:
                return last
            if eff - age > 0:
                return replace(last, effective_remaining_seconds=eff - age)
            factor = self._factor_for(last, eff)
            return replace(last, effective_remaining_seconds=0.0, out_of_time=True, limiting_factor=factor,
                           block_desired=bool(last.enforcing and policy.block_login))
        out = rt.status_failures >= STATUS_FAILURES_OUT_OF_TIME
        enforcing = self.global_enabled
        try:
            enforcing = enforcing and self.limit_enabled(user)
        except Exception:  # noqa: BLE001 - the store is what fails: limits stay on
            pass
        return TimeStatus(
            user=user, managed=True, enforcing=enforcing, limit_enabled=True, used_seconds=0.0,
            limit_minutes=None, bonus_seconds=0, remaining_seconds=None, in_window=True,
            window_remaining_seconds=None, effective_remaining_seconds=0.0 if out else None,
            out_of_time=out, manually_blocked=False,
            grace_active=rt.grace_started is not None,
            grace_ends_at=(rt.grace_started + policy.grace_seconds) if rt.grace_started is not None else None,
            block_desired=bool(out and enforcing and policy.block_login), weekday_limit=None, weekend_limit=None,
            limit_today_override=None, limiting_factor="error" if out else None,
        )

    def tick(self, now: datetime, sessions_by_user: dict[str, list[SessionInfo]]) -> list[Action]:
        actions: list[Action] = []
        now_ts = now.timestamp()
        today = now.date()
        # users we asked to be login-blocked are visited even when they are no longer in the config
        blocked = {u for u, r in self._runtime.items() if r.block_desired}
        users = set(self._policies) | set(sessions_by_user) | blocked
        self._today = today
        self._last_now = now
        for user in sorted(users):
            # one user whose tick fails (a policy that cannot be built, a storage error)
            # must not stop the accounting and enforcement of everybody after it
            try:
                actions.extend(self._tick_user(user, now, now_ts, today, sessions_by_user.get(user, [])))
            except Exception:  # noqa: BLE001
                self._log_user_error(user, "time accounting for %s failed; the other users are not affected", user)
        return actions

    def _tick_user(self, user: str, now: datetime, now_ts: float, today: date,
                   sessions: list[SessionInfo]) -> list[Action]:
        """One user's part of :meth:`tick`: accrue the time and decide what to do."""
        actions: list[Action] = []
        policy = self.policy_for(user)
        if policy is None or policy.unlimited:
            # no longer managed (removed from the config or made unlimited by a
            # hot reload) while a login block was in place: lift it, once
            old = self._runtime.get(user)
            if old is not None and old.block_desired:
                old.block_desired = False
                old.grace_started = None
                old.enforced = False
                actions.append(Action("unblock", user, reason="unmanaged"))
        if policy is None:
            # Not listed and not managed: still keep "time used" statistics.
            self._accrue_unmanaged(user, sessions, now_ts, today)
            return actions
        rt = self._runtime.get(user)
        if rt is None:
            rt = _Runtime(day=today)
            self._runtime[user] = rt
        elif rt.day != today:
            # keep the grace period, the enforcement and the warnings given across
            # midnight: each is re-evaluated below against the new day's budget
            rt.day = today
        dt = self._elapsed(rt, now_ts)

        counting = [s for s in sessions if self.session_counts(s, policy)]
        if policy.unlimited:
            if counting and dt > 0:
                self.store.add_usage(user, today, active=dt)
            return actions
        focus_active, _until, _source = self.focus_state(user, policy, now)
        focus_pauses = focus_active and not policy.focus.counts_toward_limit
        self._watch_focus_agent(rt, focus_pauses, counting, now_ts)
        if rt.focus_agentless:
            focus_pauses = False
        if counting and dt > 0 and not focus_pauses:
            try:
                self.store.add_usage(user, today, active=dt)
            except Exception:  # noqa: BLE001 - not counted this tick; enforcing must still happen
                self._log_user_error(user, "could not count the time of %s", user)
        st = self._status_for_tick(user, rt, policy, now, now_ts)
        logged_in = [s.session_id for s in sessions if s.logged_in]

        # login blocking (desired state is re-evaluated every tick)
        if st.block_desired != rt.block_desired:
            rt.block_desired = st.block_desired
            actions.append(Action("block" if st.block_desired else "unblock", user, reason=st.limiting_factor or ""))

        if st.enforcing and st.out_of_time and logged_in:
            # a reference in the future means the clock was set back: restart from now
            # rather than waiting for the clock to catch up
            if rt.grace_started is not None and now_ts < rt.grace_started:
                rt.grace_started = now_ts
            if rt.last_enforced is not None and now_ts < rt.last_enforced:
                rt.last_enforced = None
            if rt.enforced:
                if rt.last_enforced is None or now_ts - rt.last_enforced >= ENFORCE_REPEAT_SECONDS:
                    rt.last_enforced = now_ts
                    # A locked session stays logged in, so "lock" is repeated for as long as
                    # the user is out of time: the event was logged once, re-lock silently.
                    actions.append(self._enforcement(policy, user, logged_in, st, record=policy.action != "lock"))
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
            return actions

        rt.grace_started = None
        if not st.out_of_time or not st.enforcing:
            # Has time again - or limits are switched off: an enforcement from before must not
            # carry over, or switching them back on would log off at once, without a grace period.
            rt.enforced = False
            rt.last_enforced = None
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
        dt = self._elapsed(rt, now_ts)
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
        elif factor == "error":
            why = "Your remaining computer time cannot be worked out right now."
        else:
            why = "You are outside your allowed computer hours."
        return f"{why} You will be {what} in {policy.grace_seconds} seconds. Save your work now."

    def _enforcement(self, policy: UserPolicy, user: str, sessions: list[int], st: TimeStatus,
                     record: bool = True) -> Action:
        if record:
            self.store.log_event(user, policy.action,
                                 f"used={int(st.used_seconds)}s limit={st.limit_minutes} reason={st.limiting_factor}")
        title = self._grace_title(st)
        return Action(policy.action, user, sessions, title=title, message="Your computer time is over.",
                      level="critical", reason=st.limiting_factor or "")

    def reset_runtime(self, user: str) -> None:
        self._runtime.pop(user.lower(), None)
