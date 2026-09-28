"""Text inputs (parent PIN, message to a user / everyone)."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PclinkCoordinator
from .entity import PclinkEntity, async_setup_platform_entities


class PclinkText(PclinkEntity, TextEntity):
    _attr_native_max = 255

    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coordinator, spec)
        self._password = bool(spec.get("password"))
        self._attr_mode = TextMode.PASSWORD if self._password else TextMode.TEXT
        self._last: str = ""

    @property
    def native_value(self) -> Optional[str]:
        if self.spec.get("value"):
            raw = self.value
            return "" if raw is None else str(raw)[:255]
        return self._last

    async def async_set_value(self, value: str) -> None:
        await self.async_send(value)
        self._last = "" if self._password else value
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "text", PclinkText)
