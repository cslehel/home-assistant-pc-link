"""Selects (a choice from a list the PC offers)."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PclinkEntity, async_setup_platform_entities


class PclinkSelect(PclinkEntity, SelectEntity):
    def _apply_spec(self) -> None:
        super()._apply_spec()
        # follows the catalog: a reloaded configuration on the PC may add or drop a choice
        self._listed = [str(option) for option in (self.spec.get("options") or [])]

    @property
    def options(self) -> list[str]:
        """The choices the PC offers - and the current value when it is not one of them (the
        state arrived before the new catalog, or the PC kept a choice that was removed):
        Home Assistant shows a select's value only when it is among its options."""
        current = self.current_option
        return self._listed if current is None or current in self._listed else [*self._listed, current]

    @property
    def current_option(self) -> Optional[str]:
        raw = self.value
        return None if raw is None or raw == "" else str(raw)[:255]

    async def async_select_option(self, option: str) -> None:
        await self.async_send(option, shown=option)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "select", PclinkSelect)
