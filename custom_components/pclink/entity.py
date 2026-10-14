"""Base entity: one catalog entry -> one Home Assistant entity."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PclinkApiError, PclinkAuthError, PclinkCommandError, PclinkThrottledError, PclinkWrongDeviceError
from .const import DOMAIN
from .coordinator import PclinkCoordinator

_LOGGER = logging.getLogger(__name__)

_CATEGORIES = {"config": EntityCategory.CONFIG, "diagnostic": EntityCategory.DIAGNOSTIC}

# "no value set by hand is being shown" (None is a value an entity can have)
_NOT_SET: Any = object()

# Home Assistant's (name-mangled) union of a class's ``_unrecorded_attributes`` and its
# platform's; the recorder reads it from the entity when it is added.
_COMBINED_UNRECORDED = "_Entity__combined_unrecorded_attributes"

# For a PC whose catalog has no ``unrecorded_attributes`` yet: the payload parts whose
# attributes change with nearly every push (top processes, today's apps / sites / games,
# the history, presence and activity details, media, sessions ...).  Recording them every
# few seconds would fill the database; none of them is worth a history of its own.
VOLATILE_ATTRIBUTE_SOURCES = frozenset({
    "top_processes", "top_memory_processes", "history", "top_apps", "top_sites", "top_games", "usage_recent",
    "presence_details", "activity_details", "media", "break_details", "sessions",
})


class PclinkEntity(CoordinatorEntity[PclinkCoordinator]):
    """Common behaviour for every catalog-driven entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self.spec = spec
        self._in_hass = False
        # What was just set from Home Assistant, shown until the PC's next state arrives
        # (switches, numbers, selects, texts): the control does not jump back meanwhile.
        self._optimistic: Any = _NOT_SET
        self._attr_unique_id = coordinator.unique_id_for(spec)
        # Registry-level properties: taken once, when the entity is first registered.
        self._attr_entity_category = _CATEGORIES.get(str(spec.get("category") or ""))
        self._attr_entity_registry_enabled_default = bool(spec.get("enabled", True))
        self._attr_device_info = coordinator.device_info
        self._apply_spec()

    def _apply_spec(self) -> None:
        """Set everything that comes from ``self.spec`` and may change when the PC reloads its
        configuration (name, icon; platforms add limits, units, options ...)."""
        self._attr_name = str(self.spec.get("name") or self.spec["key"])
        self._attr_icon = self.spec.get("icon")
        self._apply_unrecorded()

    def _apply_unrecorded(self) -> None:
        """Keep the attributes that change with every push out of the recorder.

        Home Assistant takes them from a class attribute; the catalog decides them per
        entity, so this instance gets its own copy of the combined set (the platform's own
        ones included).  A PC that does not send ``unrecorded_attributes`` yet gets the
        built-in list (``VOLATILE_ATTRIBUTE_SOURCES``)."""
        listed = self.spec.get("unrecorded_attributes")
        if isinstance(listed, (list, tuple, set, frozenset)):
            extra = frozenset(str(name) for name in listed)
        else:
            source = str(self.spec.get("attributes") or "").rsplit(".", 1)[-1]
            extra = frozenset({MATCH_ALL}) if source in VOLATILE_ATTRIBUTE_SOURCES else frozenset()
        combined = frozenset(getattr(type(self), _COMBINED_UNRECORDED, frozenset())) | extra
        setattr(self, _COMBINED_UNRECORDED, combined)
        state_info = getattr(self, "_state_info", None)
        if isinstance(state_info, dict) and "unrecorded_attributes" in state_info:
            state_info["unrecorded_attributes"] = combined  # already added: the next write uses it

    @callback
    def async_update_spec(self, spec: dict[str, Any]) -> None:
        """The catalog now describes this entity differently: adopt it without a reload."""
        self.spec = spec
        self._apply_spec()
        if self._in_hass:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._in_hass = True

    async def async_will_remove_from_hass(self) -> None:
        self._in_hass = False
        if self.coordinator.live.get(self.key) is self:
            self.coordinator.live.pop(self.key, None)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic = _NOT_SET  # the PC has spoken
        super()._handle_coordinator_update()

    @property
    def key(self) -> str:
        return str(self.spec["key"])

    @property
    def in_catalog(self) -> bool:
        """False once the PC stopped listing this entity (deleted user, rule, app ...)."""
        return self.key in self.coordinator.catalog_keys

    @property
    def available(self) -> bool:
        return self.coordinator.online and self.in_catalog and self.coordinator.payload_for(self.spec) is not None

    @property
    def value(self) -> Any:
        if self._optimistic is not _NOT_SET:
            return self._optimistic
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

    async def _send(self, value: Any) -> dict[str, Any]:
        """The entity's ``set`` command; raises the client's errors as they are."""
        key = str(self.spec.get("command") or self.key)
        return await self.coordinator.async_command({"action": "set", "entity": key, "value": value})

    async def async_send(self, value: Any, shown: Any = _NOT_SET) -> None:
        """Send an entity command (``set``) to the PC and surface refusals (e.g. parent PIN) in the UI.

        ``shown`` is the value the entity displays until the PC's next state (see ``_optimistic``)."""
        try:
            await self._send(value)
        except PclinkThrottledError as exc:   # the PC asked to wait (or we still are): nothing changed
            raise self.coordinator.throttled_error(exc) from exc
        except PclinkCommandError as exc:
            raise HomeAssistantError(f"{self.coordinator.pc_name}: {self._attr_name}: {exc}") from exc
        except PclinkAuthError as exc:
            self.coordinator.async_start_reauth("the API token was rejected for a command")
            raise HomeAssistantError(
                f"{self.coordinator.pc_name} rejected the API token; enter the current one under Settings > Devices & services") from exc
        except PclinkWrongDeviceError as exc:
            raise HomeAssistantError(f"{self.coordinator.entry.title}: {exc}") from exc
        except PclinkApiError as exc:
            raise HomeAssistantError(f"{self.coordinator.pc_name} could not be reached: {exc}") from exc
        if shown is not _NOT_SET:
            self._optimistic = shown
            if self._in_hass:
                self.async_write_ha_state()
        await self.coordinator.async_request_state()


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

    @callback
    def _add(specs: list[dict[str, Any]]) -> None:
        if coordinator.stopped:  # the entry is being unloaded: the platform is (being) torn down
            return
        fresh: list[Entity] = []
        for spec in specs:
            if spec.get("platform") != platform or spec["key"] in coordinator.live:
                continue
            try:
                entity = factory(coordinator, spec)
            except Exception:  # noqa: BLE001 - one malformed entry must not cost the others
                _LOGGER.exception("%s: cannot create %s %s", coordinator.pc_name, platform, spec.get("key"))
                continue
            coordinator.live[spec["key"]] = entity  # recorded only once it exists
            fresh.append(entity)
        if fresh:
            async_add_entities(fresh)

    entry.async_on_unload(async_dispatcher_connect(hass, coordinator.signal_new_entities, _add))
    _add(coordinator.catalog_entities(platform))
