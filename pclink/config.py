"""Configuration schema and loader (YAML -> dataclasses with validation)."""
from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

import yaml

from .util import parse_hhmm, parse_windows, slugify

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


def _parse_minutes(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "unlimited", "none", "off", "inf", "infinite"):
            return None
        if s.endswith("h"):
            return int(float(s[:-1]) * 60)
        if s.endswith("m") or s.endswith("min"):
            s = s.rstrip("min").rstrip("m") if s.endswith("min") else s[:-1]
        raw = s
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise ConfigError(f"invalid minutes value {raw!r}") from None
    if value < 0:
        return None
    return value


def _program_data() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "PCLink")
    return os.path.join(os.path.expanduser("~"), ".pclink")


def default_config_path() -> str:
    env = os.environ.get("PCLINK_CONFIG")
    if env:
        return env
    return os.path.join(_program_data(), "config.yaml")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

@dataclass
class MqttConfig:
    host: str = "homeassistant.local"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    tls: bool = False
    tls_ca: Optional[str] = None
    tls_insecure: bool = False
    client_id: Optional[str] = None
    discovery_prefix: str = "homeassistant"
    base_topic: str = "pclink"
    keepalive: int = 60
    qos: int = 0


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
    allowed_windows: dict[int, list] = field(default_factory=lambda: {i: [] for i in range(7)})
    blocked: bool = False
    warn_at_minutes: list[int] = field(default_factory=lambda: [5, 1])
    action: str = "close"  # close | warn
    message: Optional[str] = None
    icon: str = "mdi:timer-lock-outline"

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def has_limit(self) -> bool:
        return any(v is not None for v in self.daily_limits.values())

    @property
    def has_hours(self) -> bool:
        return any(self.allowed_windows.values())


@dataclass
class FocusConfig:
    """Focus / homework mode: only allow-listed apps and sites for a while."""

    allow_apps: list[str] = field(default_factory=list)
    allow_sites: list[str] = field(default_factory=list)
    allow_categories: list[str] = field(default_factory=lambda: ["education"])
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
class TimeLimitsConfig:
    enabled: bool = True
    tick_seconds: float = 5.0
    block_method: str = "logon_hours"  # logon_hours | disable_account | logoff_only
    manage_unknown_users: bool = False
    unblock_on_stop: bool = True
    reassert_block_seconds: float = 300.0
    week_start: str = "mon"
    shared_budget: bool = False        # count this user's time on *other* PCs too (needs fleet.enabled)
    # parent PIN (required before HA can grant time / loosen limits) ----------
    parent_pin: Optional[str] = None
    parent_pin_sha256: Optional[str] = None
    pin_timeout_seconds: int = 300
    pin_max_attempts: int = 5
    pin_lockout_seconds: int = 300
    pin_required_for: list[str] = field(default_factory=lambda: ["grant", "unblock", "disable", "focus_off", "bedtime", "screenshot"])
    # shared defaults applied to every managed user -------------------------
    rules: list[AppRule] = field(default_factory=list)
    rewards: list[RewardConfig] = field(default_factory=list)
    breaks: dict[str, Any] = field(default_factory=dict)
    default_policy: dict[str, Any] = field(default_factory=dict)
    users: list[UserPolicy] = field(default_factory=list)

    @property
    def pin_enabled(self) -> bool:
        return bool(self.parent_pin or self.parent_pin_sha256)

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
    r"\Rockstar Games" + "\\", r"\Program Files\Steam\steamapps" + "\\", r"\WindowsApps\Microsoft.",
    r"\curseforge\minecraft" + "\\", r"\.minecraft" + "\\", r"\Roblox\Versions" + "\\",
    r"\Paradox Interactive" + "\\", r"\Bethesda.net Launcher\games" + "\\",
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
    media_poll_seconds: float = 6.0


@dataclass
class WindowsUpdateConfig:
    enabled: bool = False
    check_interval_hours: float = 6.0
    allow_install: bool = False   # expose the "Install updates" button


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
    retain: bool = False                             # keep the last image on the broker
    save_to_disk: bool = False
    keep: int = 20


@dataclass
class ApiConfig:
    """Optional local REST / WebSocket API and setup page (see docs/API.md)."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 47392
    token: Optional[str] = None       # required for writes; required for everything when not on loopback
    allow_config_edit: bool = True    # the setup page may write config.yaml (needs a token)
    push_interval_seconds: float = 5.0


@dataclass
class FleetConfig:
    """Multi-PC roll-up: every PC publishing under the same base topic is a peer."""

    enabled: bool = True
    publish_rollup: bool = True
    device_name: str = "PC Link (all PCs)"
    peer_timeout_seconds: float = 180.0


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
    mqtt: MqttConfig = field(default_factory=MqttConfig)
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
    api: ApiConfig = field(default_factory=ApiConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
    commands: list[CustomCommand] = field(default_factory=list)
    apps: list[AppLauncher] = field(default_factory=list)
    source_path: Optional[str] = None

    # Settings that cannot be changed by a hot reload (need a service restart).
    RESTART_SECTIONS = ("mqtt", "storage", "logging", "agent.ipc_host", "agent.ipc_port", "api.host", "api.port", "api.enabled")

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
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        cfg = cls.from_dict(raw)
        cfg.source_path = path
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        raw = dict(raw or {})
        cfg = cls()
        cfg.mqtt = _fill(MqttConfig, raw.get("mqtt"))
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
        cfg.api = _fill(ApiConfig, raw.get("api"))
        cfg.fleet = _fill(FleetConfig, raw.get("fleet"))

        tl_raw = dict(raw.get("time_limits") or {})
        users_raw = tl_raw.pop("users", []) or []
        default_policy = dict(tl_raw.pop("default_policy", {}) or {})
        shared_rules_raw = tl_raw.pop("rules", []) or []
        shared_rewards_raw = tl_raw.pop("rewards", []) or []
        shared_breaks_raw = tl_raw.pop("breaks", None)
        cfg.time_limits = _fill(TimeLimitsConfig, tl_raw)
        cfg.time_limits.default_policy = default_policy
        cfg.time_limits.rules = parse_rules(shared_rules_raw, "time_limits.rules")
        cfg.time_limits.rewards = parse_rewards(shared_rewards_raw, "time_limits.rewards")
        cfg.time_limits.breaks = _breaks_dict(shared_breaks_raw)
        if cfg.time_limits.parent_pin is not None:
            cfg.time_limits.parent_pin = str(cfg.time_limits.parent_pin)
        cfg.time_limits.pin_required_for = [str(x).lower() for x in (cfg.time_limits.pin_required_for or [])]
        try:
            _day_index(cfg.time_limits.week_start)
        except ConfigError:
            raise ConfigError("time_limits.week_start must be a day name (mon..sun)") from None
        # shared rules/rewards/breaks become defaults for every user block
        inherited = dict(default_policy)
        if isinstance(users_raw, dict):  # allow {name: {...}} mapping form
            users_raw = [dict(v or {}, name=k) for k, v in users_raw.items()]
        seen: set[str] = set()
        for item in users_raw:
            if isinstance(item, str):
                item = {"name": item}
            policy = parse_user_policy(item, inherited, shared=cfg.time_limits)
            if policy.key in seen:
                raise ConfigError(f"duplicate user {policy.name!r} in time_limits.users")
            seen.add(policy.key)
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

        if cfg.screenshots.format not in ("jpeg", "png"):
            raise ConfigError("screenshots.format must be jpeg or png")
        cfg.screenshots.users = [str(u).lower() for u in (cfg.screenshots.users or [])]
        if cfg.api.token is not None:
            cfg.api.token = str(cfg.api.token) or None
        if cfg.api.enabled and cfg.api.host not in ("127.0.0.1", "localhost", "::1") and not cfg.api.token:
            raise ConfigError("api.token is required when api.host is not loopback")

        # Derived defaults -------------------------------------------------
        hostname = socket.gethostname()
        if not cfg.device.name:
            cfg.device.name = hostname
        if not cfg.device.id:
            cfg.device.id = slugify(cfg.device.name)
        if not cfg.mqtt.client_id:
            cfg.mqtt.client_id = f"pclink-{cfg.device.id}"
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
            str(k).lower().lstrip("*.").strip(): str(v).lower() for k, v in (cfg.activity.site_categories or {}).items()
        }
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


def _parse_hhmm_default(raw: Any) -> int:
    v = _parse_hhmm_opt(raw)
    return 0 if v is None else v


def parse_rule(item: Any, where: str) -> AppRule:
    if isinstance(item, str):
        item = {"name": item, "apps": [item] if item.lower().endswith(".exe") else [], "sites": [] if item.lower().endswith(".exe") else [item], "blocked": True}
    if not isinstance(item, dict) or not item.get("name"):
        raise ConfigError(f"{where}: rule entries need a 'name': {item!r}")
    rule = AppRule(name=str(item["name"]).strip())
    rule.apps = _parse_names(item.get("apps", item.get("app")))
    rule.sites = _parse_names(item.get("sites", item.get("site", item.get("domains"))))
    rule.categories = _parse_names(item.get("categories", item.get("category")))
    if not (rule.apps or rule.sites or rule.categories):
        raise ConfigError(f"{where}: rule {rule.name!r} needs apps, sites or categories")
    rule.daily_limits = by_day(item.get("daily_limit_minutes", item.get("daily_limit")), _parse_minutes, None)
    try:
        rule.allowed_windows = by_day(item.get("allowed_hours"), parse_windows, [])
    except ValueError as exc:
        raise ConfigError(f"{where}: rule {rule.name!r}: {exc}") from None
    rule.blocked = bool(item.get("blocked", item.get("block", False)))
    rule.warn_at_minutes = _parse_warn(item.get("warn_at_minutes"), [5, 1])
    rule.action = str(item.get("action", "close")).lower()
    if rule.action not in ("close", "warn"):
        raise ConfigError(f"{where}: rule {rule.name!r}: action must be close or warn")
    rule.message = item.get("message")
    rule.icon = str(item.get("icon", rule.icon))
    if not (rule.blocked or rule.has_limit or rule.has_hours):
        raise ConfigError(f"{where}: rule {rule.name!r} needs daily_limit_minutes, allowed_hours or blocked: true")
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
    return rules


def parse_rewards(raw: Any, where: str) -> list[RewardConfig]:
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [dict(v if isinstance(v, dict) else {"minutes": v}, name=k) for k, v in raw.items()]
    out: list[RewardConfig] = []
    for item in raw:
        if isinstance(item, str):
            item = {"name": item}
        reward = _fill(RewardConfig, item, required=("name",))
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
    focus.allow_apps = _parse_names(focus.allow_apps)
    focus.allow_sites = _parse_names(focus.allow_sites)
    focus.allow_categories = _parse_names(focus.allow_categories)
    if focus.action not in ("close", "warn"):
        raise ConfigError(f"user {name!r}: focus.action must be close or warn")
    try:
        focus.schedule = by_day(schedule_raw, parse_windows, [])
    except ValueError as exc:
        raise ConfigError(f"user {name!r}: focus.schedule: {exc}") from None
    focus.default_minutes = max(1, int(focus.default_minutes))
    return focus


def parse_user_policy(item: dict[str, Any], defaults: dict[str, Any], shared: Optional[TimeLimitsConfig] = None) -> UserPolicy:
    if not isinstance(item, dict) or not item.get("name"):
        raise ConfigError(f"time_limits.users entries need a 'name': {item!r}")
    merged = dict(defaults)
    merged.update(item)
    name = str(merged["name"]).strip()
    policy = UserPolicy(name=name)
    policy.display_name = merged.get("display_name")
    policy.unlimited = bool(merged.get("unlimited", False))
    limit_raw = merged.get("daily_limit_minutes", merged.get("daily_limit"))
    policy.daily_limits = by_day(limit_raw, _parse_minutes, None)
    hours_raw = merged.get("allowed_hours", merged.get("allowed_windows"))
    try:
        policy.allowed_windows = by_day(hours_raw, parse_windows, [])
    except ValueError as exc:
        raise ConfigError(f"user {name!r}: {exc}") from None
    policy.warn_at_minutes = _parse_warn(merged.get("warn_at_minutes"), [15, 5, 1])
    policy.action = str(merged.get("action", "logoff")).lower()
    if policy.action not in ("logoff", "lock", "shutdown"):
        raise ConfigError(f"user {name!r}: action must be logoff, lock or shutdown")
    policy.grace_seconds = int(merged.get("grace_seconds", 60))
    policy.count_idle = bool(merged.get("count_idle", True))
    idle_minutes = merged.get("idle_threshold_minutes")
    if idle_minutes is not None:
        policy.idle_threshold_seconds = int(float(idle_minutes) * 60)
    else:
        policy.idle_threshold_seconds = int(merged.get("idle_threshold_seconds", 600))
    if merged.get("count_away") is not None:
        policy.count_away = bool(merged.get("count_away"))
    policy.count_passive = bool(merged.get("count_passive", True))
    policy.count_locked = bool(merged.get("count_locked", False))
    policy.block_login = bool(merged.get("block_login", True))
    # weekly budget / rollover -----------------------------------------------
    policy.weekly_limit_minutes = _parse_minutes(merged.get("weekly_limit_minutes", merged.get("weekly_limit")))
    rollover = merged.get("rollover_max_minutes", merged.get("rollover"))
    if isinstance(rollover, bool):
        rollover = 1440 if rollover else 0
    policy.rollover_max_minutes = int(_parse_minutes(rollover) or 0) if rollover is not None else 0
    # bedtime ---------------------------------------------------------------
    try:
        policy.bedtime = by_day(merged.get("bedtime"), _parse_hhmm_opt, None)
        policy.wake_time = by_day(merged.get("wake_time", merged.get("wake")), _parse_hhmm_default, 0)
    except ValueError as exc:
        raise ConfigError(f"user {name!r}: bedtime: {exc}") from None
    policy.bedtime_parent_extension_minutes = int(_parse_minutes(merged.get("bedtime_parent_extension_minutes", 0)) or 0)
    # rules / focus / breaks / rewards ------------------------------------------
    own_rules = parse_rules(merged.get("rules"), f"user {name!r}: rules")
    shared_rules = list(shared.rules) if shared else []
    own_slugs = {r.slug for r in own_rules}
    policy.rules = [r for r in shared_rules if r.slug not in own_slugs] + own_rules
    policy.focus = parse_focus(merged.get("focus"), name)
    policy.breaks = parse_breaks(merged.get("breaks"), shared.breaks if shared else None)
    own_rewards = parse_rewards(merged.get("rewards"), f"user {name!r}: rewards")
    shared_rewards = list(shared.rewards) if shared else []
    own_reward_slugs = {r.slug for r in own_rewards}
    policy.rewards = [r for r in shared_rewards if r.slug not in own_reward_slugs] + own_rewards
    return policy


def _fill(cls, raw: Any, required: tuple[str, ...] = ()):
    """Instantiate a dataclass from a mapping, ignoring unknown keys with a warning."""
    raw = dict(raw or {})
    for key in required:
        if key not in raw:
            raise ConfigError(f"{cls.__name__}: missing required key {key!r}")
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        import logging

        logging.getLogger(__name__).warning("%s: ignoring unknown keys %s", cls.__name__, sorted(unknown))
    kwargs = {k: v for k, v in raw.items() if k in known}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{cls.__name__}: {exc}") from None
