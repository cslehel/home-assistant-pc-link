"""Configuration schema and loader (YAML -> dataclasses with validation)."""
from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

import yaml

from .util import parse_windows, slugify

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
    top_processes: int = 0  # 0 disables (costly-ish)


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
    count_locked: bool = False
    block_login: bool = True

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
    def has_restrictions(self) -> bool:
        if self.unlimited:
            return False
        return any(v is not None for v in self.daily_limits.values()) or any(
            self.allowed_windows.values()
        )


@dataclass
class TimeLimitsConfig:
    enabled: bool = True
    tick_seconds: float = 5.0
    block_method: str = "logon_hours"  # logon_hours | disable_account | logoff_only
    manage_unknown_users: bool = False
    unblock_on_stop: bool = True
    reassert_block_seconds: float = 300.0
    default_policy: dict[str, Any] = field(default_factory=dict)
    users: list[UserPolicy] = field(default_factory=list)


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


@dataclass
class ActivityConfig:
    idle_seconds: int = 120
    audio_keeps_active: bool = True
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
    commands: list[CustomCommand] = field(default_factory=list)
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
        return parse_user_policy(raw, {})

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

        tl_raw = dict(raw.get("time_limits") or {})
        users_raw = tl_raw.pop("users", []) or []
        default_policy = dict(tl_raw.pop("default_policy", {}) or {})
        cfg.time_limits = _fill(TimeLimitsConfig, tl_raw)
        cfg.time_limits.default_policy = default_policy
        if isinstance(users_raw, dict):  # allow {name: {...}} mapping form
            users_raw = [dict(v or {}, name=k) for k, v in users_raw.items()]
        seen: set[str] = set()
        for item in users_raw:
            if isinstance(item, str):
                item = {"name": item}
            policy = parse_user_policy(item, default_policy)
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


def parse_user_policy(item: dict[str, Any], defaults: dict[str, Any]) -> UserPolicy:
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
    warn = merged.get("warn_at_minutes", [15, 5, 1])
    if isinstance(warn, (int, float)):
        warn = [warn]
    policy.warn_at_minutes = sorted({int(w) for w in (warn or []) if int(w) > 0}, reverse=True)
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
    policy.count_locked = bool(merged.get("count_locked", False))
    policy.block_login = bool(merged.get("block_login", True))
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
