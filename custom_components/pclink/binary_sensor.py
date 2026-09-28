"""Binary sensors (logged in, login blocked, someone at the PC, reboot pending, ...)."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PclinkCoordinator
from .entity import PclinkEntity, async_setup_platform_entities


class PclinkBinarySensor(PclinkEntity, BinarySensorEntity):
    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coordinator, spec)
        try:
            self._attr_device_class = BinarySensorDeviceClass(spec["device_class"]) if spec.get("device_class") else None
        except ValueError:
            self._attr_device_class = None

    @property
    def is_on(self) -> Optional[bool]:
        raw = self.value
        return None if raw is None else bool(raw)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "binary_sensor", PclinkBinarySensor)
