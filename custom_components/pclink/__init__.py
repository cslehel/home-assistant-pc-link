"""PC Link: screen-time limits, presence, telemetry and control for Windows PCs.

Talks to the PC Link service on each PC over its local API (REST + WebSocket);
no MQTT broker is needed.  Entities are created from the catalog the PC
publishes, so new users, rules, apps or sensors appear without an update here.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import PclinkClient
from .const import CONF_TOKEN, DEFAULT_PORT, DOMAIN, PLATFORMS
from .coordinator import PclinkCoordinator
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Nothing to do: everything is configured through config entries."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = PclinkClient(async_get_clientsession(hass), entry.data[CONF_HOST],
                          entry.data.get(CONF_PORT, DEFAULT_PORT), entry.data.get(CONF_TOKEN))
    coordinator = PclinkCoordinator(hass, entry, client)
    await coordinator.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    async_setup_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: PclinkCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            async_unload_services(hass)
    return ok
