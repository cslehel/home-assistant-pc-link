"""Home Assistant MQTT discovery payload builders."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import __version__
from .config import Config, CustomCommand, UserPolicy
from .models import HwReading
from .util import slugify

SUPPORT_URL = "https://github.com/cslehel/home-assistant-pc-link"

USER_STATUS_OPTIONS = ["logged_off", "active", "idle", "locked", "disconnected"]
ACTIVITY_OPTIONS = ["none", "locked", "idle", "gaming", "browsing", "app"]

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
    def _tpl(key: str) -> str:
        return "{{ value_json.%s | default(none) }}" % key

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
            payload["json_attributes_template"] = "{{ value_json.%s | default({}) | tojson }}" % attributes_key
        return Entity("sensor", object_id, payload)

    def binary_sensor(self, object_id: str, name: str, key: str, *, state_topic: Optional[str] = None,
                      device_class: Optional[str] = None, icon: Optional[str] = None,
                      diagnostic: bool = False) -> Entity:
        payload = self._base(
            object_id, name, state_topic=state_topic or self.state_topic,
            value_template="{{ 'ON' if value_json.%s else 'OFF' }}" % key,
            device_class=device_class, icon=icon, entity_category="diagnostic" if diagnostic else None,
        )
        return Entity("binary_sensor", object_id, payload)

    def switch(self, object_id: str, name: str, key: str, *, state_topic: Optional[str] = None,
               icon: Optional[str] = None, config: bool = True) -> Entity:
        payload = self._base(
            object_id, name, state_topic=state_topic or self.state_topic,
            command_topic=self.set_topic(object_id),
            value_template="{{ 'ON' if value_json.%s else 'OFF' }}" % key,
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

    def text(self, object_id: str, name: str, *, icon: Optional[str] = None) -> Entity:
        payload = self._base(
            object_id, name, command_topic=self.set_topic(object_id),
            state_topic=self.set_topic(object_id), icon=icon, mode="text", max=255, min=0,
        )
        return Entity("text", object_id, payload)

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
        e.append(s("mac_address", "MAC address", "mac", icon="mdi:ethernet", diagnostic=True, attributes_key="interfaces"))
        e.append(s("ip_address", "IP address", "ip", icon="mdi:ip-network", diagnostic=True))
        e.append(s("agent_status", "Service", "service_status", icon="mdi:cog", diagnostic=True, attributes_key="service_info"))
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
        e.append(self.binary_sensor(pre + "logged_in", f"{label} logged in", "logged_in", state_topic=topic, device_class="presence"))
        e.append(s("activity", "activity", "activity", icon="mdi:controller", attributes_key="activity_details"))
        e.append(s("current_app", "current app", "app", icon="mdi:application"))
        e.append(s("current_website", "current website", "domain", icon="mdi:web"))
        e.append(s("time_used_today", "time used today", "used_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:timer-sand", precision=0, attributes_key="history"))
        e.append(s("gaming_today", "gaming today", "gaming_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:gamepad-variant", precision=0, attributes_key="top_games"))
        e.append(s("browsing_today", "browsing today", "browsing_minutes", unit="min", device_class="duration", state_class="total_increasing", icon="mdi:web", precision=0, attributes_key="top_sites"))
        e.append(s("top_apps", "top apps today", "top_app", icon="mdi:apps", attributes_key="top_apps", enabled=False))
        if managed:
            e.append(s("time_remaining", "time remaining", "remaining_minutes", unit="min", device_class="duration", state_class="measurement", icon="mdi:timer-outline", precision=0, attributes_key="limit_details"))
            e.append(s("daily_limit", "daily limit", "limit_minutes", unit="min", device_class="duration", icon="mdi:timer-cog", precision=0))
            e.append(self.binary_sensor(pre + "login_blocked", f"{label} login blocked", "login_blocked", state_topic=topic, device_class="lock", icon="mdi:account-lock"))
            e.append(self.binary_sensor(pre + "in_allowed_hours", f"{label} within allowed hours", "in_allowed_hours", state_topic=topic, icon="mdi:clock-check"))
            e.append(self.number(pre + "limit_today", f"{label} limit today", "limit_today", state_topic=topic, icon="mdi:timer-edit"))
            e.append(self.number(pre + "weekday_limit", f"{label} weekday limit", "weekday_limit", state_topic=topic, icon="mdi:calendar-week"))
            e.append(self.number(pre + "weekend_limit", f"{label} weekend limit", "weekend_limit", state_topic=topic, icon="mdi:calendar-weekend"))
            e.append(self.number(pre + "bonus_today", f"{label} bonus today", "bonus_minutes", state_topic=topic, minimum=-480, maximum=480, icon="mdi:timer-plus", config=False))
            e.append(self.button(pre + "add_15", f"{label} +15 min", icon="mdi:timer-plus"))
            e.append(self.button(pre + "add_30", f"{label} +30 min", icon="mdi:timer-plus"))
            e.append(self.switch(pre + "limit_enabled", f"{label} limit enabled", "limit_enabled", state_topic=topic, icon="mdi:timer-lock"))
            e.append(self.switch(pre + "blocked", f"{label} access paused", "blocked", state_topic=topic, icon="mdi:account-cancel", config=False))
        e.append(self.button(pre + "logoff", f"{label} log off", icon="mdi:logout"))
        e.append(self.button(pre + "lock", f"{label} lock", icon="mdi:lock"))
        e.append(self.text(pre + "message", f"{label} message", icon="mdi:message-text"))
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
