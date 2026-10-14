"""Plain data structures exchanged between the service, the agent and HA."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Optional

# Everything in a report comes from a process that runs as the monitored user,
# so the service checks every field before using it: numbers must be finite
# (and are clamped), text must be text (and is cut to a sane length).
MAX_IDLE_SECONDS = 366 * 86400.0
MAX_NAME_LENGTH = 260      # a Windows file name
MAX_PATH_LENGTH = 1024
MAX_TITLE_LENGTH = 512
MAX_URL_LENGTH = 2048
MAX_DOMAIN_LENGTH = 253
MAX_LABEL_LENGTH = 256
MAX_APP_ID_LENGTH = 128    # an AppUserModelID ("Chrome._crx_<32 letters>") or a browser's --app-id


class ReportError(ValueError):
    """A field of an agent report has the wrong type or is not a finite number."""


def _text(value: Any, limit: int, field_name: str) -> str:
    """``value`` as text of at most ``limit`` characters, without control characters."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReportError(f"{field_name} is not text")
    if len(value) > limit:
        value = value[:limit]
    return "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in value)


def _opt_text(value: Any, limit: int, field_name: str) -> Optional[str]:
    return _text(value, limit, field_name) or None


def _number(value: Any, low: float, high: float, field_name: str) -> float:
    """A finite number clamped to ``low..high`` (booleans and text are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{field_name} is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ReportError(f"{field_name} is not finite")
    return min(max(number, low), high)


def _opt_number(value: Any, low: float, high: float, field_name: str) -> Optional[float]:
    return None if value is None else _number(value, low, high, field_name)


def _flag(value: Any, field_name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, (bool, int)):   # Win32 calls hand back BOOLs: any non-zero integer is true
        return bool(value)
    raise ReportError(f"{field_name} is not a flag")


@dataclass
class ForegroundInfo:
    """The window that currently has input focus in a user session."""

    pid: int = 0
    name: str = ""       # process image name, e.g. "chrome.exe"
    exe: str = ""        # full path when readable, else ""
    title: str = ""
    fullscreen: bool = False
    created: float = 0.0  # when the process started (epoch seconds; 0 = not known): tells a reused pid apart

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> Optional["ForegroundInfo"]:
        if not data:
            return None
        if not isinstance(data, dict):
            raise ReportError("foreground is not an object")
        return cls(
            pid=int(_number(data.get("pid") or 0, 0, 0xFFFFFFFF, "foreground.pid")),
            name=_text(data.get("name"), MAX_NAME_LENGTH, "foreground.name").strip(),
            exe=_text(data.get("exe"), MAX_PATH_LENGTH, "foreground.exe"),
            title=_text(data.get("title"), MAX_TITLE_LENGTH, "foreground.title"),
            fullscreen=_flag(data.get("fullscreen"), "foreground.fullscreen"),
            created=_number(data.get("created") or 0.0, 0.0, 1e11, "foreground.created"),
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
        if not isinstance(data, dict):
            raise ReportError("media is not an object")
        return cls(
            title=_text(data.get("title"), MAX_LABEL_LENGTH, "media.title"),
            artist=_text(data.get("artist"), MAX_LABEL_LENGTH, "media.artist"),
            app=_text(data.get("app"), MAX_NAME_LENGTH, "media.app"),
            status=_text(data.get("status"), 16, "media.status") or "unknown",
            source=_text(data.get("source"), 16, "media.source"),
        )

    def label(self) -> str:
        if self.title and self.artist:
            return f"{self.artist} - {self.title}"
        return self.title or ""


def _opt_bool(value: Any, field_name: str = "flag") -> Optional[bool]:
    if value is None:
        return None
    return _flag(value, field_name)


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
    host: Optional[str] = None                    # full lower-case host of ``url`` (``domain`` may be the registrable part)
    agent_pid: Optional[int] = None               # the agent's own process id: a hint only, the service knows the real one
    # a browser window without an address bar (an installed web app, an --app window) whose page
    # address could not be read: ``domain`` / ``host``, if any, are only a guess (title, start address)
    site_unknown: bool = False
    app_id: Optional[str] = None                  # such a window's app identity (AppUserModelID / --app-id)
    # set by the service, never taken from the agent: why the report was refused (see from_dict)
    invalid: Optional[str] = None
    received: float = 0.0   # monotonic time the service received it (staleness never uses the agent's ``ts``)

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
            "host": self.host,
            "agent_pid": self.agent_pid,
            "site_unknown": self.site_unknown,
            "app_id": self.app_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentReport":
        """Build a report from what an agent sent.

        The sender runs as the monitored user, so nothing is taken on trust:
        a field of the wrong type or a number that is not finite makes the
        whole report ``invalid`` (the service drops it and flags the user);
        numbers are clamped and text is cut to a sane length.  ``ts`` is only
        informative - the service stamps a report when it arrives."""
        try:
            return cls._parse(data)
        except (ReportError, TypeError, ValueError, OverflowError, AttributeError, KeyError) as exc:
            session_id = data.get("session_id") if isinstance(data, dict) else None
            session_id = session_id if isinstance(session_id, int) and not isinstance(session_id, bool) else -1
            return cls(session_id=session_id, ts=0.0, invalid=str(exc)[:200] or type(exc).__name__)

    @classmethod
    def _parse(cls, data: dict[str, Any]) -> "AgentReport":
        if not isinstance(data, dict):
            raise ReportError("report is not an object")

        def idle(key: str) -> Optional[float]:
            return _opt_number(data.get(key), 0.0, MAX_IDLE_SECONDS, key)

        volume = _opt_number(data.get("volume"), 0, 100, "volume")
        agent_pid = _opt_number(data.get("agent_pid"), 0, 0xFFFFFFFF, "agent_pid")
        domain = _text(data.get("domain"), MAX_DOMAIN_LENGTH, "domain").strip().lower()
        return cls(
            session_id=int(_number(data.get("session_id", -1), -1, 0xFFFFFFFF, "session_id")),
            ts=_number(data.get("ts") or 0.0, 0.0, 1e11, "ts"),
            idle_seconds=idle("idle_seconds") or 0.0,
            locked=_flag(data.get("locked"), "locked"),
            foreground=ForegroundInfo.from_dict(data.get("foreground")),
            url=_opt_text(data.get("url"), MAX_URL_LENGTH, "url"),
            domain=domain or None,
            audio_active=_flag(data.get("audio_active"), "audio_active"),
            input_idle_seconds=idle("input_idle_seconds"),
            controller_idle_seconds=idle("controller_idle_seconds"),
            display_state=_opt_text(data.get("display_state"), 16, "display_state"),
            user_present=_opt_bool(data.get("user_present"), "user_present"),
            screensaver=_flag(data.get("screensaver"), "screensaver"),
            fullscreen_d3d=_flag(data.get("fullscreen_d3d"), "fullscreen_d3d"),
            display_required=_flag(data.get("display_required"), "display_required"),
            presentation_mode=_flag(data.get("presentation_mode"), "presentation_mode"),
            media=MediaInfo.from_dict(data.get("media")),
            volume=None if volume is None else int(round(volume)),
            muted=_opt_bool(data.get("muted"), "muted"),
            agent_version=_text(data.get("agent_version"), 32, "agent_version"),
            host=_text(data.get("host"), MAX_DOMAIN_LENGTH, "host").strip().lower() or None,
            agent_pid=None if agent_pid is None else int(agent_pid),
            # older agents do not send these: a site they could not read is simply "no site"
            site_unknown=_flag(data.get("site_unknown"), "site_unknown"),
            app_id=_text(data.get("app_id"), MAX_APP_ID_LENGTH, "app_id").strip() or None,
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
            self.site_unknown,
            self.app_id,
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
    # a browser window whose page address the agent could not read (see AgentReport.site_unknown):
    # it is never taken for an allow-listed site, whatever ``domain`` guesses
    site_unknown: bool = False
    app_id: Optional[str] = None

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
