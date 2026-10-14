"""Configuration schema and loader (YAML -> dataclasses with validation)."""
from __future__ import annotations

import difflib
import logging
import math
import os
import re
import socket
import sys
from dataclasses import MISSING, dataclass, field
from typing import Any, Callable, Optional, TypeVar

import yaml

from .util import is_ip, normalize_site, parse_hhmm, parse_windows_split, registrable_domain, slugify, time_zone

log = logging.getLogger(__name__)

T = TypeVar("T")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_ALIASES = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
    "mo": "mon", "tu": "tue", "we": "wed", "th": "thu", "fr": "fri", "sa": "sat", "su": "sun",
}
_DAY_GROUPS = {
    "all": list(range(7)), "default": list(range(7)), "everyday": list(range(7)), "daily": list(range(7)),
    "weekday": [0, 1, 2, 3, 4], "weekdays": [0, 1, 2, 3, 4], "workday": [0, 1, 2, 3, 4], "workdays": [0, 1, 2, 3, 4],
    "weekend": [5, 6], "weekends": [5, 6],
}


class ConfigError(ValueError):
    pass


# time_limits.pin_required_for: the kinds of action the parent PIN can guard (see pclink/pin.py)
PIN_KINDS = ("grant", "unblock", "disable", "focus", "focus_off", "bedtime", "screenshot", "parent_present")
DEFAULT_PIN_KINDS = ("grant", "unblock", "disable", "focus", "focus_off", "bedtime", "screenshot")
_PBKDF2_HASH = re.compile(r"^pbkdf2_sha256\$[1-9][0-9]{0,8}\$[0-9a-f]{16,128}\$[0-9a-f]{64}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _day_index(token: str) -> int:
    token = token.strip().lower()
    token = _DAY_ALIASES.get(token, token)
    if token in DAYS:
        return DAYS.index(token)
    raise ConfigError(f"unknown day name {token!r}")


def _day_indexes(key: object) -> list[int]:
    key = str(key).strip().lower()
    if key in _DAY_GROUPS:
        return list(_DAY_GROUPS[key])
    if "," in key:
        out: list[int] = []
        for part in key.split(","):
            out.extend(_day_indexes(part))
        return out
    if "-" in key:
        a, b = key.split("-", 1)
        ia, ib = _day_index(a), _day_index(b)
        if ia <= ib:
            return list(range(ia, ib + 1))
        return list(range(ia, 7)) + list(range(0, ib + 1))
    return [_day_index(key)]


def _specificity(key: object) -> int:
    k = str(key).strip().lower()
    if k in ("all", "default", "everyday", "daily"):
        return 0
    if k in _DAY_GROUPS:
        return 1
    if "," in k or "-" in k:
        return 2
    return 3


def by_day(value: Any, parse: Callable[[Any], T], default: T) -> dict[int, T]:
    """Expand a scalar or a {day-spec: value} mapping into a full 7-day dict."""
    result = {i: default for i in range(7)}
    if value is None:
        return result
    if isinstance(value, dict):
        for key, raw in sorted(value.items(), key=lambda kv: _specificity(kv[0])):
            parsed = parse(raw)
            for idx in _day_indexes(key):
                result[idx] = parsed
        return result
    parsed = parse(value)
    return {i: parsed for i in range(7)}


def by_day_windows(value: Any, empty_is_open: bool = True) -> dict[int, list]:
    """``by_day`` for time windows.  A range that wraps past midnight ("16:00-01:00" on
    Friday) continues into the *next* day's early morning instead of the same day's.

    ``empty_is_open``: a day without windows means "no restriction" (allowed hours), so
    the tail of the previous evening is not added to it.  For a focus schedule a day
    without windows means "no focus", and the tail is kept."""
    parsed = by_day(value, parse_windows_split, ([], []))
    result: dict[int, list] = {i: list(parsed[i][0]) for i in range(7)}
    for i in range(7):
        spill = list(parsed[i][1])
        if not spill:
            continue
        nxt = (i + 1) % 7
        current = result[nxt]
        if not current:
            if not empty_is_open or (value is not None and not isinstance(value, dict)):
                result[nxt] = sorted(spill)  # same spec every day, entirely after midnight
            continue  # the next day has no restriction at all
        if current == [(0, 0)]:  # "never", except for the tail of the previous evening
            current = []
        result[nxt] = sorted(set(current) | set(spill))
    return result


def _parse_minutes(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        # YAML reads an unquoted off / no / false as the boolean false (and on / yes as true):
        # "off" was meant as "no limit", it must not turn into a limit of 0 minutes.
        if raw:
            raise ConfigError("invalid minutes value: yes / on / true is not a number of minutes "
                              "(write the minutes, or \"unlimited\")")
        log.warning("a limit is written as off / no / false: taken as no limit - "
                    "write \"unlimited\" (or quote it: \"off\") to say so clearly")
        return None
    text: Any = raw
    factor = 1.0
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("", "unlimited", "none", "off", "no", "false", "inf", "infinite"):
            return None
        if text.endswith("h"):
            text, factor = text[:-1], 60.0
        elif text.endswith("min"):
            text = text[:-3]
        elif text.endswith("m"):
            text = text[:-1]
    try:
        number = float(text) * factor
        if not math.isfinite(number):
            raise ValueError("not finite")
        value = int(number)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError(f"invalid minutes value {raw!r}") from None
    if value < 0:
        return None
    return value


def _program_data() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "PCLink")
    return os.path.join(os.path.expanduser("~"), ".pclink")


def data_dir() -> str:
    """The folder holding the configuration, the database and the service's logs."""
    return _program_data()


def default_config_path() -> str:
    env = os.environ.get("PCLINK_CONFIG")
    if env:
        return env
    return os.path.join(_program_data(), "config.yaml")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

@dataclass
class DeviceConfig:
    name: str = ""
    id: str = ""
    manufacturer: str = "home-assistant-pc-link"
    model: str = "Windows PC"
    suggested_area: Optional[str] = None


@dataclass
class TelemetryConfig:
    interval_seconds: float = 15.0
    disks: bool = True
    disk_io: bool = True
    network: bool = True
    gpu: bool = True
    battery: bool = True
    top_processes: int = 0  # 0 disables (costly-ish); N -> top-N by CPU and by memory, grouped by name
    usb_devices: bool = False
    printers: bool = False
    devices_interval_seconds: float = 300.0
    printers_interval_seconds: float = 60.0


@dataclass
class HardwareSensorsConfig:
    enabled: bool = True
    backend: str = "auto"  # auto | lhm | lhm_http | none
    lhm_dll_path: str = r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitorLib.dll"
    lhm_http_url: str = "http://127.0.0.1:8085/data.json"
    interval_seconds: float = 10.0
    storage_interval_seconds: float = 300.0
    sensor_types: list[str] = field(default_factory=lambda: ["temperature", "fan", "power", "load"])
    include: list[str] = field(default_factory=list)  # regexes on "hardware/sensor type/name"
    exclude: list[str] = field(default_factory=list)
    max_sensors: int = 80
    cpu: bool = True
    gpu: bool = True
    motherboard: bool = True
    memory: bool = False
    storage: bool = True
    controller: bool = False
    network: bool = False
    psu: bool = False
    battery: bool = False


@dataclass
class AppRule:
    """A per-app / per-site limit or blocklist entry (see ``pclink/rules.py``).

    A rule matches when the user's current activity is one of its ``apps``
    (process names), ``sites`` (domains, suffix-matched) or ``categories``
    (site categories from ``activity.site_categories`` or ``gaming``).
    """

    name: str
    apps: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    daily_limits: dict[int, Optional[int]] = field(default_factory=lambda: {i: None for i in range(7)})
    weekly_limit_minutes: Optional[int] = None   # budget per week (the week starts on time_limits.week_start)
    allowed_windows: dict[int, list] = field(default_factory=lambda: {i: [] for i in range(7)})
    blocked: bool = False
    warn_at_minutes: list[int] = field(default_factory=lambda: [5, 1])
    action: str = "close"  # close | warn
    message: Optional[str] = None
    icon: str = "mdi:timer-lock-outline"
    # the rule as it was written (canonical keys, JSON-safe): what Home Assistant reads and edits
    definition: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    # what pclink/entities.py appends to ``rule_<slug>`` for the rule's other entities
    ENTITY_SUFFIXES = ("_enabled", "_remaining", "_limit_today", "_weekly_remaining", "_weekly_limit")

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def entity_ids(self) -> set[str]:
        """Every id the rule's entities can get (relative to the user's ``rule_`` prefix)."""
        return {self.slug + suffix for suffix in ("",) + self.ENTITY_SUFFIXES}

    @property
    def has_limit(self) -> bool:
        return any(v is not None for v in self.daily_limits.values())

    @property
    def has_weekly_limit(self) -> bool:
        return self.weekly_limit_minutes is not None

    @property
    def has_hours(self) -> bool:
        return any(self.allowed_windows.values())


@dataclass
class FocusConfig:
    """Focus / homework mode: only allow-listed apps and sites for a while."""

    allow_apps: list[str] = field(default_factory=list)
    allow_sites: list[str] = field(default_factory=list)
    allow_categories: list[str] = field(default_factory=lambda: ["education"])
    # a browser window whose address cannot be read (an installed web app or --app window the
    # agent could not look into) is not allowed during focus unless this is true
    allow_unknown_sites: bool = False
    default_minutes: int = 60
    counts_toward_limit: bool = False
    action: str = "close"  # close | warn
    message: str = "Focus mode is on - this app is not allowed right now."
    schedule: dict[int, list] = field(default_factory=lambda: {i: [] for i in range(7)})  # automatic focus windows

    @property
    def scheduled(self) -> bool:
        return any(self.schedule.values())


@dataclass
class BreakConfig:
    """Break reminders (the 20-20-20 rule by default)."""

    enabled: bool = False
    every_minutes: int = 20
    duration_seconds: int = 20
    reset_after_idle_minutes: int = 3
    title: str = "Time for a short break"
    message: str = "Look at something 20 feet (6 m) away for 20 seconds."


@dataclass
class RewardConfig:
    """'Earn minutes': a button in HA that grants bonus minutes (chore done)."""

    name: str
    minutes: int = 15
    daily_max: int = 0  # how many times per day it can be claimed (0 = unlimited)
    icon: str = "mdi:star-circle"

    @property
    def slug(self) -> str:
        return slugify(self.name)


@dataclass
class UserPolicy:
    name: str
    unlimited: bool = False
    display_name: Optional[str] = None
    daily_limits: dict[int, Optional[int]] = field(default_factory=lambda: {i: None for i in range(7)})
    allowed_windows: dict[int, list] = field(default_factory=lambda: {i: [] for i in range(7)})
    warn_at_minutes: list[int] = field(default_factory=lambda: [15, 5, 1])
    action: str = "logoff"  # logoff | lock | shutdown
    grace_seconds: int = 60
    count_idle: bool = True
    idle_threshold_seconds: int = 600
    count_away: Optional[bool] = None  # None -> same as count_idle
    count_passive: bool = True         # watching a film / listening (no input) counts as active
    count_locked: bool = False
    block_login: bool = True
    # weekly budget & rollover --------------------------------------------------
    weekly_limit_minutes: Optional[int] = None
    rollover_max_minutes: int = 0      # carry unused minutes into the next day, up to this cap (0 = off)
    # bedtime (separate from allowed hours) -----------------------------------
    bedtime: dict[int, Optional[int]] = field(default_factory=lambda: {i: None for i in range(7)})     # minute of day
    wake_time: dict[int, int] = field(default_factory=lambda: {i: 0 for i in range(7)})
    bedtime_parent_extension_minutes: int = 0   # auto-extend bedtime while a parent is home
    # per-app / per-site rules, focus mode, breaks, rewards ------------------
    rules: list[AppRule] = field(default_factory=list)
    focus: FocusConfig = field(default_factory=FocusConfig)
    breaks: BreakConfig = field(default_factory=BreakConfig)
    rewards: list[RewardConfig] = field(default_factory=list)
    # profile name -> this policy with that profile's overrides applied (see PROFILE_KEYS);
    # which one is in force is a stored setting (TimeKeeper.set_profile)
    profiles: dict[str, "UserPolicy"] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        return self.name.lower()

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @property
    def has_bedtime(self) -> bool:
        return any(v is not None for v in self.bedtime.values())

    @property
    def has_restrictions(self) -> bool:
        if self.unlimited:
            return False
        return (
            any(v is not None for v in self.daily_limits.values())
            or any(self.allowed_windows.values())
            or self.weekly_limit_minutes is not None
            or self.has_bedtime
        )

    def counts_away(self) -> bool:
        return self.count_idle if self.count_away is None else self.count_away


@dataclass
class TimeRequestsConfig:
    """"Ask for more time": the child asks from the tray, a parent answers from Home Assistant."""

    enabled: bool = True
    options: list[int] = field(default_factory=lambda: [15, 30, 60])  # minutes that can be asked for
    cooldown_minutes: int = 10    # after a request, the next one can be made this much later
    expire_minutes: int = 30      # an unanswered request lapses
    max_per_day: int = 5


# what a profile (time_limits.profiles / a user's profiles) may override, and the other
# spellings a user's policy accepts for the same thing
PROFILE_KEYS = {
    "daily_limit_minutes": ("daily_limit",), "weekly_limit_minutes": ("weekly_limit",),
    "allowed_hours": ("allowed_windows",), "bedtime": (), "wake_time": ("wake",),
    "rollover_max_minutes": ("rollover",),
}
NORMAL_PROFILE = "normal"   # reserved: "no profile"
_PROFILE_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")


@dataclass
class TimeLimitsConfig:
    enabled: bool = True
    tick_seconds: float = 5.0
    block_method: str = "logon_hours"  # logon_hours | disable_account | logoff_only
    manage_unknown_users: bool = False
    unblock_on_stop: bool = False  # keep blocks across restarts/reboots; 'remove' always unblocks
    reassert_block_seconds: float = 300.0
    week_start: str = "mon"
    # IANA name ("Europe/Budapest") of the time zone days, allowed hours and bedtimes are
    # reckoned in.  Empty: the PC's own setting - which a standard user may change.
    time_zone: Optional[str] = None
    # count this user's time on *other* PCs too (needs fleet.enabled; the other PCs' usage
    # comes from the Home Assistant integration)
    shared_budget: bool = False
    # parent PIN (required before HA can grant time / loosen limits) ----------
    parent_pin: Optional[str] = None
    parent_pin_hash: Optional[str] = None     # salted: pbkdf2_sha256$<iterations>$<salt>$<hash> (pclink pin-hash)
    parent_pin_sha256: Optional[str] = None   # the older plain SHA-256 form; still accepted
    pin_timeout_seconds: int = 300
    pin_max_attempts: int = 5
    pin_lockout_seconds: int = 300
    # also available: "parent_present" (turning the "parent is home" switch on, which can extend bedtime)
    pin_required_for: list[str] = field(default_factory=lambda: list(DEFAULT_PIN_KINDS))
    # shared defaults applied to every managed user -------------------------
    rules: list[AppRule] = field(default_factory=list)
    rewards: list[RewardConfig] = field(default_factory=list)
    time_requests: TimeRequestsConfig = field(default_factory=TimeRequestsConfig)
    # profile name -> overrides (canonical PROFILE_KEYS), for every managed user
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    breaks: dict[str, Any] = field(default_factory=dict)
    default_policy: dict[str, Any] = field(default_factory=dict)
    users: list[UserPolicy] = field(default_factory=list)

    @property
    def has_pin(self) -> bool:
        return bool(self.parent_pin or self.parent_pin_hash or self.parent_pin_sha256)

    @property
    def pin_enabled(self) -> bool:
        return self.has_pin

    @property
    def week_start_index(self) -> int:
        try:
            return _day_index(self.week_start)
        except ConfigError:
            return 0


_DEFAULT_BROWSERS = [
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "opera_gx.exe",
    "vivaldi.exe", "chromium.exe", "librewolf.exe", "waterfox.exe", "iexplore.exe",
    "floorp.exe", "thorium.exe", "arc.exe", "zen.exe",
]
_DEFAULT_GAME_PATHS = [
    r"\steamapps\common" + "\\", r"\Epic Games" + "\\", r"\GOG Galaxy\Games" + "\\", r"\GOG Games" + "\\",
    r"\XboxGames" + "\\", r"\Riot Games" + "\\", r"\Battle.net" + "\\",
    r"\Ubisoft\Ubisoft Game Launcher\games" + "\\", r"\EA Games" + "\\", r"\Origin Games" + "\\",
    r"\Rockstar Games" + "\\", r"\Program Files\Steam\steamapps" + "\\",
    r"\curseforge\minecraft" + "\\", r"\.minecraft" + "\\", r"\Roblox\Versions" + "\\",
    r"\Paradox Interactive" + "\\", r"\Bethesda.net Launcher\games" + "\\",
    # Store games from Microsoft, by package name.  Only these: Notepad, Paint, Photos,
    # Terminal, Calculator, Outlook and the other Windows apps live in WindowsApps\Microsoft.*
    # too.  (Games from the Xbox app install to XboxGames, above.)
    *(r"\WindowsApps\Microsoft." + package + "_" for package in (
        "MinecraftUWP", "MinecraftEducationEdition", "MinecraftWindowsBeta", "Lovika",  # Lovika: Minecraft Dungeons
        "MicrosoftSolitaireCollection", "MicrosoftMahjong", "MicrosoftJigsaw", "MicrosoftSudoku",
        "MicrosoftMinesweeper", "MicrosoftUltimateWordGames", "MicrosoftTreasureHunt",
        "FlightSimulator", "SeaofThieves", "254428597CFE2",  # 254428597CFE2: Forza Horizon 4
    )),
]
_DEFAULT_LAUNCHERS = [
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "galaxyclient.exe", "battle.net.exe",
    "riotclientservices.exe", "riotclientux.exe", "upc.exe", "ubisoftconnect.exe", "eadesktop.exe",
    "origin.exe", "gamingservices.exe", "xboxapp.exe", "xboxpcapp.exe", "launcher.exe",
    "minecraftlauncher.exe", "curseforge.exe", "overwolf.exe",
]
_DEFAULT_GAMES = ["javaw.exe", "robloxplayerbeta.exe", "minecraft.windows.exe", "minecraftlauncher.exe"]
_DEFAULT_GAME_TITLE_PATTERNS = [r"^Minecraft", r"^Roblox", r"Fortnite", r"League of Legends", r"VALORANT"]
_DEFAULT_NON_GAME_FULLSCREEN = [
    "explorer.exe", "vlc.exe", "mpc-hc64.exe", "mpc-hc.exe", "wmplayer.exe", "potplayermini64.exe",
    "powerpnt.exe", "zoom.exe", "teams.exe", "ms-teams.exe", "discord.exe", "obs64.exe", "video.ui.exe",
    "microsoft.media.player.exe", "netflix.exe", "spotify.exe", "lockapp.exe", "searchhost.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "applicationframehost.exe", "photos.exe",
    "vmware.exe", "vmware-vmx.exe", "virtualboxvm.exe", "mstsc.exe", "parsecd.exe", "moonlight.exe",
]


_DEFAULT_MEDIA_APPS = [
    "vlc.exe", "mpc-hc64.exe", "mpc-hc.exe", "wmplayer.exe", "potplayermini64.exe", "potplayer.exe",
    "microsoft.media.player.exe", "video.ui.exe", "netflix.exe", "spotify.exe", "itunes.exe", "musicbee.exe",
    "foobar2000.exe", "kodi.exe", "plex.exe", "plexht.exe", "jellyfinmediaplayer.exe", "mpv.exe",
    "disneyplus.exe", "primevideo.exe", "amazon video.exe",
]


@dataclass
class ActivityConfig:
    idle_seconds: int = 120
    audio_keeps_active: bool = True
    # -- presence / idle detection (see docs/PRESENCE.md) -----------------------
    away_seconds: int = 600               # no input for this long (and nothing playing) = away
    display_off_is_idle: bool = True      # monitor asleep -> seat is away
    display_off_min_idle_seconds: int = 30
    screensaver_is_idle: bool = True
    controller_input: bool = True         # game controllers count as input (XInput / joystick)
    media_keeps_active: bool = True       # a full-screen video/game that keeps the display on counts as passive use
    windows_presence: bool = True         # honour Windows' own "user not present" verdict
    now_playing: bool = True              # report the now-playing media title
    media_apps: list[str] = field(default_factory=lambda: list(_DEFAULT_MEDIA_APPS))
    browsers: list[str] = field(default_factory=lambda: list(_DEFAULT_BROWSERS))
    games: list[str] = field(default_factory=lambda: list(_DEFAULT_GAMES))
    game_title_patterns: list[str] = field(default_factory=lambda: list(_DEFAULT_GAME_TITLE_PATTERNS))
    game_paths: list[str] = field(default_factory=lambda: list(_DEFAULT_GAME_PATHS))
    launchers: list[str] = field(default_factory=lambda: list(_DEFAULT_LAUNCHERS))
    not_games: list[str] = field(default_factory=list)
    fullscreen_heuristic: bool = True
    non_game_fullscreen: list[str] = field(default_factory=lambda: list(_DEFAULT_NON_GAME_FULLSCREEN))
    site_categories: dict[str, str] = field(default_factory=dict)
    url_method: str = "uia"  # uia | title | off
    group_sites_by: str = "registrable"  # registrable | host
    top_n: int = 10
    track_unmanaged_users: bool = True
    history_days: int = 7
    report_hours: int = 10            # the "usage in the last N hours" report (apps and websites)


@dataclass
class AgentConfig:
    enabled: bool = True
    python_path: Optional[str] = None
    poll_seconds: float = 2.0
    heartbeat_seconds: float = 15.0
    audio_poll_seconds: float = 6.0
    ipc_host: str = "127.0.0.1"
    ipc_port: int = 47391
    tray_icon: bool = True
    relaunch_backoff_seconds: float = 15.0
    relaunch_backoff_max_seconds: float = 300.0
    log_level: str = "INFO"
    volume_control: bool = True   # expose the session's volume / mute to HA
    display_control: bool = True  # let HA turn a session's display off and on
    media_poll_seconds: float = 6.0
    watchdog_seconds: float = 90.0  # restart an agent that is alive but silent this long (0 disables)


@dataclass
class WindowsUpdateConfig:
    enabled: bool = False
    check_interval_hours: float = 6.0
    allow_install: bool = False   # expose the "Install updates" button


@dataclass
class UpdatesConfig:
    """Updating PC Link itself from its GitHub releases (see ``pclink/updater.py``)."""

    enabled: bool = True              # look for new releases and show them in Home Assistant
    check_interval_hours: float = 12.0
    allow_install: bool = True        # Home Assistant may start the update (standalone build only)
    auto_install: bool = False        # install new releases without being asked
    include_prereleases: bool = False
    repository: str = "cslehel/home-assistant-pc-link"   # owner/name on GitHub; its releases are trusted completely
    github_token: Optional[str] = None  # only for a private repository: a token with read access to its contents
    # Code signature of a downloaded update (see docs/SECURITY.md).  A signed copy only ever
    # accepts updates signed by the same publisher; these two settings tighten that further.
    require_signature: bool = False   # refuse unsigned updates even when the installed copy is unsigned
    trusted_signers: list[str] = field(default_factory=list)  # certificate thumbprints; when set, only these may sign


@dataclass
class ScreenshotsConfig:
    """Parent-only screenshot on demand.  Off by default; PIN-gated by default."""

    enabled: bool = False
    require_pin: bool = True
    users: list[str] = field(default_factory=list)   # empty = every *managed* user (never unlimited/parent accounts)
    notify_user: bool = True                         # tell the user a screenshot was taken
    max_width: int = 1280
    format: str = "jpeg"                             # jpeg | png
    quality: int = 60
    save_to_disk: bool = False
    keep: int = 20


@dataclass
class ApiConfig:
    """Optional local REST / WebSocket API and setup page (see docs/API.md)."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 47392
    token: Optional[str] = None       # required for writes; required for everything when not on loopback
    # A second token that can only read.  Setting it also ends token-free reads from the PC
    # itself: other users logged in on the PC can then no longer look at the API.
    read_token: Optional[str] = None
    # A third token for the dangerous part: reading and writing config.yaml, reloading it,
    # installing an update and running custom commands.  Once it is set, the write token can
    # no longer do those.
    admin_token: Optional[str] = None
    tls_cert: Optional[str] = None    # PEM certificate and key: the API is then served over HTTPS
    tls_key: Optional[str] = None     # (both or neither; `pclink make-cert` creates a pair)
    allow_config_edit: bool = False   # the setup page may write config.yaml (needs a token)
    # Other names a browser may use for this PC (a router's DNS name, say) to open the page
    # and read without a token; besides localhost and the PC's own name.  Names only: no
    # scheme, no port.
    allowed_hosts: list[str] = field(default_factory=list)
    push_interval_seconds: float = 5.0
    advertise: bool = True            # announce the API on the LAN via mDNS so Home Assistant discovers the PC


@dataclass
class FleetConfig:
    """Usage on the other PCs, which the Home Assistant integration passes on (shared budgets)."""

    enabled: bool = True
    peer_timeout_seconds: float = 180.0   # a PC Home Assistant stopped reporting is forgotten after this


@dataclass
class AppLauncher:
    """An app that HA can launch / close in a user's session."""

    name: str
    launch: Optional[str] = None      # command line to start it
    process: Optional[str] = None     # process name to close (defaults to the launch executable)
    user: Optional[str] = None        # only this user's sessions (default: every active session)
    icon: str = "mdi:application"

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def process_name(self) -> Optional[str]:
        if self.process:
            return self.process.lower()
        if self.launch:
            launch = self.launch.strip()
            head = launch[1:].split('"', 1)[0] if launch.startswith('"') else launch.split(" ")[0]
            # Windows path on any host: split on both separators
            name = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
            return name or None
        return None


@dataclass
class StorageConfig:
    path: str = ""
    flush_seconds: float = 30.0
    retention_days: int = 90


@dataclass
class LoggingConfig:
    path: str = ""
    level: str = "INFO"
    max_bytes: int = 2_000_000
    backups: int = 5


@dataclass
class PowerConfig:
    allow_shutdown: bool = True
    allow_reboot: bool = True
    allow_sleep: bool = True
    allow_hibernate: bool = True
    shutdown_delay_seconds: int = 10
    wol_broadcast: str = "255.255.255.255"
    wol_port: int = 9


@dataclass
class CustomCommand:
    name: str
    command: str
    run_as: str = "system"  # system | user
    user: Optional[str] = None  # for run_as: user -> which user's session (default: all active)
    icon: str = "mdi:console"

    @property
    def slug(self) -> str:
        return slugify(self.name)


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    hardware_sensors: HardwareSensorsConfig = field(default_factory=HardwareSensorsConfig)
    time_limits: TimeLimitsConfig = field(default_factory=TimeLimitsConfig)
    activity: ActivityConfig = field(default_factory=ActivityConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    windows_update: WindowsUpdateConfig = field(default_factory=WindowsUpdateConfig)
    screenshots: ScreenshotsConfig = field(default_factory=ScreenshotsConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
    commands: list[CustomCommand] = field(default_factory=list)
    apps: list[AppLauncher] = field(default_factory=list)
    source_path: Optional[str] = None

    # -- helpers ------------------------------------------------------------
    def user_policy(self, user: str) -> Optional[UserPolicy]:
        key = user.lower()
        for policy in self.time_limits.users:
            if policy.key == key:
                return policy
        return None

    def policies_by_key(self) -> dict[str, UserPolicy]:
        return {p.key: p for p in self.time_limits.users}

    def build_default_policy(self, user: str) -> UserPolicy:
        """Policy for a user that is not listed (used when manage_unknown_users is on)."""
        raw = dict(self.time_limits.default_policy)
        raw["name"] = user
        return parse_user_policy(raw, {}, shared=self.time_limits)

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        path = path or default_config_path()
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        try:
            cfg = cls.from_yaml(text)
        except ConfigError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        cfg.source_path = path
        return cfg

    @classmethod
    def from_yaml(cls, text: str) -> "Config":
        """Build and validate a configuration from YAML text (what ``load`` reads from the
        file).  Unlike ``from_dict`` this still sees how a value was *written*, which is
        what tells an unquoted PIN that YAML changed (``0123`` is read as 83) from one it
        left alone."""
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("top level must be a mapping")
        return cls.from_dict(raw, pin_text=_pin_as_written(text))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], pin_text: Optional[str] = None) -> "Config":
        """Build and validate a configuration; every problem is reported as ``ConfigError``.

        ``pin_text``: how ``time_limits.parent_pin`` was written in the file (``from_yaml``
        passes it)."""
        try:
            return cls._from_dict(raw, pin_text)
        except ConfigError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
            raise ConfigError(f"invalid configuration: {exc}") from exc

    @classmethod
    def _from_dict(cls, raw: dict[str, Any], pin_text: Optional[str] = None) -> "Config":
        if raw is not None and not isinstance(raw, dict):
            raise ConfigError("top level must be a mapping")
        raw = dict(raw or {})
        cfg = cls()
        cfg.device = _fill(DeviceConfig, raw.get("device"))
        cfg.telemetry = _fill(TelemetryConfig, raw.get("telemetry"))
        cfg.hardware_sensors = _fill(HardwareSensorsConfig, raw.get("hardware_sensors"))
        cfg.activity = _fill(ActivityConfig, raw.get("activity"))
        cfg.agent = _fill(AgentConfig, raw.get("agent"))
        cfg.storage = _fill(StorageConfig, raw.get("storage"))
        cfg.logging = _fill(LoggingConfig, raw.get("logging"))
        cfg.power = _fill(PowerConfig, raw.get("power"))
        cfg.windows_update = _fill(WindowsUpdateConfig, raw.get("windows_update"))
        cfg.screenshots = _fill(ScreenshotsConfig, raw.get("screenshots"))
        cfg.updates = _fill(UpdatesConfig, raw.get("updates"))
        if not _REPOSITORY.match(cfg.updates.repository):
            raise ConfigError("updates.repository must be a GitHub repository of the form owner/name")
        cfg.api = _fill(ApiConfig, raw.get("api"))
        cfg.fleet = _fill(FleetConfig, raw.get("fleet"))

        tl_raw = dict(raw.get("time_limits") or {})
        users_raw = tl_raw.pop("users", []) or []
        default_policy = dict(tl_raw.pop("default_policy", {}) or {})
        shared_rules_raw = tl_raw.pop("rules", []) or []
        shared_rewards_raw = tl_raw.pop("rewards", []) or []
        shared_breaks_raw = tl_raw.pop("breaks", None)
        time_requests_raw = tl_raw.pop("time_requests", None)
        profiles_raw = tl_raw.pop("profiles", None)
        cfg.time_limits = _fill(TimeLimitsConfig, tl_raw)
        cfg.time_limits.default_policy = default_policy
        cfg.time_limits.rules = parse_rules(shared_rules_raw, "time_limits.rules")
        cfg.time_limits.rewards = parse_rewards(shared_rewards_raw, "time_limits.rewards")
        cfg.time_limits.breaks = _breaks_dict(shared_breaks_raw)
        cfg.time_limits.time_requests = parse_time_requests(time_requests_raw)
        cfg.time_limits.profiles = parse_profiles(profiles_raw, "time_limits.profiles")
        _check_pin(cfg.time_limits, pin_text)
        cfg.time_limits.pin_required_for = _pin_kinds(cfg.time_limits.pin_required_for)
        try:
            _day_index(cfg.time_limits.week_start)
        except ConfigError:
            raise ConfigError("time_limits.week_start must be a day name (mon..sun)") from None
        zone = str(cfg.time_limits.time_zone or "").strip()
        cfg.time_limits.time_zone = None if zone.lower() in ("", "local", "system") else zone
        if cfg.time_limits.time_zone:
            try:
                time_zone(cfg.time_limits.time_zone)
            except ValueError as exc:
                raise ConfigError(f"time_limits.time_zone: {exc}") from None
        if default_policy:
            # Unlisted users get this policy when manage_unknown_users is on: a mistake in it
            # must show now, not when the first such user logs in.
            try:
                parse_user_policy(dict(default_policy, name="default_policy"), {}, shared=cfg.time_limits)
            except (ConfigError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
                if cfg.time_limits.manage_unknown_users:
                    raise ConfigError(f"time_limits.default_policy: {exc}") from None
                log.warning("time_limits.default_policy is not valid (%s)", exc)
        # shared rules/rewards/breaks become defaults for every user block
        inherited = dict(default_policy)
        if isinstance(users_raw, dict):  # allow {name: {...}} mapping form
            users_raw = [dict(v or {}, name=k) for k, v in users_raw.items()]
        seen: set[str] = set()
        slugs: dict[str, str] = {}
        for item in users_raw:
            if isinstance(item, str):
                item = {"name": item}
            policy = parse_user_policy(item, inherited, shared=cfg.time_limits)
            if policy.key in seen:
                raise ConfigError(f"duplicate user {policy.name!r} in time_limits.users")
            seen.add(policy.key)
            if policy.slug in slugs:
                # the slug is the user's id in topics, entities and commands: two users
                # sharing one would receive each other's commands
                raise ConfigError(f"time_limits.users: {slugs[policy.slug]!r} and {policy.name!r} get the same id "
                                  f"{policy.slug!r} in Home Assistant; rename one of the accounts' entries")
            slugs[policy.slug] = policy.name
            cfg.time_limits.users.append(policy)
        if cfg.time_limits.block_method not in ("logon_hours", "disable_account", "logoff_only"):
            raise ConfigError("time_limits.block_method must be logon_hours, disable_account or logoff_only")

        cmds_raw = raw.get("commands") or {}
        if isinstance(cmds_raw, dict):
            cmds_raw = [dict(v if isinstance(v, dict) else {"command": v}, name=k) for k, v in cmds_raw.items()]
        for item in cmds_raw:
            cmd = _fill(CustomCommand, item, required=("name", "command"))
            if cmd.run_as not in ("system", "user"):
                raise ConfigError(f"command {cmd.name!r}: run_as must be system or user")
            cfg.commands.append(cmd)

        apps_raw = raw.get("apps") or {}
        if isinstance(apps_raw, dict):
            apps_raw = [dict(v if isinstance(v, dict) else {"launch": v}, name=k) for k, v in apps_raw.items()]
        for item in apps_raw:
            app = _fill(AppLauncher, item, required=("name",))
            if not app.launch and not app.process:
                raise ConfigError(f"app {app.name!r}: needs 'launch' and/or 'process'")
            if app.user:
                app.user = str(app.user).lower()
            cfg.apps.append(app)

        _warn_duplicates("commands", [c.slug for c in cfg.commands])
        _warn_duplicates("apps", [a.slug for a in cfg.apps])
        _check_ranges(cfg)
        _effective_values(cfg)
        for pattern in cfg.activity.game_title_patterns:
            try:
                re.compile(str(pattern), re.I)
            except re.error as exc:
                raise ConfigError(f"activity.game_title_patterns: {pattern!r} is not a valid "
                                  f"regular expression ({exc})") from None

        if cfg.screenshots.format not in ("jpeg", "png"):
            raise ConfigError("screenshots.format must be jpeg or png")
        cfg.screenshots.users = [str(u).lower() for u in (cfg.screenshots.users or [])]
        if cfg.api.token is not None:
            cfg.api.token = str(cfg.api.token) or None
        if cfg.api.read_token is not None:
            cfg.api.read_token = str(cfg.api.read_token) or None
        if cfg.api.read_token and cfg.api.read_token == cfg.api.token:
            raise ConfigError("api.read_token must differ from api.token (it only reads; api.token may do everything)")
        if cfg.api.admin_token is not None:
            cfg.api.admin_token = str(cfg.api.admin_token) or None
        if cfg.api.admin_token and cfg.api.admin_token in (cfg.api.token, cfg.api.read_token):
            raise ConfigError("api.admin_token must differ from api.token and api.read_token")
        for key in ("tls_cert", "tls_key"):
            value = getattr(cfg.api, key)
            setattr(cfg.api, key, (str(value).strip() or None) if value is not None else None)
        if bool(cfg.api.tls_cert) != bool(cfg.api.tls_key):
            raise ConfigError("api.tls_cert and api.tls_key go together: set both or neither")
        cfg.api.allowed_hosts = _host_names(cfg.api.allowed_hosts, "api.allowed_hosts", 20)
        cfg.updates.trusted_signers = [_thumbprint(x) for x in cfg.updates.trusted_signers]
        if cfg.api.enabled and cfg.api.host not in ("127.0.0.1", "localhost", "::1"):
            if not cfg.api.token:
                raise ConfigError("api.token is required when api.host is not loopback")
            if len(cfg.api.token) < 16:
                log.warning("api.token is short (%d characters) for an API that can be reached from the network; "
                            "use at least 16", len(cfg.api.token))

        # Derived defaults -------------------------------------------------
        hostname = socket.gethostname()
        if not cfg.device.name:
            cfg.device.name = hostname
        if not cfg.device.id:
            cfg.device.id = slugify(cfg.device.name)
        base = _program_data()
        if not cfg.storage.path:
            cfg.storage.path = os.path.join(base, "pclink.db")
        if not cfg.logging.path:
            cfg.logging.path = os.path.join(base, "logs")
        if cfg.activity.url_method not in ("uia", "title", "off"):
            raise ConfigError("activity.url_method must be uia, title or off")
        if cfg.activity.group_sites_by not in ("registrable", "host"):
            raise ConfigError("activity.group_sites_by must be registrable or host")
        cfg.activity.site_categories = {
            normalize_site(k) or str(k).lower().lstrip("*.").strip(): str(v).lower()
            for k, v in (cfg.activity.site_categories or {}).items()
        }
        _warn_unmatchable_sites(cfg)
        return cfg


def _parse_warn(value: Any, default: list[int]) -> list[int]:
    warn = default if value is None else value
    if isinstance(warn, (int, float)):
        warn = [warn]
    return sorted({int(w) for w in (warn or []) if int(w) > 0}, reverse=True)


def _parse_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in value.replace(";", ",").split(",")]
    return [str(v).strip().lower().lstrip("*.") for v in value if str(v).strip()]


def _has_path(site: str) -> bool:
    """Does a website as written name more than the site (``youtube.com/shorts``)?"""
    text = site.split("://", 1)[-1]
    rest = re.split(r"[/?#]", text, maxsplit=1)
    return len(rest) > 1 and rest[1].strip("/?#") != ""


def _parse_apps(value: Any, where: str) -> list[str]:
    """Program names as rules match them: the process name.  A path
    (``C:\\Games\\x.exe``) is cut to its file name, which is what is compared."""
    out: list[str] = []
    for name in _parse_names(value):
        base = name.replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1].strip()
        if base != name:
            log.warning("%s: %r is a path, but rules match programs by name - it is read as %r "
                        "(every program of that name)", where, name, base)
        if base and base not in out:
            out.append(base)
    return out


def _yes_no(value: Any, default: bool, where: str) -> bool:
    """A yes / no setting: true / false (YAML's yes / no / on / off included), also as
    text.  Anything else is an error - ``bool("false")`` would be true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:  # (_TRUE / _FALSE: see _coerce)
        return value.strip().lower() in _TRUE
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ConfigError(f"{where} must be true or false (got {value!r})")


def _parse_sites(value: Any, where: str) -> list[str]:
    """Website names as rules match them (``util.normalize_site``): however a site was
    written - ``www.youtube.com``, ``https://youtube.com/`` - it becomes ``youtube.com``."""
    out: list[str] = []
    for name in _parse_names(value):
        site = normalize_site(name)
        if site is None:
            log.warning("%s: %r does not look like a website (expected e.g. youtube.com); kept as written",
                        where, name)
            site = name
        elif _has_path(name):
            # rules match whole sites: youtube.com/shorts would quietly cover all of youtube.com
            log.warning("%s: %r has a path, but rules match whole websites - it is read as %r (all of it)",
                        where, name, site)
        elif site != name:
            log.info("%s: site %r is read as %r", where, name, site)
        if site not in out:
            out.append(site)
    return out


def _parse_hhmm_opt(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"invalid time of day: {raw!r}")
    if isinstance(raw, (int, float)):
        # YAML 1.1 reads an unquoted "7:30" as the sexagesimal integer 450 (minutes);
        # a bare small number ("bedtime: 21") is taken as an hour.
        value = int(raw)
        if 0 <= value <= 24:
            return value * 60 if value < 24 else 1440
        if 0 <= value <= 1440:
            return value
        raise ValueError(f"invalid time of day: {raw!r} (quote it, e.g. \"{raw}\")")
    s = str(raw).strip().lower()
    if s in ("", "none", "off", "never"):
        return None
    return parse_hhmm(s)


_HOST_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def _host_names(value: Any, where: str, limit: int) -> list[str]:
    """A list of host names (or IP addresses), lower-cased: no scheme, no port, no path."""
    out: list[str] = []
    for item in value or []:
        if isinstance(item, (bool, dict, list)):
            raise ConfigError(f"{where}: {item!r} is not a host name")
        host = str(item).strip().lower().rstrip(".")
        ipv6 = host.startswith("[") and host.endswith("]")
        if not (_HOST_NAME.match(host) or is_ip(host[1:-1] if ipv6 else host)):
            raise ConfigError(f"{where}: {item!r} is not a host name (write the name only: no http://, no port)")
        host = host[1:-1] if ipv6 else host
        if host not in out:
            out.append(host)
    if len(out) > limit:
        raise ConfigError(f"{where}: at most {limit} names")
    return out


def _pin_as_written(text: str) -> Optional[str]:
    """``time_limits.parent_pin`` exactly as it stands in the YAML text, or None."""
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return None
    for key in ("time_limits", "parent_pin"):
        if not isinstance(node, yaml.MappingNode):
            return None
        node = next((v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == key), None)
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _check_pin(tl: "TimeLimitsConfig", pin_text: Optional[str]) -> None:
    """Validate the three ways a parent PIN can be given (plain, salted hash, SHA-256)."""
    pin = tl.parent_pin
    if pin is not None and not isinstance(pin, str):
        quoted = 'put it in quotes: parent_pin: "1234"'
        if isinstance(pin, bool) or not isinstance(pin, int):
            raise ConfigError(f"time_limits.parent_pin is read as {pin!r}, not as text - {quoted}")
        if pin_text is not None and pin_text.strip() != str(pin):
            # 0123 is the octal number 83 to YAML, 0000 is 0, 1_000 is 1000: the PIN that
            # would be in force is not the one that was typed
            raise ConfigError(f"time_limits.parent_pin is written as {pin_text.strip()} without quotes, which "
                              f"YAML reads as the number {pin} - {quoted}")
        log.warning("time_limits.parent_pin is not quoted; it is read as %r - %s", str(pin), quoted)
    if pin is not None:
        tl.parent_pin = str(pin)
    if tl.parent_pin_hash is not None:
        value = str(tl.parent_pin_hash).strip()
        tl.parent_pin_hash = value or None
        if value:
            if not _PBKDF2_HASH.match(value) or int(value.split("$")[1]) > 10_000_000:
                raise ConfigError("time_limits.parent_pin_hash is not a PIN hash "
                                  "(pbkdf2_sha256$<iterations>$<salt>$<hash>; `pclink pin-hash` prints one)")
    if tl.parent_pin_sha256 is not None:
        value = str(tl.parent_pin_sha256).strip().lower()
        tl.parent_pin_sha256 = value or None
        if value and not _SHA256_HEX.match(value):
            # it never matched any PIN before either; keep refusing rather than stop the service
            log.error("time_limits.parent_pin_sha256 is not a SHA-256 digest (64 hex digits): no PIN will "
                      "ever be accepted - make a new one with `pclink pin-hash`")


def _pin_kinds(value: Any) -> list[str]:
    """``pin_required_for`` with every entry checked: a misspelt kind would silently
    leave that kind of action without the PIN."""
    out: list[str] = []
    for item in value or []:
        kind = str(item).strip().lower()
        if kind not in PIN_KINDS:
            close = difflib.get_close_matches(kind, PIN_KINDS, n=1, cutoff=0.75)
            if close:
                log.warning("time_limits.pin_required_for: %r is not a kind of action; read as %r", kind, close[0])
                kind = close[0]
            else:
                log.warning("time_limits.pin_required_for: %r is not a kind of action and guards nothing "
                            "(known: %s)", kind, ", ".join(PIN_KINDS))
        if kind not in out:
            out.append(kind)
    return out


def _thumbprint(value: Any) -> str:
    """A certificate thumbprint as Windows prints it, however it was typed (spaces, colons, case)."""
    text = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    if len(text) not in (40, 64):
        raise ConfigError(f"updates.trusted_signers: {value!r} is not a certificate thumbprint (40 or 64 hex digits)")
    return text


def _plain(value: Any) -> Any:
    """``value`` reduced to what JSON can hold (rule definitions are stored and sent to Home Assistant)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def parse_rule(item: Any, where: str) -> AppRule:
    if isinstance(item, str):
        item = {"name": item, "apps": [item] if item.lower().endswith(".exe") else [], "sites": [] if item.lower().endswith(".exe") else [item], "blocked": True}
    if not isinstance(item, dict) or not item.get("name"):
        raise ConfigError(f"{where}: rule entries need a 'name': {item!r}")
    rule = AppRule(name=str(item["name"]).strip())
    rule.apps = _parse_apps(item.get("apps", item.get("app")), f"{where}: rule {rule.name!r}")
    rule.sites = _parse_sites(item.get("sites", item.get("site", item.get("domains"))), f"{where}: rule {rule.name!r}")
    rule.categories = _parse_names(item.get("categories", item.get("category")))
    if not (rule.apps or rule.sites or rule.categories):
        raise ConfigError(f"{where}: rule {rule.name!r} needs apps, sites or categories")
    daily_raw = item.get("daily_limit_minutes", item.get("daily_limit"))
    weekly_raw = item.get("weekly_limit_minutes", item.get("weekly_limit"))
    rule.daily_limits = by_day(daily_raw, _parse_minutes, None)
    rule.weekly_limit_minutes = _parse_minutes(weekly_raw)
    if rule.weekly_limit_minutes == 0:
        raise ConfigError(f"{where}: rule {rule.name!r}: weekly_limit_minutes must be positive (use blocked: true to forbid it)")
    try:
        rule.allowed_windows = by_day_windows(item.get("allowed_hours"))
    except ValueError as exc:
        raise ConfigError(f"{where}: rule {rule.name!r}: {exc}") from None
    rule.blocked = _yes_no(item.get("blocked", item.get("block")), False, f"{where}: rule {rule.name!r}: blocked")
    rule.warn_at_minutes = _parse_warn(item.get("warn_at_minutes"), [5, 1])
    rule.action = str(item.get("action", "close")).lower()
    if rule.action not in ("close", "warn"):
        raise ConfigError(f"{where}: rule {rule.name!r}: action must be close or warn")
    # the message is put together with text every tick: a number or a list must not stop the rules
    message = item.get("message")
    rule.message = None if message is None else (str(message).strip() or None)
    rule.icon = str(item.get("icon", rule.icon))
    if not (rule.blocked or rule.has_limit or rule.has_weekly_limit or rule.has_hours):
        raise ConfigError(f"{where}: rule {rule.name!r} needs daily_limit_minutes, weekly_limit_minutes, "
                          "allowed_hours or blocked: true")
    rule.definition = _plain({
        "name": rule.name, "apps": rule.apps, "sites": rule.sites, "categories": rule.categories,
        "daily_limit_minutes": daily_raw, "weekly_limit_minutes": weekly_raw,
        "allowed_hours": item.get("allowed_hours"), "blocked": rule.blocked,
        "warn_at_minutes": rule.warn_at_minutes, "action": rule.action, "message": rule.message, "icon": rule.icon,
    })
    return rule


def parse_rules(raw: Any, where: str) -> list[AppRule]:
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [dict(v if isinstance(v, dict) else {}, name=k) for k, v in raw.items()]
    rules = [parse_rule(item, where) for item in raw]
    seen: set[str] = set()
    for r in rules:
        if r.slug in seen:
            raise ConfigError(f"{where}: duplicate rule {r.name!r}")
        seen.add(r.slug)
    check_rule_ids(rules, where)
    return rules


def rule_id_clash(rules: list[AppRule]) -> Optional[tuple[AppRule, AppRule]]:
    """Two rules (of different names) whose Home Assistant entities would get the same id:
    "YouTube" with a weekly budget has ``rule_youtube_weekly_remaining``, and so does a rule
    called "YouTube weekly" with a daily one.  None when there is no such pair."""
    owner: dict[str, AppRule] = {}
    for rule in rules:
        for oid in rule.entity_ids:
            other = owner.get(oid)
            if other is not None and other.slug != rule.slug:
                return other, rule
            owner[oid] = rule
    return None


def check_rule_ids(rules: list[AppRule], where: str) -> None:
    """``ConfigError`` when two of ``rules`` would give entities the same id (see ``rule_id_clash``)."""
    clash = rule_id_clash(rules)
    if clash is not None:
        first, second = clash
        raise ConfigError(f"{where}: the rules {first.name!r} and {second.name!r} would give Home Assistant "
                          f"entities the same id; rename one of them")


def parse_rewards(raw: Any, where: str) -> list[RewardConfig]:
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [dict(v if isinstance(v, dict) else {"minutes": v}, name=k) for k, v in raw.items()]
    out: list[RewardConfig] = []
    for item in raw:
        if isinstance(item, str):
            item = {"name": item}
        reward = _fill(RewardConfig, item, required=("name",), lenient=("minutes",))
        reward.minutes = int(_parse_minutes(reward.minutes) or 0)
        if reward.minutes <= 0:
            raise ConfigError(f"{where}: reward {reward.name!r} needs positive minutes")
        out.append(reward)
    return out


def _breaks_dict(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, bool):
        return {"enabled": raw}
    if isinstance(raw, (int, float)):
        return {"enabled": True, "every_minutes": int(raw)}
    if not isinstance(raw, dict):
        raise ConfigError("breaks must be a mapping, a number of minutes or true/false")
    return dict(raw)


def parse_breaks(raw: Any, defaults: Optional[dict[str, Any]] = None) -> BreakConfig:
    merged = dict(defaults or {})
    merged.update(_breaks_dict(raw))
    if "enabled" not in merged and merged:
        merged["enabled"] = True
    breaks = _fill(BreakConfig, merged)
    breaks.every_minutes = max(1, int(breaks.every_minutes))
    for key, high in (("duration_seconds", 3600), ("reset_after_idle_minutes", 1440)):
        if not 0 <= getattr(breaks, key) <= high:
            raise ConfigError(f"breaks.{key} must be between 0 and {high} (got {getattr(breaks, key)!r})")
    return breaks


def parse_focus(raw: Any, name: str) -> FocusConfig:
    if raw is None:
        return FocusConfig()
    if isinstance(raw, list):
        raw = {"allow_apps": [x for x in raw if str(x).lower().endswith(".exe")],
               "allow_sites": [x for x in raw if not str(x).lower().endswith(".exe")]}
    if not isinstance(raw, dict):
        raise ConfigError(f"user {name!r}: focus must be a mapping")
    raw = dict(raw)
    schedule_raw = raw.pop("schedule", None)
    focus = _fill(FocusConfig, raw)
    focus.allow_apps = _parse_apps(focus.allow_apps, f"user {name!r}: focus.allow_apps")
    focus.allow_sites = _parse_sites(focus.allow_sites, f"user {name!r}: focus.allow_sites")
    focus.allow_categories = _parse_names(focus.allow_categories)
    if focus.action not in ("close", "warn"):
        raise ConfigError(f"user {name!r}: focus.action must be close or warn")
    try:
        # a day without focus windows has no focus: the tail of a window that runs past midnight stays
        focus.schedule = by_day_windows(schedule_raw, empty_is_open=False)
    except ValueError as exc:
        raise ConfigError(f"user {name!r}: focus.schedule: {exc}") from None
    focus.default_minutes = max(1, int(focus.default_minutes))
    return focus


def parse_time_requests(raw: Any) -> TimeRequestsConfig:
    """``time_limits.time_requests``: a mapping, or just true / false."""
    if raw is None:
        return TimeRequestsConfig()
    if isinstance(raw, bool):
        return TimeRequestsConfig(enabled=raw)
    if not isinstance(raw, dict):
        raise ConfigError("time_limits.time_requests must be a mapping or true / false")
    req = _fill(TimeRequestsConfig, raw, lenient=("options",))
    options = req.options
    if isinstance(options, (int, float, str)) and not isinstance(options, bool):
        options = [options]
    if not isinstance(options, (list, tuple)):
        raise ConfigError("time_limits.time_requests.options must be a list of minutes")
    minutes: list[int] = []
    for value in options:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(f"time_limits.time_requests.options: {value!r} is not a number of minutes")
        try:
            number = float(value)
        except ValueError:
            raise ConfigError(f"time_limits.time_requests.options: {value!r} is not a number of minutes") from None
        if not math.isfinite(number) or number != int(number) or not 1 <= number <= 240:
            raise ConfigError(f"time_limits.time_requests.options: {value!r} must be 1 to 240 whole minutes")
        if int(number) not in minutes:
            minutes.append(int(number))
    if len(minutes) > 6:
        raise ConfigError("time_limits.time_requests.options: at most 6 options")
    req.options = sorted(minutes)
    for key, low in (("cooldown_minutes", 0), ("expire_minutes", 1), ("max_per_day", 1)):
        if getattr(req, key) < low:
            raise ConfigError(f"time_limits.time_requests.{key} must be at least {low} (got {getattr(req, key)!r})")
    return req


def parse_profiles(raw: Any, where: str) -> dict[str, dict[str, Any]]:
    """``{name: {overrides}}`` with the names and keys checked and the keys in their
    canonical spelling (``PROFILE_KEYS``).  The values are parsed when a profile is
    applied to a user's policy, where they mean the same as in the policy itself."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping of profile name to overrides")
    canonical = {alias: key for key, aliases in PROFILE_KEYS.items() for alias in aliases}
    out: dict[str, dict[str, Any]] = {}
    for name, overrides in raw.items():
        key = str(name).strip().lower()
        if key == NORMAL_PROFILE:
            raise ConfigError(f"{where}: {NORMAL_PROFILE!r} is reserved (it means no profile)")
        if not _PROFILE_NAME.match(key):
            raise ConfigError(f"{where}: profile name {name!r} must be letters, digits, _ or - (at most 32)")
        if key in out:
            raise ConfigError(f"{where}: duplicate profile {key!r}")
        if not isinstance(overrides, dict) or not overrides:
            raise ConfigError(f"{where}: profile {key!r} needs at least one of {', '.join(PROFILE_KEYS)}")
        values: dict[str, Any] = {}
        for field_name, value in overrides.items():
            target = canonical.get(str(field_name), str(field_name))
            if target not in PROFILE_KEYS:
                raise ConfigError(f"{where}: profile {key!r} cannot set {field_name!r} "
                                  f"(only {', '.join(PROFILE_KEYS)})")
            values[target] = value
        out[key] = values
    return out


def _apply_profile(merged: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """A user's raw policy with a profile's overrides in place of the user's own values."""
    out = {k: v for k, v in merged.items() if k != "profiles"}
    for key, value in overrides.items():
        for alias in PROFILE_KEYS[key]:
            out.pop(alias, None)
        out[key] = value
    return out


DEFAULT_WAKE_MINUTE = 6 * 60   # the end of a night whose wake time is not configured


def _wake_times(bedtime: dict[int, Optional[int]], wake: dict[int, Optional[int]], name: str,
                quiet: bool = False) -> dict[int, int]:
    """The wake time of every day.  Without one a bedtime night would end at midnight -
    ``bedtime: "21:00"`` closed only 21:00-24:00, and a bedtime after midnight closed the
    whole next day - so a day whose night needs a wake time (it has a bedtime, or the
    evening before had one) and has none gets ``DEFAULT_WAKE_MINUTE``, with a warning."""
    out: dict[int, int] = {}
    defaulted: list[str] = []
    for day in range(7):
        value = wake.get(day)
        if value is None:
            needs = bedtime.get(day) is not None or bedtime.get((day - 1) % 7) is not None
            value = DEFAULT_WAKE_MINUTE if needs else 0
            if needs:
                defaulted.append(DAYS[day])
        out[day] = value
    if defaulted:
        (log.debug if quiet else log.warning)(
            "user %r: a bedtime without a wake_time - the night ends at %02d:%02d on %s; set wake_time to say when",
            name, DEFAULT_WAKE_MINUTE // 60, DEFAULT_WAKE_MINUTE % 60, ", ".join(defaulted))
    return out


def _policy_number(value: Any, default: float, where: str, low: float, high: float) -> float:
    """A number of a user's policy, checked: ``ConfigError`` when it is not one or out of range."""
    if value is None:
        return default
    try:
        number = float(value) if not isinstance(value, bool) and isinstance(value, (int, float, str)) else math.nan
    except (ValueError, OverflowError):
        number = math.nan
    if not math.isfinite(number):
        raise ConfigError(f"{where} must be a number (got {value!r})")
    if not low <= number <= high:
        raise ConfigError(f"{where} must be between {low:g} and {high:g} (got {value!r})")
    return number


def parse_user_policy(item: dict[str, Any], defaults: dict[str, Any], shared: Optional[TimeLimitsConfig] = None,
                      _with_profiles: bool = True) -> UserPolicy:
    if not isinstance(item, dict) or not item.get("name"):
        raise ConfigError(f"time_limits.users entries need a 'name': {item!r}")
    merged = dict(defaults)
    merged.update(item)
    name = str(merged["name"]).strip()
    policy = UserPolicy(name=name)
    policy.display_name = merged.get("display_name")
    where = f"user {name!r}"
    policy.unlimited = _yes_no(merged.get("unlimited"), False, f"{where}: unlimited")
    limit_raw = merged.get("daily_limit_minutes", merged.get("daily_limit"))
    policy.daily_limits = by_day(limit_raw, _parse_minutes, None)
    hours_raw = merged.get("allowed_hours", merged.get("allowed_windows"))
    try:
        policy.allowed_windows = by_day_windows(hours_raw)
    except ValueError as exc:
        raise ConfigError(f"user {name!r}: {exc}") from None
    policy.warn_at_minutes = _parse_warn(merged.get("warn_at_minutes"), [15, 5, 1])
    policy.action = str(merged.get("action", "logoff")).lower()
    if policy.action not in ("logoff", "lock", "shutdown"):
        raise ConfigError(f"user {name!r}: action must be logoff, lock or shutdown")
    policy.grace_seconds = int(_policy_number(merged.get("grace_seconds"), 60, f"{where}: grace_seconds", 0, 3600))
    policy.count_idle = _yes_no(merged.get("count_idle"), True, f"{where}: count_idle")
    idle_minutes = merged.get("idle_threshold_minutes")
    if idle_minutes is not None:
        policy.idle_threshold_seconds = int(_policy_number(idle_minutes, 10.0, f"{where}: idle_threshold_minutes",
                                                           0, 1440) * 60)
    else:
        policy.idle_threshold_seconds = int(_policy_number(merged.get("idle_threshold_seconds"), 600,
                                                           f"{where}: idle_threshold_seconds", 0, 86400))
    if merged.get("count_away") is not None:
        policy.count_away = _yes_no(merged.get("count_away"), True, f"{where}: count_away")
    policy.count_passive = _yes_no(merged.get("count_passive"), True, f"{where}: count_passive")
    policy.count_locked = _yes_no(merged.get("count_locked"), False, f"{where}: count_locked")
    policy.block_login = _yes_no(merged.get("block_login"), True, f"{where}: block_login")
    # weekly budget / rollover -----------------------------------------------
    policy.weekly_limit_minutes = _parse_minutes(merged.get("weekly_limit_minutes", merged.get("weekly_limit")))
    if policy.weekly_limit_minutes == 0:
        log.warning("user %r: weekly_limit_minutes is 0 - no computer time at all, every week "
                    "(leave it out or write \"unlimited\" for no weekly limit)", name)
    rollover = merged.get("rollover_max_minutes", merged.get("rollover"))
    if isinstance(rollover, bool):
        rollover = 1440 if rollover else 0
    policy.rollover_max_minutes = int(_parse_minutes(rollover) or 0) if rollover is not None else 0
    # bedtime ---------------------------------------------------------------
    try:
        policy.bedtime = by_day(merged.get("bedtime"), _parse_hhmm_opt, None)
        wake = by_day(merged.get("wake_time", merged.get("wake")), _parse_hhmm_opt, None)
    except ValueError as exc:
        raise ConfigError(f"user {name!r}: bedtime: {exc}") from None
    policy.wake_time = _wake_times(policy.bedtime, wake, name, quiet=not _with_profiles)
    policy.bedtime_parent_extension_minutes = int(_parse_minutes(merged.get("bedtime_parent_extension_minutes", 0)) or 0)
    # rules / focus / breaks / rewards ------------------------------------------
    own_rules = parse_rules(merged.get("rules"), f"user {name!r}: rules")
    shared_rules = list(shared.rules) if shared else []
    own_slugs = {r.slug for r in own_rules}
    policy.rules = [r for r in shared_rules if r.slug not in own_slugs] + own_rules
    check_rule_ids(policy.rules, f"user {name!r}: rules (with time_limits.rules)")
    policy.focus = parse_focus(merged.get("focus"), name)
    policy.breaks = parse_breaks(merged.get("breaks"), shared.breaks if shared else None)
    own_rewards = parse_rewards(merged.get("rewards"), f"user {name!r}: rewards")
    shared_rewards = list(shared.rewards) if shared else []
    own_reward_slugs = {r.slug for r in own_rewards}
    policy.rewards = [r for r in shared_rewards if r.slug not in own_reward_slugs] + own_rewards
    if _with_profiles and not policy.unlimited:
        # the shared profiles, a user's own entry of the same name replacing the shared one
        profiles = dict(shared.profiles) if shared else {}
        profiles.update(parse_profiles(merged.get("profiles"), f"user {name!r}: profiles"))
        for profile, overrides in profiles.items():
            try:
                policy.profiles[profile] = parse_user_policy(_apply_profile(merged, overrides), {}, shared=shared,
                                                             _with_profiles=False)
            except ConfigError as exc:
                raise ConfigError(f"profile {profile!r}: {exc}") from None
            except (ValueError, TypeError, OverflowError) as exc:
                raise ConfigError(f"user {name!r}: profile {profile!r}: {exc}") from None
    return policy


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")

# (section, key, minimum, maximum) - values outside are a configuration error
_RANGES = (
    ("time_limits", "tick_seconds", 0.2, 3600), ("time_limits", "reassert_block_seconds", 1, None),
    ("time_limits", "pin_timeout_seconds", 0, None), ("time_limits", "pin_max_attempts", 1, None),
    ("time_limits", "pin_lockout_seconds", 0, None),
    ("storage", "flush_seconds", 0, 300), ("storage", "retention_days", 1, None),
    ("activity", "idle_seconds", 10, 86400), ("activity", "away_seconds", 10, 86400),
    ("activity", "display_off_min_idle_seconds", 0, 86400), ("activity", "history_days", 1, 60),
    ("activity", "top_n", 1, 100),
    ("logging", "max_bytes", 100_000, None), ("logging", "backups", 0, 100),
    ("telemetry", "interval_seconds", 0, None), ("api", "push_interval_seconds", 0, 60),
    ("api", "port", 1, 65535), ("agent", "ipc_port", 1, 65535),
    ("agent", "poll_seconds", 0.2, None), ("agent", "heartbeat_seconds", 1, None),
    ("agent", "watchdog_seconds", 0, None),
    ("screenshots", "keep", 1, None), ("screenshots", "quality", 1, 100), ("screenshots", "max_width", 160, 7680),
    ("activity", "report_hours", 1, 336), ("fleet", "peer_timeout_seconds", 1, None), ("updates", "check_interval_hours", 1, None), ("power", "shutdown_delay_seconds", 0, None),
)


def _check_ranges(cfg: "Config") -> None:
    for section, key, low, high in _RANGES:
        value = getattr(getattr(cfg, section), key)
        if (low is not None and value < low) or (high is not None and value > high):
            bounds = f"at least {low}" if high is None else f"between {low} and {high}"
            raise ConfigError(f"{section}.{key} must be {bounds} (got {value!r})")
    if cfg.activity.away_seconds < cfg.activity.idle_seconds:
        raise ConfigError(f"activity.away_seconds ({cfg.activity.away_seconds}) must be at least "
                          f"activity.idle_seconds ({cfg.activity.idle_seconds}): away comes after idle")


MAX_TICK_SECONDS = 60.0     # a gap of 120 s between ticks is taken for sleep (timekeeper.MAX_TICK_GAP_SECONDS)
MIN_RETENTION_DAYS = 8      # weekly budgets add up the week so far; shared budgets send 7 days of history
MIN_SHARED_HISTORY_DAYS = 7


def _effective_values(cfg: "Config") -> None:
    """Settings that are in range but would quietly break something else are brought to
    the nearest value that works, with a warning - an existing configuration keeps loading."""
    if cfg.time_limits.tick_seconds > MAX_TICK_SECONDS:
        log.warning("time_limits.tick_seconds: %s is too long - every tick would look like the PC had slept and "
                    "most screen time would not be counted; using %s", cfg.time_limits.tick_seconds, MAX_TICK_SECONDS)
        cfg.time_limits.tick_seconds = MAX_TICK_SECONDS
    if cfg.storage.retention_days < MIN_RETENTION_DAYS:
        log.warning("storage.retention_days: %s is less than the weekly budgets need (they add up the week so "
                    "far); keeping %s days", cfg.storage.retention_days, MIN_RETENTION_DAYS)
        cfg.storage.retention_days = MIN_RETENTION_DAYS
    if cfg.time_limits.shared_budget and cfg.activity.history_days < MIN_SHARED_HISTORY_DAYS:
        log.warning("activity.history_days: %s is less than a shared weekly budget needs (the other PCs learn "
                    "this week's usage from the history); using %s", cfg.activity.history_days, MIN_SHARED_HISTORY_DAYS)
        cfg.activity.history_days = MIN_SHARED_HISTORY_DAYS


def _warn_unmatchable_sites(cfg: "Config") -> None:
    """A site that is more specific than what is known about the page can never match.

    With ``group_sites_by: registrable`` the agent reports ``google.com`` for every Google
    page.  A rule for ``docs.google.com`` then matches through the page's address, which
    only ``url_method: uia`` reads; a *category* for such a host is looked up the same way."""
    if cfg.activity.group_sites_by != "registrable" or cfg.activity.url_method == "uia":
        return
    named: dict[str, str] = {site: "activity.site_categories" for site in cfg.activity.site_categories}
    for policy in cfg.time_limits.users:
        for rule in policy.rules:
            for site in rule.sites:
                named.setdefault(site, f"rule {rule.name!r}")
        for site in policy.focus.allow_sites:
            named.setdefault(site, f"user {policy.name!r}: focus.allow_sites")
    for rule in cfg.time_limits.rules:
        for site in rule.sites:
            named.setdefault(site, f"rule {rule.name!r}")
    for site, where in sorted(named.items()):
        if registrable_domain(site) != site:
            log.warning("%s: %r can never match - with activity.group_sites_by: registrable and url_method: %s "
                        "only %r is known about a page. Use %r, or url_method: uia",
                        where, site, cfg.activity.url_method, registrable_domain(site), registrable_domain(site))


def _warn_duplicates(where: str, slugs: list[str]) -> None:
    seen: set[str] = set()
    for slug in slugs:
        if slug in seen:
            log.warning("%s: two entries share the id %r; their Home Assistant entities and commands will collide",
                        where, slug)
        seen.add(slug)


def _coerce(owner: str, name: str, default: Any, value: Any) -> Any:
    """Bring a YAML value to the type of the field's default, or say exactly what is wrong."""
    if value is None or default is None:
        return default if value is None else value
    where = f"{owner}.{name}"
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
            return value.strip().lower() in _TRUE
        raise ConfigError(f"{where} must be true or false (got {value!r})")
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(f"{where} must be a number (got {value!r})")
        try:
            number = float(value)
        except ValueError:
            raise ConfigError(f"{where} must be a number (got {value!r})") from None
        if not math.isfinite(number):
            raise ConfigError(f"{where} must be a finite number (got {value!r})")
        return int(number) if isinstance(default, int) else number
    if isinstance(default, str):
        if isinstance(value, (dict, list)):
            raise ConfigError(f"{where} must be text (got {value!r})")
        return value if isinstance(value, str) else str(value)
    if isinstance(default, list):
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return [value]
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where} must be a list (got {value!r})")
        return list(value)
    if isinstance(default, dict) and not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping (got {value!r})")
    return value


def _fill(cls, raw: Any, required: tuple[str, ...] = (), lenient: tuple[str, ...] = ()):
    """Instantiate a dataclass from a mapping, ignoring unknown keys with a warning.

    Values are checked against the type of the field's default (``lenient`` names
    fields that are parsed by the caller instead)."""
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError(f"{cls.__name__}: expected a mapping, got {raw!r}")
    raw = dict(raw or {})
    for key in required:
        if key not in raw:
            raise ConfigError(f"{cls.__name__}: missing required key {key!r}")
    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    known = set(fields)
    unknown = set(raw) - known
    if unknown:
        log.warning("%s: ignoring unknown keys %s", cls.__name__, sorted(unknown, key=str))
    kwargs = {}
    for key, value in raw.items():
        if key not in known:
            continue
        f = fields[key]
        if key in lenient or (f.default is MISSING and f.default_factory is MISSING):
            kwargs[key] = value
            continue
        default = f.default if f.default is not MISSING else f.default_factory()
        kwargs[key] = _coerce(cls.__name__, key, default, value)
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{cls.__name__}: {exc}") from None
