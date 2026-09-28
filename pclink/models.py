"""Plain data structures exchanged between the service, the agent and HA."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
class AgentReport:
    """Periodic activity report sent by the per-session agent."""

    session_id: int
    ts: float
    idle_seconds: float = 0.0
    locked: bool = False
    foreground: Optional[ForegroundInfo] = None
    url: Optional[str] = None
    domain: Optional[str] = None
    audio_active: bool = False

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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentReport":
        return cls(
            session_id=int(data["session_id"]),
            ts=float(data.get("ts", 0.0)),
            idle_seconds=float(data.get("idle_seconds", 0.0) or 0.0),
            locked=bool(data.get("locked", False)),
            foreground=ForegroundInfo.from_dict(data.get("foreground")),
            url=data.get("url"),
            domain=data.get("domain"),
            audio_active=bool(data.get("audio_active", False)),
        )

    def signature(self) -> tuple:
        """Hashable summary used to suppress duplicate reports."""
        fg = self.foreground
        return (
            self.locked,
            self.audio_active,
            self.domain,
            (fg.pid, fg.name, fg.title, fg.fullscreen) if fg else None,
            self.idle_seconds > 60,
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

    @property
    def logged_in(self) -> bool:
        return self.state in ("active", "connected", "disconnected")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "user": self.user,
            "station": self.station,
            "state": self.state,
            "locked": self.locked,
            "idle_seconds": None if self.idle_seconds is None else round(self.idle_seconds),
        }


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
        return self.category not in ("none", "locked", "idle")


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
