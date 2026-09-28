"""Plain data structures exchanged between the service, the agent and HA."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class ForegroundInfo:
    """The window that currently has input focus in a user session."""

    pid: int = 0
    name: str = ""       # process image name, e.g. "chrome.exe"
    exe: str = ""        # full path when readable, else ""
    title: str = ""
    fullscreen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> Optional["ForegroundInfo"]:
        if not data:
            return None
        return cls(
            pid=int(data.get("pid", 0) or 0),
            name=str(data.get("name", "") or ""),
            exe=str(data.get("exe", "") or ""),
            title=str(data.get("title", "") or ""),
            fullscreen=bool(data.get("fullscreen", False)),
        )


@dataclass
class MediaInfo:
    """What the session is playing (now-playing), when the agent can tell."""

    title: str = ""
    artist: str = ""
    app: str = ""            # process name or source app id
    status: str = "unknown"  # playing | paused | stopped | unknown
    source: str = ""         # gsmtc | title

    @property
    def is_playing(self) -> bool:
        return self.status == "playing"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> Optional["MediaInfo"]:
        if not data:
            return None
        return cls(
            title=str(data.get("title", "") or ""),
            artist=str(data.get("artist", "") or ""),
            app=str(data.get("app", "") or ""),
            status=str(data.get("status", "unknown") or "unknown"),
            source=str(data.get("source", "") or ""),
        )

    def label(self) -> str:
        if self.title and self.artist:
            return f"{self.artist} - {self.title}"
        return self.title or ""


def _opt_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


@dataclass
class AgentReport:
    """Periodic activity report sent by the per-session agent.

    ``idle_seconds`` is the time since the last *human* input the agent could
    observe (keyboard, mouse and - when enabled - game controllers).  The
    extra presence signals let the service tell "watching a film" from "went
    to bed with the PC on" without a webcam; see ``pclink/presence.py``.
    """

    session_id: int
    ts: float
    idle_seconds: float = 0.0
    locked: bool = False
    foreground: Optional[ForegroundInfo] = None
    url: Optional[str] = None
    domain: Optional[str] = None
    audio_active: bool = False
    # -- presence signals (all optional; older agents simply omit them) ------
    input_idle_seconds: Optional[float] = None    # keyboard/mouse only
    controller_idle_seconds: Optional[float] = None
    display_state: Optional[str] = None           # on | off | dimmed
    user_present: Optional[bool] = None           # Windows' own user-presence verdict
    screensaver: bool = False
    fullscreen_d3d: bool = False                  # exclusive full-screen D3D app (game / video)
    display_required: bool = False                # some app asked Windows to keep the display on
    presentation_mode: bool = False
    # -- extras ---------------------------------------------------------------
    media: Optional[MediaInfo] = None
    volume: Optional[int] = None                  # 0..100 of the session's default output
    muted: Optional[bool] = None
    agent_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "report",
            "session_id": self.session_id,
            "ts": self.ts,
            "idle_seconds": round(self.idle_seconds, 1),
            "locked": self.locked,
            "foreground": self.foreground.to_dict() if self.foreground else None,
            "url": self.url,
            "domain": self.domain,
            "audio_active": self.audio_active,
            "input_idle_seconds": None if self.input_idle_seconds is None else round(self.input_idle_seconds, 1),
            "controller_idle_seconds": None if self.controller_idle_seconds is None else round(self.controller_idle_seconds, 1),
            "display_state": self.display_state,
            "user_present": self.user_present,
            "screensaver": self.screensaver,
            "fullscreen_d3d": self.fullscreen_d3d,
            "display_required": self.display_required,
            "presentation_mode": self.presentation_mode,
            "media": self.media.to_dict() if self.media else None,
            "volume": self.volume,
            "muted": self.muted,
            "agent_version": self.agent_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentReport":
        def _opt_float(key: str) -> Optional[float]:
            v = data.get(key)
            return None if v is None else float(v)

        volume = data.get("volume")
        return cls(
            session_id=int(data["session_id"]),
            ts=float(data.get("ts", 0.0)),
            idle_seconds=float(data.get("idle_seconds", 0.0) or 0.0),
            locked=bool(data.get("locked", False)),
            foreground=ForegroundInfo.from_dict(data.get("foreground")),
            url=data.get("url"),
            domain=data.get("domain"),
            audio_active=bool(data.get("audio_active", False)),
            input_idle_seconds=_opt_float("input_idle_seconds"),
            controller_idle_seconds=_opt_float("controller_idle_seconds"),
            display_state=(str(data["display_state"]) if data.get("display_state") else None),
            user_present=_opt_bool(data.get("user_present")),
            screensaver=bool(data.get("screensaver", False)),
            fullscreen_d3d=bool(data.get("fullscreen_d3d", False)),
            display_required=bool(data.get("display_required", False)),
            presentation_mode=bool(data.get("presentation_mode", False)),
            media=MediaInfo.from_dict(data.get("media")),
            volume=None if volume is None else int(volume),
            muted=_opt_bool(data.get("muted")),
            agent_version=str(data.get("agent_version", "") or ""),
        )

    def signature(self) -> tuple:
        """Hashable summary used to suppress duplicate reports."""
        fg = self.foreground
        media = self.media
        return (
            self.locked,
            self.audio_active,
            self.domain,
            (fg.pid, fg.name, fg.title, fg.fullscreen) if fg else None,
            self.idle_seconds > 60,
            self.display_state,
            self.user_present,
            self.screensaver,
            self.fullscreen_d3d,
            self.display_required,
            (media.title, media.artist, media.status) if media else None,
            self.volume,
            self.muted,
        )


@dataclass
class SessionInfo:
    """A Windows logon session as seen by the service (one per ASTER seat)."""

    session_id: int
    user: str                     # SAM account name, lower-case
    domain: str = ""
    station: str = ""
    state: str = "active"         # active | connected | disconnected | other
    locked: Optional[bool] = None
    idle_seconds: Optional[float] = None
    logon_time: Optional[float] = None
    report: Optional[AgentReport] = None
    presence: Optional[str] = None        # active | passive | idle | away (see presence.py)
    presence_reason: str = ""

    @property
    def logged_in(self) -> bool:
        return self.state in ("active", "connected", "disconnected")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.session_id,
            "user": self.user,
            "station": self.station,
            "state": self.state,
            "locked": self.locked,
            "idle_seconds": None if self.idle_seconds is None else round(self.idle_seconds),
            "presence": self.presence,
            "presence_reason": self.presence_reason or None,
        }
        if self.report is not None:
            d["display"] = self.report.display_state
            d["agent"] = True
        else:
            d["agent"] = False
        return d


@dataclass
class Activity:
    """What a user is doing right now, derived from the agent report."""

    category: str = "none"        # none | locked | idle | gaming | browsing | app | <site category>
    app: Optional[str] = None
    exe: Optional[str] = None
    title: Optional[str] = None
    domain: Optional[str] = None
    url: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.category not in ("none", "locked", "idle", "away")


@dataclass
class HwReading:
    """A single hardware sensor reading (LibreHardwareMonitor style)."""

    key: str
    hardware: str
    hardware_type: str
    sensor_type: str
    name: str
    value: Optional[float]
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
