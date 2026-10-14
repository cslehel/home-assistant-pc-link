"""Device triggers: "Kevin ran out of time", "PC needs a reboot for updates", ...

The PC reports every event over the local API; the coordinator fires it as a
``pclink_event`` on the bus and these triggers match on it.
"""
from __future__ import annotations

from typing import Any, Optional

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_TYPE, PC_TRIGGERS, ROLLUP_DEVICE_ID, USER_TRIGGERS, device_entry_ids
from .coordinator import PclinkCoordinator

CONF_SUBTYPE = "subtype"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({
    vol.Required(CONF_TYPE): cv.string,
    vol.Optional(CONF_SUBTYPE): cv.string,
})


def coordinator_for_device(hass: HomeAssistant, device_id: str) -> Optional[PclinkCoordinator]:
    device = dr.async_get(hass).async_get(device_id)
    if device is None or (DOMAIN, ROLLUP_DEVICE_ID) in device.identifiers:
        return None  # the "all PCs" device belongs to a PC's entry, but no PC's events are its own
    coordinators = hass.data.get(DOMAIN, {})
    for entry_id in device_entry_ids(device):
        if entry_id in coordinators:
            return coordinators[entry_id]
    return None


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    coordinator = coordinator_for_device(hass, device_id)
    if coordinator is None:
        return []
    catalog_triggers = coordinator.catalog.get("triggers") or {}
    pc_types = list(catalog_triggers.get("pc") or PC_TRIGGERS)
    user_types = list(catalog_triggers.get("user") or USER_TRIGGERS)
    base = {CONF_PLATFORM: "device", CONF_DOMAIN: DOMAIN, CONF_DEVICE_ID: device_id}
    triggers = [{**base, CONF_TYPE: kind} for kind in pc_types]
    for user in sorted(coordinator.catalog.get("users") or {}):
        triggers += [{**base, CONF_TYPE: kind, CONF_SUBTYPE: user} for kind in user_types]
    return triggers


async def async_attach_trigger(hass: HomeAssistant, config: ConfigType, action: TriggerActionType,
                               trigger_info: TriggerInfo) -> CALLBACK_TYPE:
    event_data: dict[str, Any] = {CONF_DEVICE_ID: config[CONF_DEVICE_ID], "event": config[CONF_TYPE]}
    if config.get(CONF_SUBTYPE):
        event_data["user"] = config[CONF_SUBTYPE]
    event_config = event_trigger.TRIGGER_SCHEMA({
        event_trigger.CONF_PLATFORM: "event",
        event_trigger.CONF_EVENT_TYPE: EVENT_TYPE,
        event_trigger.CONF_EVENT_DATA: event_data,
    })
    return await event_trigger.async_attach_trigger(hass, event_config, action, trigger_info, platform_type="device")
