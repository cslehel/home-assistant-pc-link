"""Numbers (limits, bonus minutes, bedtime extension, focus duration, volume, rule limits)."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PclinkCoordinator
from .entity import PclinkEntity, async_setup_platform_entities


class PclinkNumber(PclinkEntity, NumberEntity):
    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coordinator, spec)
        self._attr_native_min_value = float(spec.get("min", 0))
        self._attr_native_max_value = float(spec.get("max", 1440))
        self._attr_native_step = float(spec.get("step", 1))
        self._attr_native_unit_of_measurement = spec.get("unit") or None
        try:
            self._attr_mode = NumberMode(str(spec.get("mode") or "box"))
        except ValueError:
            self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> Optional[float]:
        raw = self.value
        try:
            return None if raw is None else float(raw)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.async_send(int(value) if float(value).is_integer() else value)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "number", PclinkNumber)
