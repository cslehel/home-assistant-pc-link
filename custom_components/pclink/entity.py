"""Base entity: one catalog entry -> one Home Assistant entity."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PclinkApiError, PclinkCommandError
from .const import DOMAIN
from .coordinator import PclinkCoordinator

_LOGGER = logging.getLogger(__name__)

_CATEGORIES = {"config": EntityCategory.CONFIG, "diagnostic": EntityCategory.DIAGNOSTIC}


class PclinkEntity(CoordinatorEntity[PclinkCoordinator]):
    """Common behaviour for every catalog-driven entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self.spec = spec
        self._attr_unique_id = str(spec.get("unique_id") or f"pclink_{coordinator.pc_id}_{spec['key']}")
        self._attr_name = str(spec.get("name") or spec["key"])
        self._attr_icon = spec.get("icon")
        self._attr_entity_category = _CATEGORIES.get(str(spec.get("category") or ""))
        self._attr_entity_registry_enabled_default = bool(spec.get("enabled", True))
        self._attr_device_info = coordinator.device_info

    @property
    def key(self) -> str:
        return str(self.spec["key"])

    @property
    def available(self) -> bool:
        return self.coordinator.online and self.coordinator.payload_for(self.spec) is not None

    @property
    def value(self) -> Any:
        return self.coordinator.value(self.spec)

    @property
    def extra_state_attributes(self) -> Optional[dict[str, Any]]:
        key = self.spec.get("attributes")
        if not key:
            return None
        attrs = self.coordinator.value(self.spec, "attributes")
        if attrs is None:
            return None
        if isinstance(attrs, dict):
            return attrs
        return {str(key).rsplit(".", 1)[-1]: attrs}

    async def async_send(self, value: Any) -> None:
        """Send an entity command (``set``) to the PC and surface refusals (e.g. parent PIN) in the UI."""
        key = str(self.spec.get("command") or self.key)
        try:
            await self.coordinator.client.set_entity(key, value)
        except PclinkCommandError as exc:
            raise HomeAssistantError(f"{self.coordinator.pc_name}: {self._attr_name}: {exc}") from exc
        except PclinkApiError as exc:
            raise HomeAssistantError(f"{self.coordinator.pc_name} could not be reached: {exc}") from exc
        await self.coordinator.async_request_refresh()


@callback
def async_setup_platform_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    platform: str,
    factory: Callable[[PclinkCoordinator, dict[str, Any]], Entity],
) -> None:
    """Create the platform's entities from the catalog now and whenever the PC reports new ones."""
    coordinator: PclinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def _add(specs: list[dict[str, Any]]) -> None:
        fresh = [s for s in specs if s.get("platform") == platform and s["key"] not in added]
        if not fresh:
            return
        added.update(s["key"] for s in fresh)
        async_add_entities([factory(coordinator, s) for s in fresh])

    entry.async_on_unload(async_dispatcher_connect(hass, coordinator.signal_new_entities, _add))
    _add(coordinator.catalog_entities(platform))
