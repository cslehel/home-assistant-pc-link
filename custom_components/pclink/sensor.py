"""Sensors (telemetry, hardware readings, per-user statistics and limits)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import PclinkCoordinator
from .entity import PclinkEntity, async_setup_platform_entities

_LOGGER = logging.getLogger(__name__)

_NON_NUMERIC = {SensorDeviceClass.ENUM, SensorDeviceClass.TIMESTAMP, SensorDeviceClass.DATE}


def _enum(cls, value: Optional[str]):
    if not value:
        return None
    try:
        return cls(value)
    except ValueError:
        _LOGGER.debug("unknown %s %r", cls.__name__, value)
        return None


class PclinkSensor(PclinkEntity, SensorEntity):
    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coordinator, spec)
        self._attr_device_class = _enum(SensorDeviceClass, spec.get("device_class"))
        self._attr_state_class = _enum(SensorStateClass, spec.get("state_class"))
        self._attr_native_unit_of_measurement = spec.get("unit") or None
        if self._attr_device_class == SensorDeviceClass.ENUM:
            self._attr_options = [str(o) for o in (spec.get("options") or [])]
            self._attr_native_unit_of_measurement = None
            self._attr_state_class = None
        if spec.get("precision") is not None:
            self._attr_suggested_display_precision = int(spec["precision"])
        self._numeric = bool(self._attr_native_unit_of_measurement or self._attr_state_class) or (
            self._attr_device_class is not None and self._attr_device_class not in _NON_NUMERIC
        )

    @property
    def native_value(self) -> Any:
        raw = self.value
        if raw is None or raw == "":
            return None
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP:
            parsed = dt_util.parse_datetime(str(raw))
            if parsed is None:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        if self._attr_device_class == SensorDeviceClass.ENUM:
            text = str(raw)
            return text if text in (self._attr_options or []) else None
        if self._numeric:
            if isinstance(raw, bool):
                return int(raw)
            if isinstance(raw, (int, float)):
                return raw
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        return str(raw)[:255]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "sensor", PclinkSensor)
