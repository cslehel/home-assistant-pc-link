"""Home Assistant entity definitions.

Every entity is described once, here, as a **catalog entry**
(``Entity.catalog_entry()``) that the local API serves at
``GET /api/entities`` and that the Home Assistant integration
(``custom_components/pclink``) turns into entities.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from . import __version__
from .config import AppRule, Config, UserPolicy
from .models import HwReading
from .util import slugify

USER_STATUS_OPTIONS = ["logged_off", "active", "idle", "away", "locked", "disconnected"]
PRESENCE_OPTIONS = ["active", "passive", "idle", "away", "none"]
ACTIVITY_OPTIONS = ["none", "locked", "idle", "gaming", "browsing", "app"]
RULE_STATE_OPTIONS = ["ok", "used_up", "blocked", "outside_hours", "off"]

# Device triggers ("Kevin ran out of time") - type -> human label shown in the HA automation editor
USER_TRIGGERS = {
    "time_up": "ran out of time",
    "warning": "is about to run out of time",
    "grace_started": "entered the grace period",
    "logged_in": "logged in",
    "logged_off": "logged off",
    "login_blocked": "got login blocked",
    "login_unblocked": "got login unblocked",
    "bedtime": "reached bedtime",
    "focus_started": "started focus mode",
    "focus_ended": "ended focus mode",
    "break_due": "is due for a break",
    "app_limit_reached": "hit an app or site limit",
    "app_closed": "had an app or tab closed",
    "reward_claimed": "earned bonus minutes",
    "screenshot_taken": "had a screenshot taken",
    "presence_away": "left the seat",
    "presence_back": "came back to the seat",
    "app_close_failed": "had an app that could not be closed",
    "tamper": "may have tampered with PC Link",
    "time_requested": "asked for more time",
    "time_request_approved": "got a time request approved",
    "time_request_denied": "got a time request denied",
    "time_request_expired": "had a time request expire",
    "profile_changed": "was switched to another profile",
    "weekly_summary": "has a new weekly summary",
}
PC_TRIGGERS = {
    "started": "service started",
    "config_reloaded": "configuration reloaded",
    "parent_unlocked": "parent PIN entered",
    "pin_wrong": "wrong parent PIN entered",
    "pin_locked_out": "parent PIN locked out",
    "reboot_pending": "needs a reboot for updates",
    "updates_available": "has Windows updates pending",
    "usb_changed": "USB devices changed",
    "printer_error": "printer reported an error",
    "degraded": "service is not working properly",
    "config_invalid": "started from the last working configuration",
}

# why a user is flagged as possibly tampering (the ``tamper_reason`` of the user payload)
TAMPER_REASONS = ["agent_silent", "agent_killed", "report_rejected", "clock_changed", "time_zone_changed"]

# Attributes that change on (nearly) every push, by the payload mapping a sensor shows as its
# attributes (``attributes_key``).  The catalog lists them as ``unrecorded_attributes`` so
# Home Assistant can keep them out of its recorder; the state and the other attributes are
# recorded as usual.  (A list payload is shown as one attribute named like its key.)
UNRECORDED_ATTRIBUTES: dict[str, list[str]] = {
    "top_processes": ["by_cpu"],
    "top_memory_processes": ["by_memory"],
    "sessions": ["sessions"],
    "service_info": ["ticks", "events", "agents_connected"],
    "health": ["last_good_tick", "failing_steps", "dropped_messages"],
    "usage_recent": ["from", "to", "total_minutes", "website_minutes", "active_minutes", "passive_minutes",
                     "idle_minutes", "away_minutes", "locked_minutes", "apps", "games", "sites"],
    "presence_details": ["reason", "idle_seconds", "input_idle_seconds", "controller", "audio",
                         "media_playing", "display", "display_required", "fullscreen", "screensaver",
                         "windows_present"],
    "activity_details": ["app", "exe", "title", "domain", "url"],
    "media": ["title", "artist", "app", "status", "source"],
    "history": ["history"],
    "break_details": ["continuous_minutes", "next_break_in_minutes"],
    "limit_details": ["used_minutes", "window_remaining_minutes"],
    "weekly_details": ["used_minutes", "remaining_minutes", "other_pcs_today_minutes"],
    "bedtime_details": ["remaining_minutes"],
    "focus_details": ["until"],
}
# a rule's sensor shows the rule's status (rules.<slug>)
_RULE_UNRECORDED = ["active_now", "used_minutes", "remaining_minutes", "weekly_used_minutes",
                    "weekly_remaining_minutes"]


def unrecorded_attributes(attributes_key: Optional[str]) -> Optional[list[str]]:
    """The ``unrecorded_attributes`` of a sensor that shows ``attributes_key`` (None: record them all)."""
    if not attributes_key:
        return None
    if attributes_key.startswith("rules."):
        return list(_RULE_UNRECORDED)
    names = UNRECORDED_ATTRIBUTES.get(attributes_key)
    return list(names) if names else None


class SlugRegistry:
    """Gives every user one id for entity ids and commands, and never the same id twice.

    The orchestrator and the :class:`EntityFactory` share one registry, so the
    entities of a user and the commands that come back always belong to the
    same user - also when two
    account names boil down to the same slug (``kevin.b`` / ``kevin-b``).  The
    first user keeps the plain slug; a later one gets a suffix made from its
    name, which is the same after every restart."""

    def __init__(self) -> None:
        self._by_user: dict[str, str] = {}
        self._by_slug: dict[str, str] = {}

    def assign(self, user: str, preferred: Optional[str] = None) -> str:
        user = str(user).lower()
        slug = self._by_user.get(user)
        if slug is not None:
            return slug
        slug = preferred or slugify(user)
        if slug in self._by_slug:
            base, digest = slug, hashlib.sha1(user.encode("utf-8")).hexdigest()
            slug, n = f"{base}_{digest[:4]}", 4
            while slug in self._by_slug and n < len(digest):
                n += 1
                slug = f"{base}_{digest[:n]}"
        self._by_user[user] = slug
        self._by_slug[slug] = user
        return slug

    def slug(self, user: str) -> str:
        return self.assign(user)

    def user(self, slug: str) -> Optional[str]:
        return self._by_slug.get(slug)

    def slugs(self) -> list[str]:
        return list(self._by_slug)

_HW_UNITS = {
    "temperature": "°C", "load": "%", "fan": "RPM", "power": "W", "clock": "MHz", "voltage": "V",
    "current": "A", "data": "GB", "smalldata": "MB", "throughput": "B/s", "control": "%", "level": "%",
    "factor": "", "frequency": "Hz", "energy": "mWh", "noise": "dBA", "humidity": "%", "timespan": "s",
    "flow": "L/h", "conductivity": "µS/cm",
}
_HW_DEVICE_CLASS = {
    "temperature": "temperature", "power": "power", "voltage": "voltage", "current": "current",
    "frequency": "frequency", "clock": "frequency", "humidity": "humidity", "throughput": "data_rate",
}
_HW_ICONS = {"fan": "mdi:fan", "load": "mdi:gauge", "clock": "mdi:sine-wave", "data": "mdi:database"}


@dataclass
class Entity:
    component: str           # the Home Assistant platform (sensor, switch, ...)
    object_id: str
    unique_id: str
    # Transport-neutral description used by the local API / Home Assistant integration.
    spec: dict[str, Any] = field(default_factory=dict)

    def catalog_entry(self) -> dict[str, Any]:
        """Entry for ``GET /api/entities``."""
        entry = {"unique_id": self.unique_id, "key": self.object_id, **self.spec}
        return {k: v for k, v in entry.items() if v is not None}


def catalog_hash(entries: list[dict[str, Any]]) -> str:
    """One value for a whole catalog: it changes when an entry is added, removed or altered.

    The PC state carries it as ``catalog_hash``, so the Home Assistant
    integration knows when to fetch ``GET /api/entities`` again."""
    parts = sorted((str(e.get("key")), hashlib.sha1(json.dumps(e, sort_keys=True, default=str).encode("utf-8")).hexdigest())
                   for e in entries)
    return hashlib.sha1(json.dumps(parts).encode("utf-8")).hexdigest()


def _tag(entities: list[Entity], **fields: Any) -> list[Entity]:
    """Stamp ``fields`` (source / user) onto every entity."""
    for ent in entities:
        ent.spec.update(fields)
    return entities


@dataclass
class EntityFactory:
    cfg: Config
    slugs: SlugRegistry = field(default_factory=SlugRegistry)   # shared with the orchestrator

    def __post_init__(self) -> None:
        for policy in self.cfg.time_limits.users:   # configured users come first: they keep the plain slug
            self.slugs.assign(policy.key, policy.slug)

    # -- shared bits --------------------------------------------------------
    @property
    def device_id(self) -> str:
        return self.cfg.device.id

    def device_block(self) -> dict[str, Any]:
        dev = self.cfg.device
        block: dict[str, Any] = {
            "identifiers": [f"pclink_{dev.id}"],
            "name": dev.name,
            "manufacturer": dev.manufacturer,
            "model": dev.model,
            "sw_version": __version__,
        }
        if dev.suggested_area:
            block["suggested_area"] = dev.suggested_area
        return block

    def _entity(self, component: str, object_id: str, spec: dict[str, Any]) -> Entity:
        return Entity(component, object_id, f"pclink_{self.device_id}_{object_id}", spec)

    # -- generic builders -----------------------------------------------------
    def sensor(self, object_id: str, name: str, key: str, *,
               unit: Optional[str] = None, device_class: Optional[str] = None,
               state_class: Optional[str] = None, icon: Optional[str] = None,
               attributes_key: Optional[str] = None, diagnostic: bool = False,
               enabled: bool = True, options: Optional[list[str]] = None,
               precision: Optional[int] = None) -> Entity:
        spec = {"platform": "sensor", "name": name, "value": key, "attributes": attributes_key, "unit": unit,
                "device_class": device_class, "state_class": state_class, "icon": icon,
                "category": "diagnostic" if diagnostic else None, "enabled": enabled, "options": options,
                "precision": precision, "unrecorded_attributes": unrecorded_attributes(attributes_key)}
        return self._entity("sensor", object_id, spec)

    def binary_sensor(self, object_id: str, name: str, key: str, *,
                      device_class: Optional[str] = None, icon: Optional[str] = None,
                      diagnostic: bool = False) -> Entity:
        spec = {"platform": "binary_sensor", "name": name, "value": key, "device_class": device_class, "icon": icon,
                "category": "diagnostic" if diagnostic else None}
        return self._entity("binary_sensor", object_id, spec)

    def switch(self, object_id: str, name: str, key: str, *, icon: Optional[str] = None, config: bool = True) -> Entity:
        spec = {"platform": "switch", "name": name, "value": key, "icon": icon,
                "category": "config" if config else None, "command": object_id}
        return self._entity("switch", object_id, spec)

    def number(self, object_id: str, name: str, key: str, *,
               minimum: float = 0, maximum: float = 1440, step: float = 5, unit: str = "min",
               icon: Optional[str] = None, mode: str = "box", config: bool = True) -> Entity:
        spec = {"platform": "number", "name": name, "value": key, "min": minimum, "max": maximum, "step": step,
                "unit": unit, "mode": mode, "icon": icon, "category": "config" if config else None,
                "command": object_id}
        return self._entity("number", object_id, spec)

    def button(self, object_id: str, name: str, *, icon: Optional[str] = None,
               device_class: Optional[str] = None, config: bool = False) -> Entity:
        spec = {"platform": "button", "name": name, "icon": icon, "device_class": device_class,
                "category": "config" if config else None, "command": object_id}
        return self._entity("button", object_id, spec)

    def text(self, object_id: str, name: str, *, icon: Optional[str] = None, password: bool = False,
             key: Optional[str] = None, config: bool = False) -> Entity:
        spec = {"platform": "text", "name": name, "value": key, "icon": icon, "password": password or None,
                "category": "config" if config else None, "command": object_id}
        return self._entity("text", object_id, spec)

    def update(self, object_id: str, name: str, *, installed_key: str, latest_key: str, in_progress_key: str,
               url_key: str, summary_key: str, install_command: Optional[str], title: str,
               attributes_key: Optional[str] = None, notes_key: Optional[str] = None,
               percentage_key: Optional[str] = None) -> Entity:
        """A Home Assistant *update* entity (installed vs latest version, optional Install button).

        ``attributes_key`` names a mapping in the payload shown as the entity's attributes
        (the state of the update: why the last one failed or was refused, the last check)."""
        spec = {"platform": "update", "name": name, "value": installed_key, "latest": latest_key,
                "attributes": attributes_key,
                "in_progress": in_progress_key, "release_url": url_key, "summary": summary_key, "title": title,
                "release_notes": notes_key, "percentage": percentage_key,
                "device_class": "firmware", "category": "config", "command": install_command}
        return self._entity("update", object_id, spec)

    def select(self, object_id: str, name: str, key: str, options: list[str], *,
               icon: Optional[str] = None, config: bool = True) -> Entity:
        """A choice among ``options``; the chosen one is sent as the command's value."""
        spec = {"platform": "select", "name": name, "value": key, "options": list(options), "icon": icon,
                "category": "config" if config else None, "command": object_id}
        return self._entity("select", object_id, spec)

    def camera(self, object_id: str, name: str, *, icon: Optional[str] = None) -> Entity:
        spec = {"platform": "camera", "name": name, "value": "screenshot", "icon": icon}
        return self._entity("camera", object_id, spec)

    # -- PC level -------------------------------------------------------------
    def pc_entities(self, snapshot: dict[str, Any]) -> list[Entity]:
        e: list[Entity] = []
        s = self.sensor
        e.append(s("cpu_percent", "CPU usage", "cpu_percent", unit="%", state_class="measurement", icon="mdi:cpu-64-bit", precision=0))
        e.append(s("cpu_frequency", "CPU frequency", "cpu_freq_mhz", unit="MHz", device_class="frequency", state_class="measurement", enabled=False, precision=0))
        e.append(s("memory_percent", "Memory usage", "memory_percent", unit="%", state_class="measurement", icon="mdi:memory", precision=0))
        e.append(s("memory_used", "Memory used", "memory_used_gb", unit="GB", device_class="data_size", state_class="measurement", precision=1))
        e.append(s("swap_percent", "Swap usage", "swap_percent", unit="%", state_class="measurement", icon="mdi:memory", enabled=False, precision=0))
        for letter in snapshot.get("disks", []):
            key = slugify(letter)
            e.append(s(f"disk_{key}_percent", f"Disk {letter.upper()}: usage", f"disk_{key}_percent", unit="%", state_class="measurement", icon="mdi:harddisk", precision=0))
            e.append(s(f"disk_{key}_free", f"Disk {letter.upper()}: free", f"disk_{key}_free_gb", unit="GB", device_class="data_size", state_class="measurement", icon="mdi:harddisk", precision=1))
        if self.cfg.telemetry.disk_io:
            e.append(s("disk_read_rate", "Disk read", "disk_read_mbps", unit="MB/s", state_class="measurement", icon="mdi:harddisk", enabled=False, precision=1))
            e.append(s("disk_write_rate", "Disk write", "disk_write_mbps", unit="MB/s", state_class="measurement", icon="mdi:harddisk", enabled=False, precision=1))
        if self.cfg.telemetry.network:
            e.append(s("network_rx", "Network download", "net_rx_mbps", unit="Mbit/s", device_class="data_rate", state_class="measurement", icon="mdi:download-network", precision=2))
            e.append(s("network_tx", "Network upload", "net_tx_mbps", unit="Mbit/s", device_class="data_rate", state_class="measurement", icon="mdi:upload-network", precision=2))
        e.append(s("last_boot", "Last boot", "last_boot", device_class="timestamp", diagnostic=True))
        e.append(s("uptime", "Uptime", "uptime_hours", unit="h", device_class="duration", state_class="measurement", diagnostic=True, enabled=False, precision=1))
        e.append(s("process_count", "Processes", "process_count", icon="mdi:application-cog", state_class="measurement", diagnostic=True, enabled=False))
        if snapshot.get("gpu_name"):
            e.append(s("gpu_load", "GPU usage", "gpu_load", unit="%", state_class="measurement", icon="mdi:expansion-card", precision=0))
            e.append(s("gpu_memory_percent", "GPU memory usage", "gpu_memory_percent", unit="%", state_class="measurement", icon="mdi:expansion-card", precision=0))
            e.append(s("gpu_temperature", "GPU temperature", "gpu_temperature", unit="°C", device_class="temperature", state_class="measurement", precision=0))
            e.append(s("gpu_power", "GPU power", "gpu_power", unit="W", device_class="power", state_class="measurement", enabled=False, precision=0))
            e.append(s("gpu_name", "GPU", "gpu_name", icon="mdi:expansion-card", diagnostic=True, enabled=False))
        if snapshot.get("battery_percent") is not None:
            e.append(s("battery", "Battery", "battery_percent", unit="%", device_class="battery", state_class="measurement"))
            e.append(self.binary_sensor("battery_charging", "Battery charging", "battery_plugged", device_class="battery_charging"))
        e.append(s("active_sessions", "Active sessions", "active_sessions", icon="mdi:account-multiple", state_class="measurement", attributes_key="sessions"))
        e.append(s("logged_in_users", "Logged in users", "logged_in_users", icon="mdi:account-group"))
        e.append(self.binary_sensor("any_user_active", "Someone is using the PC", "any_user_active", device_class="occupancy"))
        e.append(self.binary_sensor("anyone_present", "Someone is at the PC", "anyone_present", device_class="presence", icon="mdi:account-eye"))
        e.append(s("mac_address", "MAC address", "mac", icon="mdi:ethernet", diagnostic=True, attributes_key="interfaces"))
        e.append(s("ip_address", "IP address", "ip", icon="mdi:ip-network", diagnostic=True))
        e.append(s("agent_status", "Service", "service_status", icon="mdi:cog", diagnostic=True, attributes_key="service_info"))
        # health of the service itself: a step of the main loop keeps failing, or a part of it could not start
        e.append(self.binary_sensor("degraded", "Service problem", "degraded", device_class="problem", diagnostic=True))
        e.append(s("tick_failures", "Service errors", "tick_failures", icon="mdi:alert-circle-outline",
                   state_class="total_increasing", diagnostic=True, enabled=False, attributes_key="health"))
        e.append(s("last_good_tick", "Last complete check", "last_good_tick", device_class="timestamp", diagnostic=True, enabled=False))
        if self.cfg.telemetry.top_processes > 0:
            e.append(s("top_process", "Top process (CPU)", "top_process", icon="mdi:chart-timeline-variant", attributes_key="top_processes"))
            e.append(s("top_process_memory", "Top process (memory)", "top_memory_process", icon="mdi:memory", attributes_key="top_memory_processes", enabled=False))
        if self.cfg.telemetry.usb_devices:
            e.append(s("usb_devices", "USB devices", "usb_devices", icon="mdi:usb", state_class="measurement", attributes_key="usb_device_list"))
            e.append(s("removable_drives", "Removable drives", "removable_drives", icon="mdi:usb-flash-drive", state_class="measurement", attributes_key="removable_drive_list", enabled=False))
        if self.cfg.telemetry.printers:
            e.append(s("printer_status", "Printer", "printer_status", icon="mdi:printer", attributes_key="printers"))
            e.append(s("print_jobs", "Print jobs", "print_jobs", icon="mdi:printer-pos", state_class="measurement", enabled=False))
        if self.cfg.windows_update.enabled:
            e.append(s("pending_updates", "Windows updates pending", "pending_updates", icon="mdi:microsoft-windows", state_class="measurement", attributes_key="update_details"))
            e.append(self.binary_sensor("reboot_pending", "Reboot pending", "reboot_pending", device_class="problem", icon="mdi:restart-alert"))
            e.append(s("last_update_check", "Last update check", "last_update_check", device_class="timestamp", diagnostic=True, enabled=False))
            e.append(self.button("check_updates", "Check for updates", icon="mdi:update", config=True))
            if self.cfg.windows_update.allow_install:
                e.append(self.button("install_updates", "Install updates", icon="mdi:download", config=True))
        if self.cfg.updates.enabled:
            can_install = bool(snapshot.get("pclink_update_info", {}).get("can_install"))
            e.append(self.update("pclink_update", "PC Link update", installed_key="pclink_installed_version",
                                 latest_key="pclink_latest_version", in_progress_key="pclink_update_in_progress",
                                 url_key="pclink_update_release_url", summary_key="pclink_update_summary",
                                 install_command="pclink_update_install" if can_install else None, title="PC Link",
                                 attributes_key="pclink_update_info", notes_key="pclink_update_release_notes",
                                 percentage_key="pclink_update_percentage"))
            e.append(self.button("pclink_update_check", "Check for PC Link update", icon="mdi:update", config=True))
        if self.cfg.time_limits.pin_enabled:
            e.append(self.text("parent_pin", "Parent PIN", icon="mdi:form-textbox-password", password=True,
                               key="parent_pin_state", config=True))
            e.append(self.binary_sensor("parent_unlocked", "Parent PIN unlocked", "parent_unlocked", device_class="lock", icon="mdi:shield-key"))
        e.append(self.switch("parent_present", "Parent at home", "parent_present", icon="mdi:home-account"))
        e.append(self.button("reload_config", "Reload configuration", icon="mdi:reload", config=True))
        for app in self.cfg.apps:
            if app.launch:
                e.append(self.button(f"launch_{app.slug}", f"Launch {app.name}", icon=app.icon))
            if app.process_name:
                e.append(self.button(f"close_{app.slug}", f"Close {app.name}", icon="mdi:close-box-outline"))
        # controls ------------------------------------------------------------
        p = self.cfg.power
        if p.allow_shutdown:
            e.append(self.button("shutdown", "Shut down", icon="mdi:power"))
        if p.allow_reboot:
            e.append(self.button("reboot", "Reboot", device_class="restart"))
        if p.allow_sleep:
            e.append(self.button("sleep", "Sleep", icon="mdi:power-sleep"))
        if p.allow_hibernate:
            e.append(self.button("hibernate", "Hibernate", icon="mdi:power-sleep"))
        e.append(self.button("lock_all", "Lock all sessions", icon="mdi:lock"))
        e.append(self.button("cancel_shutdown", "Cancel shutdown", icon="mdi:power-off", config=True))
        e.append(self.switch("time_limits_enabled", "Time limits", "time_limits_enabled", icon="mdi:timer-lock"))
        e.append(self.text("broadcast_message", "Message to everyone", icon="mdi:message-alert"))
        for cmd in self.cfg.commands:
            e.append(self.button(f"cmd_{cmd.slug}", cmd.name, icon=cmd.icon))
        return _tag(e, source="pc")

    # -- per user -------------------------------------------------------------
    def user_entities(self, user: str, policy: Optional[UserPolicy], managed: bool,
                      profiles: Optional[list[str]] = None) -> list[Entity]:
        """``profiles``: the names of the user's profiles (default: those of ``policy``)."""
        slug = self.slugs.slug(user)
        label = policy.label if policy else user
        pre = f"user_{slug}_"
        e: list[Entity] = []

        def s(oid, name, key, **kw):
            return self.sensor(pre + oid, f"{label} {name}", key, **kw)

        e.append(s("status", "status", "status", device_class="enum", options=USER_STATUS_OPTIONS, icon="mdi:account", attributes_key="sessions"))
        e.append(s("recent_usage", f"usage in the last {self.cfg.activity.report_hours} h", "usage_recent.total_minutes", unit="min",
                   device_class="duration", icon="mdi:chart-timeline", attributes_key="usage_recent", precision=0))
        e.append(s("idle_today", "idle time today", "idle_minutes_today", unit="min", device_class="duration",
                   state_class="total_increasing", icon="mdi:sleep", precision=0))
        e.append(s("away_today", "away time today", "away_minutes_today", unit="min", device_class="duration",
                   state_class="total_increasing", icon="mdi:account-off", precision=0))
        e.append(s("presence", "presence", "presence", device_class="enum", options=PRESENCE_OPTIONS, icon="mdi:account-eye", attributes_key="presence_details"))
        e.append(self.binary_sensor(pre + "logged_in", f"{label} logged in", "logged_in", device_class="presence"))
        e.append(s("activity", "activity", "activity", icon="mdi:controller", attributes_key="activity_details"))
        e.append(s("current_app", "current app", "app", icon="mdi:application"))
        e.append(s("current_website", "current website", "domain", icon="mdi:web"))
        if self.cfg.activity.now_playing:
            e.append(s("now_playing", "now playing", "now_playing", icon="mdi:music-note", attributes_key="media"))
        e.append(s("time_used_today", "time used today", "used_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:timer-sand", precision=0, attributes_key="history"))
        e.append(s("gaming_today", "gaming today", "gaming_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:gamepad-variant", precision=0, attributes_key="top_games"))
        e.append(s("browsing_today", "browsing today", "browsing_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:web", precision=0, attributes_key="top_sites"))
        e.append(s("top_apps", "top apps today", "top_app", icon="mdi:apps", attributes_key="top_apps", enabled=False))
        e.append(s("time_used_this_week", "time used this week", "weekly_used_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:calendar-clock", precision=0, attributes_key="weekly_details", enabled=managed))
        if self.cfg.agent.volume_control and self.cfg.agent.enabled:
            e.append(self.number(pre + "volume", f"{label} volume", "volume", minimum=0, maximum=100, step=1, unit="%", icon="mdi:volume-high", mode="slider", config=False))
            e.append(self.switch(pre + "muted", f"{label} muted", "muted", icon="mdi:volume-mute", config=False))
        if self.cfg.agent.display_control and self.cfg.agent.enabled:
            e.append(self.switch(pre + "display", f"{label} display", "display_on", icon="mdi:monitor", config=False))
        if managed and policy is not None:
            e.append(s("time_remaining", "time remaining", "remaining_minutes", unit="min", device_class="duration", state_class="measurement", icon="mdi:timer-outline", precision=0, attributes_key="limit_details"))
            e.append(s("daily_limit", "daily limit", "limit_minutes", unit="min", device_class="duration", icon="mdi:timer-cog", precision=0))
            e.append(s("weekly_remaining", "weekly time remaining", "weekly_remaining_minutes", unit="min", device_class="duration", state_class="measurement", icon="mdi:calendar-clock", precision=0, enabled=policy.weekly_limit_minutes is not None))
            e.append(s("rollover", "rolled-over minutes", "rollover_minutes", unit="min", device_class="duration", icon="mdi:timer-refresh-outline", precision=0, enabled=policy.rollover_max_minutes > 0))
            e.append(s("earned_today", "earned today", "earned_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:star-circle", precision=0, enabled=bool(policy.rewards)))
            # no device class: "lock" would show a blocked login as "Unlocked"
            e.append(self.binary_sensor(pre + "login_blocked", f"{label} login blocked", "login_blocked", icon="mdi:account-lock"))
            e.append(self.binary_sensor(pre + "in_allowed_hours", f"{label} within allowed hours", "in_allowed_hours", icon="mdi:clock-check"))
            e.append(self.number(pre + "limit_today", f"{label} limit today", "limit_today", icon="mdi:timer-edit"))
            e.append(self.number(pre + "weekday_limit", f"{label} weekday limit", "weekday_limit", icon="mdi:calendar-week"))
            e.append(self.number(pre + "weekend_limit", f"{label} weekend limit", "weekend_limit", icon="mdi:calendar-weekend"))
            e.append(self.number(pre + "weekly_limit", f"{label} weekly limit", "weekly_limit", maximum=10080, step=15, icon="mdi:calendar-clock"))
            e.append(self.number(pre + "bonus_today", f"{label} bonus today", "bonus_local_minutes", minimum=-480, maximum=480, icon="mdi:timer-plus", config=False))
            e.append(self.number(pre + "add_minutes", f"{label} add minutes", "add_minutes", minimum=0, maximum=480, icon="mdi:timer-plus-outline", config=False))
            e.append(self.button(pre + "add_15", f"{label} +15 min", icon="mdi:timer-plus"))
            e.append(self.button(pre + "add_30", f"{label} +30 min", icon="mdi:timer-plus"))
            for reward in policy.rewards:
                e.append(self.button(pre + "reward_" + reward.slug, f"{label} reward: {reward.name} (+{reward.minutes} min)", icon=reward.icon))
            e.append(self.switch(pre + "limit_enabled", f"{label} limit enabled", "limit_enabled", icon="mdi:timer-lock"))
            e.append(self.switch(pre + "blocked", f"{label} access paused", "blocked", icon="mdi:account-cancel", config=False))
            # bedtime -----------------------------------------------------------
            if policy.has_bedtime:
                e.append(s("bedtime", "bedtime tonight", "bedtime", icon="mdi:bed-clock", attributes_key="bedtime_details"))
                e.append(self.binary_sensor(pre + "past_bedtime", f"{label} past bedtime", "past_bedtime", icon="mdi:sleep"))
                e.append(self.number(pre + "bedtime_extension", f"{label} bedtime extension", "bedtime_extension_local_minutes", maximum=1440, step=15, icon="mdi:bed-clock", config=False))
                e.append(self.button(pre + "extend_bedtime_30", f"{label} bedtime +30 min", icon="mdi:bed-clock"))
            # focus mode ----------------------------------------------------------------
            e.append(self.switch(pre + "focus_mode", f"{label} focus mode", "focus_active", icon="mdi:book-open-page-variant", config=False))
            e.append(self.number(pre + "focus_minutes", f"{label} focus duration", "focus_minutes", minimum=5, maximum=480, step=5, icon="mdi:timer-cog-outline"))
            e.append(s("focus_remaining", "focus time remaining", "focus_remaining_minutes", unit="min", device_class="duration", icon="mdi:book-clock", precision=0, attributes_key="focus_details"))
            # breaks ------------------------------------------------------------------------
            e.append(self.switch(pre + "breaks_enabled", f"{label} break reminders", "breaks_enabled", icon="mdi:eye-refresh"))
            e.append(s("continuous_use", "continuous screen time", "continuous_minutes", unit="min", device_class="duration", state_class="measurement", icon="mdi:timer-play", precision=0, attributes_key="break_details"))
            # per-app / per-site rules ---------------------------------------------------------
            # (the suffixes are AppRule.ENTITY_SUFFIXES: config.yaml refuses rules whose ids would collide)
            enabled_sfx, remaining_sfx, limit_sfx, weekly_remaining_sfx, weekly_limit_sfx = AppRule.ENTITY_SUFFIXES
            for rule in policy.rules:
                rp = pre + "rule_" + rule.slug
                e.append(self.sensor(rp, f"{label} {rule.name}", f"rules.{rule.slug}.state", device_class="enum",
                                     options=RULE_STATE_OPTIONS, icon=rule.icon, attributes_key=f"rules.{rule.slug}"))
                e.append(self.switch(rp + enabled_sfx, f"{label} {rule.name} rule", f"rules.{rule.slug}.enabled", icon=rule.icon))
                if rule.has_limit:
                    e.append(self.sensor(rp + remaining_sfx, f"{label} {rule.name} remaining", f"rules.{rule.slug}.remaining_minutes",
                                         unit="min", device_class="duration", state_class="measurement", icon=rule.icon, precision=0))
                    e.append(self.number(rp + limit_sfx, f"{label} {rule.name} limit today", f"rules.{rule.slug}.limit_minutes", icon=rule.icon))
                if rule.has_weekly_limit:
                    e.append(self.sensor(rp + weekly_remaining_sfx, f"{label} {rule.name} remaining this week", f"rules.{rule.slug}.weekly_remaining_minutes",
                                         unit="min", device_class="duration", state_class="measurement", icon="mdi:calendar-clock", precision=0))
                    e.append(self.number(rp + weekly_limit_sfx, f"{label} {rule.name} weekly limit", f"rules.{rule.slug}.weekly_limit",
                                         maximum=10080, step=15, icon="mdi:calendar-clock"))
        if self._screenshot_allowed(user, managed):
            e.append(self.button(pre + "screenshot", f"{label} take screenshot", icon="mdi:monitor-screenshot", config=True))
            e.append(self.camera(pre + "screenshot_image", f"{label} screenshot", icon="mdi:monitor-screenshot"))
        e.append(self.button(pre + "logoff", f"{label} log off", icon="mdi:logout"))
        e.append(self.button(pre + "lock", f"{label} lock", icon="mdi:lock"))
        e.append(self.text(pre + "message", f"{label} message", icon="mdi:message-text"))
        if managed:
            e.append(s("blocked_items", "blocked apps and websites", "blocked_count", icon="mdi:cancel",
                       state_class="measurement", attributes_key="blocked_details"))
            e.append(self.text(pre + "block_item", f"{label} block app or website", icon="mdi:shield-plus", config=True))
            e.append(self.text(pre + "unblock_item", f"{label} unblock app or website", icon="mdi:shield-remove", config=True))
            # signs that somebody worked around PC Link (agent killed or silent, forged reports, clock changes)
            e.append(self.binary_sensor(pre + "tamper", f"{label} tampering suspected", "tamper",
                                        device_class="tamper", icon="mdi:shield-alert"))
            e.append(s("tamper_reason", "tampering reason", "tamper_reason", device_class="enum", options=TAMPER_REASONS,
                       icon="mdi:shield-alert-outline", attributes_key="tamper_details", diagnostic=True))
            e.append(self.button(pre + "clear_tamper", f"{label} clear tampering warning", icon="mdi:shield-check", config=True))
            # "ask for more time": what the child asked for from the tray, and the parent's answer
            requests = self.cfg.time_limits.time_requests
            if requests.enabled and requests.options:
                e.append(s("time_request", "time request", "time_request_minutes", unit="min", device_class="duration",
                           icon="mdi:timer-alert-outline", precision=0, attributes_key="time_request"))
                e.append(self.button(pre + "approve_time_request", f"{label} approve time request", icon="mdi:timer-check-outline"))
                e.append(self.button(pre + "deny_time_request", f"{label} deny time request", icon="mdi:timer-cancel-outline"))
            # holiday / school-day profiles (only for a user that has any)
            names = sorted(profiles if profiles is not None else getattr(policy, "profiles", None) or [])
            if names:
                e.append(self.select(pre + "profile", f"{label} profile", "profile", ["normal", *names],
                                     icon="mdi:calendar-star"))
            e.append(s("time_last_week", "time last week", "last_week_minutes", unit="min", device_class="duration",
                       icon="mdi:calendar-week", precision=0, attributes_key="weekly_summary"))
        return _tag(e, source="user", user=user, user_name=label, managed=managed)

    def _screenshot_allowed(self, user: str, managed: bool) -> bool:
        sc = self.cfg.screenshots
        if not sc.enabled:
            return False
        if sc.users:
            return user.lower() in sc.users
        return managed

    # -- hardware sensors -----------------------------------------------------
    def hw_entities(self, readings: list[HwReading]) -> list[Entity]:
        e: list[Entity] = []
        for r in readings:
            unit = r.unit or _HW_UNITS.get(r.sensor_type, "")
            e.append(
                self.sensor(
                    f"hw_{r.key}", f"{r.hardware} {r.name} {r.sensor_type}".strip(), f"hw_{r.key}",
                    unit=unit or None, device_class=_HW_DEVICE_CLASS.get(r.sensor_type),
                    state_class="measurement", icon=_HW_ICONS.get(r.sensor_type),
                    diagnostic=r.sensor_type not in ("temperature", "load"),
                    precision=0 if r.sensor_type in ("fan", "clock", "load") else 1,
                )
            )
        return _tag(e, source="pc")
