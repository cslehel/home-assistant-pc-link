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
        self._last: str = ""
        super().__init__(coordinator, spec)

    def _apply_spec(self) -> None:
        super()._apply_spec()
        self._password = bool(self.spec.get("password"))
        self._attr_mode = TextMode.PASSWORD if self._password else TextMode.TEXT
        if self._password:
            self._last = ""

    @property
    def native_value(self) -> Optional[str]:
        if self.spec.get("value"):
            raw = self.value
            return "" if raw is None else str(raw)[:255]
        return self._last

    async def async_set_value(self, value: str) -> None:
        # what was typed is kept on the entity (a PIN never is) once the PC took it; a text the
        # PC reports a value for shows the typed one until the PC's next state
        if self.spec.get("value") and not self._password:
            await self.async_send(value, shown=value)
        else:
            await self.async_send(value)
        self._last = "" if self._password else value
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "text", PclinkText)
