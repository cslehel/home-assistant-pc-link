"""Buttons (power, +15/+30 minutes, rewards, log off, lock, screenshot, launch/close apps, custom commands)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PclinkCoordinator
from .entity import PclinkEntity, async_setup_platform_entities


class PclinkButton(PclinkEntity, ButtonEntity):
    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coordinator, spec)
        try:
            self._attr_device_class = ButtonDeviceClass(spec["device_class"]) if spec.get("device_class") else None
        except ValueError:
            self._attr_device_class = None

    @property
    def available(self) -> bool:
        return self.coordinator.online  # buttons have no state; the PC only has to be reachable

    async def async_press(self) -> None:
        await self.async_send("PRESS")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "button", PclinkButton)
