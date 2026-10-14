"""Switches (time limits, access paused, focus mode, rules, break reminders, mute, parent at home)."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PclinkEntity, async_setup_platform_entities


class PclinkSwitch(PclinkEntity, SwitchEntity):
    @property
    def is_on(self) -> Optional[bool]:
        raw = self.value
        return None if raw is None else bool(raw)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_send("ON", shown=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_send("OFF", shown=False)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "switch", PclinkSwitch)
