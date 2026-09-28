"""Decide whether somebody is actually *at* a seat, without a webcam.

"No keyboard or mouse input for N seconds" is a poor proxy for "nobody is
there": a child watching a film or playing with a game controller produces no
keyboard input, while a PC left on with the monitor asleep looks exactly like
a child reading a long web page.  This module combines every cheap, local
signal the per-session agent can observe into one of four states:

======== =====================================================================
active   human input (keyboard, mouse or game controller) within the idle
         threshold
passive  no recent input, but the seat is clearly in use: audio is playing,
         a full-screen video/game holds the display awake, media is playing
idle     no input past the idle threshold and nothing playing
away     definitely nobody there: session locked, screensaver running, the
         monitor is asleep, Windows itself reports the user as absent, or no
         input for the (longer) away threshold
======== =====================================================================

The evaluation is pure (no Windows calls) so it is unit-tested on any OS; the
agent side that gathers the raw signals lives in ``pclink/agent/win_presence.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .config import ActivityConfig
from .models import AgentReport, SessionInfo

PRESENCE_STATES = ["active", "passive", "idle", "away", "none"]
_ORDER = {"active": 0, "passive": 1, "idle": 2, "away": 3, "none": 4}


@dataclass
class Presence:
    state: str
    reason: str = ""
    idle_seconds: Optional[float] = None
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def engaged(self) -> bool:
        """Somebody is using the seat (actively or passively)."""
        return self.state in ("active", "passive")

    @property
    def absent(self) -> bool:
        return self.state == "away"

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason,
                "idle_seconds": None if self.idle_seconds is None else round(self.idle_seconds), **self.signals}


def _fg_is_media(report: AgentReport, media_apps: set[str], browsers: set[str]) -> bool:
    fg = report.foreground
    if fg is None or not fg.name:
        return False
    name = fg.name.lower()
    if name in media_apps:
        return True
    if fg.fullscreen:
        return True
    if name in browsers and report.media is not None and report.media.is_playing:
        return True
    return False


def evaluate(session: SessionInfo, cfg: ActivityConfig, *, idle_threshold: Optional[float] = None,
             away_threshold: Optional[float] = None) -> Presence:
    """Classify one session.  ``idle_threshold`` overrides ``cfg.idle_seconds``
    (the TimeKeeper passes the per-user budget threshold), ``away_threshold``
    overrides ``cfg.away_seconds``."""
    idle_th = float(cfg.idle_seconds if idle_threshold is None else idle_threshold)
    away_th = float(cfg.away_seconds if away_threshold is None else away_threshold)
    away_th = max(away_th, idle_th)

    if session.state != "active":
        return Presence("away", f"session {session.state}", session.idle_seconds)
    report = session.report
    locked = bool(session.locked) or bool(report and report.locked)
    idle = session.idle_seconds
    if idle is None and report is not None:
        idle = report.idle_seconds
    if locked:
        return Presence("away", "locked", idle, {"locked": True})

    signals: dict[str, Any] = {}
    if report is None:
        # Only the service-side WTS idle timer is available (agent not running).
        if idle is None:
            return Presence("active", "no idle data", None, {"agent": False})
        signals["agent"] = False
        if idle >= away_th:
            return Presence("away", f"no input for {int(idle // 60)} min (no agent)", idle, signals)
        if idle >= idle_th:
            return Presence("idle", "no input (no agent)", idle, signals)
        return Presence("active", "recent input", idle, signals)

    media_apps = {m.lower() for m in cfg.media_apps}
    browsers = {b.lower() for b in cfg.browsers}
    if idle is None:
        idle = report.idle_seconds
    audio = bool(report.audio_active and cfg.audio_keeps_active)
    media_playing = bool(report.media is not None and report.media.is_playing)
    fg_media = _fg_is_media(report, media_apps, browsers)
    keeps_display = bool(report.fullscreen_d3d or report.presentation_mode or (report.display_required and fg_media))
    media_engaged = bool(cfg.media_keeps_active and (media_playing or keeps_display))
    controller = (
        report.controller_idle_seconds is not None
        and report.input_idle_seconds is not None
        and report.controller_idle_seconds < report.input_idle_seconds
        and report.controller_idle_seconds < idle_th
    )
    signals.update({
        "agent": True,
        "input_idle_seconds": None if report.input_idle_seconds is None else round(report.input_idle_seconds),
        "controller": bool(controller),
        "audio": bool(report.audio_active),
        "media_playing": media_playing,
        "display": report.display_state,
        "display_required": bool(report.display_required),
        "fullscreen": bool(report.fullscreen_d3d or (report.foreground.fullscreen if report.foreground else False)),
        "screensaver": bool(report.screensaver),
        "windows_present": report.user_present,
    })

    # -- definitely away ----------------------------------------------------------
    if cfg.screensaver_is_idle and report.screensaver:
        return Presence("away", "screensaver running", idle, signals)
    if cfg.display_off_is_idle and report.display_state == "off" and idle >= cfg.display_off_min_idle_seconds:
        return Presence("away", "display off", idle, signals)

    passive_reason = None
    if audio:
        passive_reason = "audio playing"
    if media_engaged:
        if media_playing and report.media is not None and report.media.label():
            passive_reason = f"playing: {report.media.label()}"[:80]
        elif report.fullscreen_d3d:
            passive_reason = "full-screen game/video"
        elif report.presentation_mode:
            passive_reason = "presentation mode"
        elif passive_reason is None:
            passive_reason = "video keeps display on"

    windows_absent = bool(cfg.windows_presence and report.user_present is False)

    # -- recent input wins ------------------------------------------------------
    if idle < idle_th and not (windows_absent and idle >= 60):
        reason = "controller input" if controller else "recent input"
        return Presence("active", reason, idle, signals)

    # -- no recent input: passive engagement? -------------------------------------
    if passive_reason:
        return Presence("passive", passive_reason, idle, signals)
    if idle >= away_th:
        return Presence("away", f"no input for {int(idle // 60)} min", idle, signals)
    if windows_absent:
        return Presence("idle", "windows reports user absent", idle, signals)
    if cfg.display_off_is_idle and report.display_state == "off":
        return Presence("idle", "display off", idle, signals)
    return Presence("idle", "no input", idle, signals)


def best(presences: Iterable[Presence]) -> Optional[Presence]:
    """The most 'present' of several seats belonging to one user."""
    items = list(presences)
    if not items:
        return None
    return min(items, key=lambda p: _ORDER.get(p.state, 9))


def user_state(sessions: Iterable[SessionInfo]) -> str:
    """Roll a user's sessions up into a single presence state ('none' if logged off)."""
    states = [s.presence for s in sessions if s.presence]
    if not states:
        return "none"
    return min(states, key=lambda s: _ORDER.get(s, 9))
