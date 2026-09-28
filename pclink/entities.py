"""Home Assistant MQTT discovery payload builders."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import __version__
from .config import Config, CustomCommand, UserPolicy
from .models import HwReading
from .util import slugify

SUPPORT_URL = "https://github.com/cslehel/home-assistant-pc-link"

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
}

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
    component: str
    object_id: str
    payload: dict[str, Any]

    @property
    def unique_id(self) -> str:
        return self.payload.get("unique_id", self.object_id)


@dataclass
class EntityFactory:
    cfg: Config
    base_topic: str          # <base>/<device_id>
    availability_topic: str
    state_topic: str
    discovery_prefix: str = "homeassistant"
    extra_device: dict[str, Any] = field(default_factory=dict)

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
        block.update(self.extra_device)
        return block

    def _base(self, object_id: str, name: str, **extra) -> dict[str, Any]:
        payload = {
            "name": name,
            "unique_id": f"pclink_{self.device_id}_{object_id}",
            "has_entity_name": True,
            "availability_topic": self.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self.device_block(),
            "origin": {"name": "home-assistant-pc-link", "sw_version": __version__, "support_url": SUPPORT_URL},
        }
        payload.update({k: v for k, v in extra.items() if v is not None})
        return payload

    def set_topic(self, object_id: str) -> str:
        return f"{self.base_topic}/set/{object_id}"

    def user_state_topic(self, slug: str) -> str:
        return f"{self.base_topic}/user/{slug}/state"

    @staticmethod
    def _path(key: str) -> str:
        """``a.b.c`` -> ``value_json['a']['b']['c']`` (bracket access survives odd keys)."""
        if "." not in key:
            return f"value_json.{key}"
        return "value_json" + "".join(f"['{part}']" for part in key.split("."))

    @classmethod
    def _tpl(cls, key: str) -> str:
        return "{{ %s | default(none) }}" % cls._path(key)

    # -- generic builders -----------------------------------------------------
    def sensor(self, object_id: str, name: str, key: str, *, state_topic: Optional[str] = None,
               unit: Optional[str] = None, device_class: Optional[str] = None,
               state_class: Optional[str] = None, icon: Optional[str] = None,
               attributes_key: Optional[str] = None, diagnostic: bool = False,
               enabled: bool = True, options: Optional[list[str]] = None,
               precision: Optional[int] = None) -> Entity:
        topic = state_topic or self.state_topic
        payload = self._base(
            object_id, name,
            state_topic=topic, value_template=self._tpl(key),
            unit_of_measurement=unit, device_class=device_class, state_class=state_class, icon=icon,
            entity_category="diagnostic" if diagnostic else None,
            enabled_by_default=None if enabled else False,
            options=options, suggested_display_precision=precision,
        )
        if attributes_key:
            payload["json_attributes_topic"] = topic
            payload["json_attributes_template"] = "{{ %s | default({}) | tojson }}" % self._path(attributes_key)
        return Entity("sensor", object_id, payload)

    def binary_sensor(self, object_id: str, name: str, key: str, *, state_topic: Optional[str] = None,
                      device_class: Optional[str] = None, icon: Optional[str] = None,
                      diagnostic: bool = False) -> Entity:
        payload = self._base(
            object_id, name, state_topic=state_topic or self.state_topic,
            value_template="{{ 'ON' if %s else 'OFF' }}" % self._path(key),
            device_class=device_class, icon=icon, entity_category="diagnostic" if diagnostic else None,
        )
        return Entity("binary_sensor", object_id, payload)

    def switch(self, object_id: str, name: str, key: str, *, state_topic: Optional[str] = None,
               icon: Optional[str] = None, config: bool = True) -> Entity:
        payload = self._base(
            object_id, name, state_topic=state_topic or self.state_topic,
            command_topic=self.set_topic(object_id),
            value_template="{{ 'ON' if %s else 'OFF' }}" % self._path(key),
            payload_on="ON", payload_off="OFF", icon=icon,
            entity_category="config" if config else None,
        )
        return Entity("switch", object_id, payload)

    def number(self, object_id: str, name: str, key: str, *, state_topic: Optional[str] = None,
               minimum: float = 0, maximum: float = 1440, step: float = 5, unit: str = "min",
               icon: Optional[str] = None, mode: str = "box", config: bool = True) -> Entity:
        payload = self._base(
            object_id, name, state_topic=state_topic or self.state_topic,
            command_topic=self.set_topic(object_id), value_template=self._tpl(key),
            min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=mode, icon=icon,
            entity_category="config" if config else None,
        )
        return Entity("number", object_id, payload)

    def button(self, object_id: str, name: str, *, icon: Optional[str] = None,
               device_class: Optional[str] = None, config: bool = False) -> Entity:
        payload = self._base(
            object_id, name, command_topic=self.set_topic(object_id), payload_press="PRESS",
            icon=icon, device_class=device_class, entity_category="config" if config else None,
        )
        return Entity("button", object_id, payload)

    def text(self, object_id: str, name: str, *, icon: Optional[str] = None, password: bool = False,
             state_topic: Optional[str] = None, key: Optional[str] = None, config: bool = False) -> Entity:
        payload = self._base(
            object_id, name, command_topic=self.set_topic(object_id),
            state_topic=state_topic or self.set_topic(object_id), icon=icon,
            mode="password" if password else "text", max=255, min=0,
            value_template=self._tpl(key) if key else None,
            entity_category="config" if config else None,
        )
        return Entity("text", object_id, payload)

    def camera(self, object_id: str, name: str, topic: str, *, icon: Optional[str] = None) -> Entity:
        payload = self._base(object_id, name, topic=topic, icon=icon)
        return Entity("camera", object_id, payload)

    def trigger(self, object_id: str, trigger_type: str, subtype: str, topic: str, payload: str) -> Entity:
        """A native HA *device trigger* (``device_automation``): no state, just an event."""
        data = {
            "automation_type": "trigger",
            "type": trigger_type,
            "subtype": subtype,
            "topic": topic,
            "payload": payload,
            "device": self.device_block(),
            "origin": {"name": "home-assistant-pc-link", "sw_version": __version__, "support_url": SUPPORT_URL},
        }
        return Entity("device_automation", object_id, data)

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
        if self.cfg.time_limits.pin_enabled:
            e.append(self.text("parent_pin", "Parent PIN", icon="mdi:form-textbox-password", password=True,
                               state_topic=self.state_topic, key="parent_pin_state", config=True))
            e.append(self.binary_sensor("parent_unlocked", "Parent PIN unlocked", "parent_unlocked", device_class="lock", icon="mdi:shield-key"))
        e.append(self.switch("parent_present", "Parent at home", "parent_present", icon="mdi:home-account"))
        e.append(self.button("reload_config", "Reload configuration", icon="mdi:reload", config=True))
        for app in self.cfg.apps:
            if app.launch:
                e.append(self.button(f"launch_{app.slug}", f"Launch {app.name}", icon=app.icon))
            if app.process_name:
                e.append(self.button(f"close_{app.slug}", f"Close {app.name}", icon="mdi:close-box-outline"))
        # power state as a plain on/off sensor (retained availability topic)
        online = self._base("online", "Power", state_topic=self.availability_topic,
                            payload_on="online", payload_off="offline", device_class="power")
        online.pop("availability_topic", None)
        online.pop("payload_available", None)
        online.pop("payload_not_available", None)
        e.append(Entity("binary_sensor", "online", online))
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
        return e

    # -- per user -------------------------------------------------------------
    def user_entities(self, user: str, policy: Optional[UserPolicy], managed: bool) -> list[Entity]:
        slug = slugify(user)
        label = policy.label if policy else user
        topic = self.user_state_topic(slug)
        pre = f"user_{slug}_"
        e: list[Entity] = []

        def s(oid, name, key, **kw):
            return self.sensor(pre + oid, f"{label} {name}", key, state_topic=topic, **kw)

        e.append(s("status", "status", "status", device_class="enum", options=USER_STATUS_OPTIONS, icon="mdi:account", attributes_key="sessions"))
        e.append(s("presence", "presence", "presence", device_class="enum", options=PRESENCE_OPTIONS, icon="mdi:account-eye", attributes_key="presence_details"))
        e.append(self.binary_sensor(pre + "logged_in", f"{label} logged in", "logged_in", state_topic=topic, device_class="presence"))
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
            e.append(self.number(pre + "volume", f"{label} volume", "volume", state_topic=topic, minimum=0, maximum=100, step=1, unit="%", icon="mdi:volume-high", mode="slider", config=False))
            e.append(self.switch(pre + "muted", f"{label} muted", "muted", state_topic=topic, icon="mdi:volume-mute", config=False))
        if managed and policy is not None:
            e.append(s("time_remaining", "time remaining", "remaining_minutes", unit="min", device_class="duration", state_class="measurement", icon="mdi:timer-outline", precision=0, attributes_key="limit_details"))
            e.append(s("daily_limit", "daily limit", "limit_minutes", unit="min", device_class="duration", icon="mdi:timer-cog", precision=0))
            e.append(s("weekly_remaining", "weekly time remaining", "weekly_remaining_minutes", unit="min", device_class="duration", state_class="measurement", icon="mdi:calendar-clock", precision=0, enabled=policy.weekly_limit_minutes is not None))
            e.append(s("rollover", "rolled-over minutes", "rollover_minutes", unit="min", device_class="duration", icon="mdi:timer-refresh-outline", precision=0, enabled=policy.rollover_max_minutes > 0))
            e.append(s("earned_today", "earned today", "earned_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:star-circle", precision=0, enabled=bool(policy.rewards)))
            e.append(self.binary_sensor(pre + "login_blocked", f"{label} login blocked", "login_blocked", state_topic=topic, device_class="lock", icon="mdi:account-lock"))
            e.append(self.binary_sensor(pre + "in_allowed_hours", f"{label} within allowed hours", "in_allowed_hours", state_topic=topic, icon="mdi:clock-check"))
            e.append(self.number(pre + "limit_today", f"{label} limit today", "limit_today", state_topic=topic, icon="mdi:timer-edit"))
            e.append(self.number(pre + "weekday_limit", f"{label} weekday limit", "weekday_limit", state_topic=topic, icon="mdi:calendar-week"))
            e.append(self.number(pre + "weekend_limit", f"{label} weekend limit", "weekend_limit", state_topic=topic, icon="mdi:calendar-weekend"))
            e.append(self.number(pre + "weekly_limit", f"{label} weekly limit", "weekly_limit", state_topic=topic, maximum=10080, step=15, icon="mdi:calendar-clock"))
            e.append(self.number(pre + "bonus_today", f"{label} bonus today", "bonus_minutes", state_topic=topic, minimum=-480, maximum=480, icon="mdi:timer-plus", config=False))
            e.append(self.number(pre + "add_minutes", f"{label} add minutes", "add_minutes", state_topic=topic, minimum=0, maximum=480, icon="mdi:timer-plus-outline", config=False))
            e.append(self.button(pre + "add_15", f"{label} +15 min", icon="mdi:timer-plus"))
            e.append(self.button(pre + "add_30", f"{label} +30 min", icon="mdi:timer-plus"))
            for reward in policy.rewards:
                e.append(self.button(pre + "reward_" + reward.slug, f"{label} reward: {reward.name} (+{reward.minutes} min)", icon=reward.icon))
            e.append(self.switch(pre + "limit_enabled", f"{label} limit enabled", "limit_enabled", state_topic=topic, icon="mdi:timer-lock"))
            e.append(self.switch(pre + "blocked", f"{label} access paused", "blocked", state_topic=topic, icon="mdi:account-cancel", config=False))
            # bedtime -----------------------------------------------------------
            if policy.has_bedtime:
                e.append(s("bedtime", "bedtime tonight", "bedtime", icon="mdi:bed-clock", attributes_key="bedtime_details"))
                e.append(self.binary_sensor(pre + "past_bedtime", f"{label} past bedtime", "past_bedtime", state_topic=topic, icon="mdi:sleep"))
                e.append(self.number(pre + "bedtime_extension", f"{label} bedtime extension", "bedtime_extension_minutes", state_topic=topic, maximum=240, step=15, icon="mdi:bed-clock", config=False))
                e.append(self.button(pre + "extend_bedtime_30", f"{label} bedtime +30 min", icon="mdi:bed-clock"))
            # focus mode ----------------------------------------------------------------
            e.append(self.switch(pre + "focus_mode", f"{label} focus mode", "focus_active", state_topic=topic, icon="mdi:book-open-page-variant", config=False))
            e.append(self.number(pre + "focus_minutes", f"{label} focus duration", "focus_minutes", state_topic=topic, minimum=5, maximum=480, step=5, icon="mdi:timer-cog-outline"))
            e.append(s("focus_remaining", "focus time remaining", "focus_remaining_minutes", unit="min", device_class="duration", icon="mdi:book-clock", precision=0, attributes_key="focus_details"))
            # breaks ------------------------------------------------------------------------
            e.append(self.switch(pre + "breaks_enabled", f"{label} break reminders", "breaks_enabled", state_topic=topic, icon="mdi:eye-refresh"))
            e.append(s("continuous_use", "continuous screen time", "continuous_minutes", unit="min", device_class="duration", state_class="measurement", icon="mdi:timer-play", precision=0, attributes_key="break_details"))
            # per-app / per-site rules ---------------------------------------------------------
            for rule in policy.rules:
                rp = pre + "rule_" + rule.slug
                e.append(self.sensor(rp, f"{label} {rule.name}", f"rules.{rule.slug}.state", state_topic=topic, device_class="enum",
                                     options=RULE_STATE_OPTIONS, icon=rule.icon, attributes_key=f"rules.{rule.slug}"))
                e.append(self.switch(rp + "_enabled", f"{label} {rule.name} rule", f"rules.{rule.slug}.enabled", state_topic=topic, icon=rule.icon))
                if rule.has_limit:
                    e.append(self.sensor(rp + "_remaining", f"{label} {rule.name} remaining", f"rules.{rule.slug}.remaining_minutes", state_topic=topic,
                                         unit="min", device_class="duration", state_class="measurement", icon=rule.icon, precision=0))
                    e.append(self.number(rp + "_limit_today", f"{label} {rule.name} limit today", f"rules.{rule.slug}.limit_minutes", state_topic=topic, icon=rule.icon))
        if self._screenshot_allowed(user, managed):
            e.append(self.button(pre + "screenshot", f"{label} take screenshot", icon="mdi:monitor-screenshot", config=True))
            e.append(self.camera(pre + "screenshot_image", f"{label} screenshot", self.screenshot_topic(slug), icon="mdi:monitor-screenshot"))
        e.append(self.button(pre + "logoff", f"{label} log off", icon="mdi:logout"))
        e.append(self.button(pre + "lock", f"{label} lock", icon="mdi:lock"))
        e.append(self.text(pre + "message", f"{label} message", icon="mdi:message-text"))
        return e

    def screenshot_topic(self, slug: str) -> str:
        return f"{self.base_topic}/user/{slug}/screenshot"

    def _screenshot_allowed(self, user: str, managed: bool) -> bool:
        sc = self.cfg.screenshots
        if not sc.enabled:
            return False
        if sc.users:
            return user.lower() in sc.users
        return managed

    # -- device triggers ---------------------------------------------------------------
    def user_trigger_entities(self, user: str, policy: Optional[UserPolicy]) -> list[Entity]:
        slug = slugify(user)
        label = policy.label if policy else user
        topic = f"{self.base_topic}/event/user/{slug}"
        return [self.trigger(f"trigger_{slug}_{kind}", text, label, topic, kind) for kind, text in USER_TRIGGERS.items()]

    def pc_trigger_entities(self) -> list[Entity]:
        topic = f"{self.base_topic}/event/pc"
        return [self.trigger(f"trigger_pc_{kind}", text, self.cfg.device.name, topic, kind) for kind, text in PC_TRIGGERS.items()]

    # -- fleet roll-up device (published by the leader only) -------------------------------
    def fleet_device_block(self) -> dict[str, Any]:
        return {
            "identifiers": ["pclink_fleet"],
            "name": self.cfg.fleet.device_name,
            "manufacturer": self.cfg.device.manufacturer,
            "model": "Roll-up of all PCs",
            "sw_version": __version__,
        }

    def fleet_entities(self, users: dict[str, dict[str, Any]], fleet_state_topic: str,
                       user_topic: Callable[[str], str]) -> list[Entity]:
        """Entities of the virtual 'all PCs' device.  ``users`` is the rollup's user map.

        Every PC publishes identical payloads for these (same unique ids), so
        whichever PC is leader can refresh them without creating duplicates."""
        e: list[Entity] = []
        dev = self.fleet_device_block()
        base = self._base

        def ent(component, object_id, name, **extra):
            payload = base(object_id, name, **extra)
            payload["unique_id"] = f"pclink_fleet_{object_id}"
            payload["device"] = dict(dev)
            payload.pop("availability_topic", None)
            payload.pop("payload_available", None)
            payload.pop("payload_not_available", None)
            return Entity(component, object_id, payload)

        e.append(ent("sensor", "fleet_pcs_online", "PCs online", state_topic=fleet_state_topic, value_template=self._tpl("pcs_online"),
                     icon="mdi:desktop-tower-monitor", state_class="measurement",
                     json_attributes_topic=fleet_state_topic, json_attributes_template="{{ value_json.pcs | default({}) | tojson }}"))
        e.append(ent("sensor", "fleet_pcs_in_use", "PCs in use", state_topic=fleet_state_topic, value_template=self._tpl("pcs_in_use"),
                     icon="mdi:account-multiple-check", state_class="measurement",
                     json_attributes_topic=fleet_state_topic, json_attributes_template="{{ {'pcs': value_json.pcs_in_use_list, 'active_users': value_json.active_users} | tojson }}"))
        e.append(ent("binary_sensor", "fleet_any_pc_in_use", "Any PC in use", state_topic=fleet_state_topic,
                     value_template="{{ 'ON' if value_json.any_pc_in_use else 'OFF' }}", device_class="occupancy"))
        for user, info in sorted(users.items()):
            slug = slugify(user)
            label = info.get("name") or user
            topic = user_topic(slug)
            e.append(ent("sensor", f"fleet_user_{slug}_total_today", f"{label} total time today (all PCs)", state_topic=topic,
                         value_template=self._tpl("total_minutes_today"), unit_of_measurement="min", device_class="duration",
                         state_class="total_increasing", icon="mdi:timer-sand", suggested_display_precision=0,
                         json_attributes_topic=topic, json_attributes_template="{{ value_json | tojson }}"))
            e.append(ent("sensor", f"fleet_user_{slug}_status", f"{label} status (all PCs)", state_topic=topic,
                         value_template=self._tpl("status"), device_class="enum", options=USER_STATUS_OPTIONS, icon="mdi:account"))
            e.append(ent("sensor", f"fleet_user_{slug}_active_on", f"{label} active on", state_topic=topic,
                         value_template="{{ (value_json.active_on | default([]) | join(', ')) or 'none' }}", icon="mdi:monitor-account"))
            e.append(ent("binary_sensor", f"fleet_user_{slug}_logged_in", f"{label} logged in anywhere", state_topic=topic,
                         value_template="{{ 'ON' if value_json.logged_in_on else 'OFF' }}", device_class="presence"))
        return e

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
        return e
