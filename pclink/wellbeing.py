"""Break reminders (the "20-20-20" rule): every N minutes of continuous screen
use, remind the user to look away for a moment.  Pure logic; the orchestrator
turns the result into a tray balloon and a Home Assistant event.

"Continuous use" is measured per *user* (all their seats together) and only
resets after a real pause: no input and nothing playing for at least the
configured break duration.  Locking the session or logging off ends the
streak immediately.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .config import BreakConfig

MIN_PAUSE_SECONDS = 10.0   # less time without input than this is not a pause, whatever duration_seconds says
MAX_TICK_GAP_SECONDS = 120.0   # a longer hole between ticks is sleep (as in timekeeper)


@dataclass
class _State:
    day: Optional[date] = None
    streak_start: Optional[float] = None
    pause_start: Optional[float] = None
    last_engaged: Optional[float] = None
    last_reminder: Optional[float] = None
    last_tick: Optional[float] = None
    reminders_today: int = 0
    breaks_taken_today: int = 0


class BreakReminder:
    def __init__(self):
        self._users: dict[str, _State] = {}

    def _state(self, user: str, today: date) -> _State:
        st = self._users.get(user)
        if st is None:
            st = _State(day=today)
            self._users[user] = st
        elif st.day != today:
            # a new day resets the counters, not a streak that runs across midnight
            st.day = today
            st.reminders_today = 0
            st.breaks_taken_today = 0
        return st

    @staticmethod
    def _gap_seconds(cfg: BreakConfig) -> float:
        """Longest stretch without a tick that still counts as continuous use.

        Ticks normally arrive every few seconds (at most every minute); a hole
        longer than two minutes - the same as ``timekeeper.MAX_TICK_GAP_SECONDS`` -
        or than a break means the PC slept or the service was not running: nobody
        was looking at the screen then."""
        return max(MAX_TICK_GAP_SECONDS, float(cfg.duration_seconds))

    def tick(self, user: str, cfg: BreakConfig, now_ts: float, today: date, *, engaged: bool,
             enabled: bool = True, input_idle_seconds: Optional[float] = None,
             passive: bool = False) -> Optional[dict[str, Any]]:
        """Advance the streak.  Returns a reminder payload when one is due.

        ``engaged``: the seat is in use - its presence is active or passive.
        ``input_idle_seconds``: seconds since the last keyboard / mouse / controller
        input on the user's seats (the smallest of them; None when no agent says).
        ``passive``: something is playing (a film), which keeps the eyes on the screen.

        Presence only turns idle after ``activity.idle_seconds`` (minutes), far
        longer than a break, so the input idle time decides: at least
        ``cfg.duration_seconds`` without input, and nothing playing, is a break.
        Without input for ``cfg.reset_after_idle_minutes`` (0: off) the count starts
        anew even while something plays.
        """
        idle: Optional[float] = None
        if input_idle_seconds is not None:
            idle = max(0.0, float(input_idle_seconds))
            if not idle < float("inf"):
                idle = None
        if engaged and idle is not None:
            reset_after = float(cfg.reset_after_idle_minutes) * 60
            # (a few seconds without input are just reading: the agent samples every couple of seconds)
            if (not passive and idle >= max(MIN_PAUSE_SECONDS, cfg.duration_seconds)) or \
                    (reset_after > 0 and idle >= reset_after):
                engaged = False
        st = self._state(user, today)
        last_tick, st.last_tick = st.last_tick, now_ts
        if not enabled or not cfg.enabled:
            st.streak_start = None
            st.pause_start = None
            return None
        if last_tick is not None:
            if now_ts < last_tick:
                # the clock was set back: restart the references instead of waiting for it to catch up
                if st.streak_start is not None:
                    st.streak_start = now_ts
                if st.pause_start is not None:
                    st.pause_start = now_ts
                st.last_reminder = None
            elif now_ts - last_tick > self._gap_seconds(cfg):
                # nobody was ticking us: the PC slept or the user was logged off - that was a break
                if st.streak_start is not None:
                    st.breaks_taken_today += 1
                st.streak_start = None
                st.pause_start = None
        if engaged:
            st.pause_start = None
            st.last_engaged = now_ts
            if st.streak_start is None:
                st.streak_start = now_ts
            streak = now_ts - st.streak_start
            every = cfg.every_minutes * 60
            due_since = st.streak_start if st.last_reminder is None else max(st.last_reminder, st.streak_start)
            if streak >= every and now_ts - due_since >= every:
                st.last_reminder = now_ts
                st.reminders_today += 1
                return {
                    "title": cfg.title,
                    "message": cfg.message,
                    "continuous_minutes": int(streak // 60),
                    "reminders_today": st.reminders_today,
                }
            return None
        # not engaged ------------------------------------------------------------
        if st.pause_start is None:
            st.pause_start = now_ts
        if idle is not None:
            # the pause began with the last input, not when it was noticed
            st.pause_start = min(st.pause_start, now_ts - idle)
            if st.streak_start is not None:
                st.pause_start = max(st.pause_start, st.streak_start)
        if st.streak_start is not None and now_ts - st.pause_start >= cfg.duration_seconds:
            st.streak_start = None
            st.breaks_taken_today += 1
        return None

    def reset(self, user: str) -> None:
        st = self._users.get(user)
        if st:
            st.streak_start = None
            st.pause_start = None

    def status(self, user: str, cfg: BreakConfig, now_ts: float) -> dict[str, Any]:
        st = self._users.get(user)
        streak = 0.0
        if st and st.streak_start is not None:
            streak = max(0.0, now_ts - st.streak_start)
        every = cfg.every_minutes * 60
        if st and st.streak_start is not None:
            base = st.streak_start if st.last_reminder is None else max(st.last_reminder, st.streak_start)
            next_in = max(0.0, every - (now_ts - base))
        else:
            next_in = float(every)
        return {
            "enabled": cfg.enabled,
            "every_minutes": cfg.every_minutes,
            "continuous_minutes": round(streak / 60, 1),
            "next_break_in_minutes": round(next_in / 60, 1) if cfg.enabled else None,
            "reminders_today": st.reminders_today if st else 0,
            "breaks_taken_today": st.breaks_taken_today if st else 0,
        }
