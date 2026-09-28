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


@dataclass
class _State:
    day: Optional[date] = None
    streak_start: Optional[float] = None
    pause_start: Optional[float] = None
    last_engaged: Optional[float] = None
    last_reminder: Optional[float] = None
    reminders_today: int = 0
    breaks_taken_today: int = 0


class BreakReminder:
    def __init__(self):
        self._users: dict[str, _State] = {}

    def _state(self, user: str, today: date) -> _State:
        st = self._users.get(user)
        if st is None or st.day != today:
            st = _State(day=today)
            self._users[user] = st
        return st

    def tick(self, user: str, cfg: BreakConfig, now_ts: float, today: date, *, engaged: bool,
             enabled: bool = True) -> Optional[dict[str, Any]]:
        """Advance the streak.  Returns a reminder payload when one is due.

        ``engaged`` must be true while the person is looking at the screen:
        input within the last few seconds *or* passive media use.  A stretch of
        at least ``cfg.duration_seconds`` without engagement counts as a break.
        """
        st = self._state(user, today)
        if not enabled or not cfg.enabled:
            st.streak_start = None
            st.pause_start = None
            return None
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
